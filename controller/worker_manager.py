"""
Worker process manager for spawning and controlling stream workers.

Handles subprocess lifecycle, health checks, and graceful shutdown.
"""
import os
import signal
import asyncio
import logging
from datetime import datetime
from typing import Optional

from .models import StreamConfig, StreamState, StreamStatus
from .persistence import StreamPersistence


logger = logging.getLogger(__name__)


class WorkerManagerError(Exception):
    """Worker manager error."""
    pass


class WorkerManager:
    """
    Manage worker subprocess lifecycle.

    Spawns, monitors, and terminates worker processes.
    """

    # Health check interval (seconds)
    HEALTH_CHECK_INTERVAL = 30

    # Worker shutdown timeout (seconds)
    SHUTDOWN_TIMEOUT = 10

    def __init__(self, persistence: StreamPersistence):
        """
        Initialize worker manager.

        Args:
            persistence: StreamPersistence instance for state management
        """
        self.persistence = persistence
        self.worker_process: Optional[asyncio.subprocess.Process] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

    async def start_worker(self, config: StreamConfig) -> None:
        """
        Start worker subprocess with given configuration.

        Args:
            config: Stream configuration

        Raises:
            WorkerManagerError: Failed to start worker
        """
        if self.worker_process and not self.worker_process.returncode:
            raise WorkerManagerError("Worker is already running")

        logger.info(f"Starting worker for media: {config.media_key}")

        # Build worker command
        worker_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "worker",
            "worker.py"
        )

        # Build worker command
        worker_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "worker",
            "worker.py"
        )

        # Check if playlist mode
        if config.is_playlist:
            # Pass playlist as JSON argument
            import json
            playlist_json = json.dumps(config.playlist)

            cmd = [
                "python",
                worker_path,
                "--media-key", config.playlist[0],  # First track
                "--rtmp-url", config.youtube_rtmp_url,
                "--playlist", playlist_json,
            ]
        else:
            cmd = [
                "python",
                worker_path,
                "--media-key", config.media_key,
                "--rtmp-url", config.youtube_rtmp_url,
            ]

        # Environment (inherit from parent, stream key from env)
        env = os.environ.copy()
        # Stream key loaded from YOUTUBE_STREAM_KEY env var by worker

        try:
            # Spawn worker process
            self.worker_process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            logger.info(f"Worker started with PID: {self.worker_process.pid}")

            # Update state
            state = StreamState(
                status=StreamStatus.RUNNING,
                worker_pid=self.worker_process.pid,
                started_at=datetime.now().isoformat(),
                media_key=config.media_key,
            )
            self.persistence.save_state(state)

            # Start health checks
            self._start_health_checks()
            self._start_log_reader()

        except Exception as e:
            raise WorkerManagerError(f"Failed to start worker: {str(e)}")

    async def stop_worker(self) -> None:
        """
        Stop worker process gracefully.

        Args:
            WorkerManagerError: Failed to stop worker
        """
        if not self.worker_process:
            raise WorkerManagerError("No worker process running")

        pid = self.worker_process.pid
        logger.info(f"Stopping worker PID: {pid}")

        # Cancel health checks
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()

        try:
            # Send SIGTERM for graceful shutdown
            self.worker_process.send_signal(signal.SIGTERM)

            # Wait for clean shutdown (with timeout)
            try:
                await asyncio.wait_for(
                    self.worker_process.wait(),
                    timeout=self.SHUTDOWN_TIMEOUT
                )
                logger.info(f"Worker {pid} shut down cleanly")

            except asyncio.TimeoutError:
                logger.warning(f"Worker {pid} didn't shut down in {self.SHUTDOWN_TIMEOUT}s")
                # Force kill with SIGKILL
                self.worker_process.kill()
                await self.worker_process.wait()
                logger.info(f"Worker {pid} killed after timeout")

        except Exception as e:
            raise WorkerManagerError(f"Failed to stop worker: {str(e)}")

        finally:
            # Update state
            state = self.persistence.load_state()
            state.status = StreamStatus.STOPPED
            state.worker_pid = None
            state.exited_at = datetime.now().isoformat()
            self.persistence.save_state(state)
            self.worker_process = None

    async def cleanup_orphans(self) -> None:
        """
        Clean up orphaned worker processes from previous controller run.

        Checks state file for PID and verifies if process still exists.
        Terminates orphaned workers.
        """
        try:
            state = self.persistence.load_state()

            if state.status == StreamStatus.RUNNING and state.worker_pid:
                pid = state.worker_pid
                logger.info(f"Checking for orphaned worker PID: {pid}")

                # Check if process exists and is a worker
                try:
                    os.kill(pid, 0)  # Check if process exists
                    # Process exists - check if it's actually a worker
                    # (simple check: command line contains "worker.py")
                    import psutil
                    proc = psutil.Process(pid)
                    cmdline = " ".join(proc.cmdline())
                    if "worker.py" in cmdline:
                        logger.warning(f"Found orphaned worker {pid}, terminating...")
                        os.kill(pid, signal.SIGTERM)
                        await asyncio.sleep(5)

                        # Force kill if still running
                        try:
                            os.kill(pid, 0)
                            logger.warning(f"Orphan {pid} still alive, sending SIGKILL")
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass  # Already dead

                except ProcessLookupError:
                    # Process doesn't exist - clean up state
                    logger.info(f"Orphaned worker {pid} no longer exists")
                except ImportError:
                    logger.warning("psutil not installed, skipping orphan check")
                except Exception as e:
                    logger.error(f"Error checking orphaned worker: {e}")

            # Reset state to stopped
            state.status = StreamStatus.STOPPED
            state.worker_pid = None
            self.persistence.save_state(state)

        except Exception as e:
            logger.error(f"Failed to cleanup orphans: {e}")

    async def shutdown(self) -> None:
        """Clean up on controller shutdown."""
        logger.info("Worker manager shutting down...")

        # Cancel health checks
        if self._health_check_task:
            self._health_check_task.cancel()

        self._shutdown_event.set()

    def _start_health_checks(self) -> None:
        """Start periodic health checks for worker process."""
        async def health_check_loop():
            while not self._shutdown_event.is_set():
                try:
                    await self._check_worker_health()
                    await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Health check error: {e}")

        self._health_check_task = asyncio.create_task(health_check_loop())

    async def _check_worker_health(self) -> None:
        """Check if worker process is still alive."""
        if not self.worker_process:
            return

        # Check process exit code
        if self.worker_process.returncode is not None:
            logger.warning(f"Worker exited with code: {self.worker_process.returncode}")

            # Update state to error
            state = self.persistence.load_state()
            state.status = StreamStatus.ERROR
            state.worker_pid = None
            state.exited_at = datetime.now().isoformat()
            state.exit_code = self.worker_process.returncode
            state.error_message = f"Worker exited unexpectedly with code {self.worker_process.returncode}"
            self.persistence.save_state(state)

            self.worker_process = None
            self._shutdown_event.set()

        else:
            # Update health check timestamp
            state = self.persistence.load_state()
            state.last_health_check = datetime.now().isoformat()
            self.persistence.save_state(state)

    def _start_log_reader(self) -> None:
        """Start reading worker stdout/stderr for logging."""
        async def read_stream(stream, prefix):
            while not self._shutdown_event.is_set():
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    logger.info(f"[{prefix}] {line.decode().strip()}")
                except Exception:
                    break

        if self.worker_process:
            if self.worker_process.stdout:
                asyncio.create_task(read_stream(self.worker_process.stdout, "WORKER"))
            if self.worker_process.stderr:
                asyncio.create_task(read_stream(self.worker_process.stderr, "WORKER_ERROR"))
