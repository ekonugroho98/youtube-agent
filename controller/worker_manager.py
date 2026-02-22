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
from .encryption import decrypt
from .ffmpeg_parser import FFmpegLogMonitor, StreamConnectionState


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

    # Starting timeout - how long to wait before considering stream failed (seconds)
    STARTING_TIMEOUT = 30

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
        self.ffmpeg_monitor = FFmpegLogMonitor()

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

        # Reset FFmpeg monitor for new worker
        self.ffmpeg_monitor.reset()

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

        # Environment (inherit from parent, override loop from config)
        env = os.environ.copy()
        env["LOOP_STREAMING"] = "true" if config.loop_streaming else "false"
        env["LOOP_DELAY"] = str(config.loop_delay)

        # Stream key: from config (decrypted) or environment fallback
        if config.youtube_stream_key_encrypted:
            try:
                stream_key = decrypt(config.youtube_stream_key_encrypted)
                env["YOUTUBE_STREAM_KEY"] = stream_key
                logger.info("Using stream key from config (decrypted)")
            except Exception as e:
                logger.error(f"Failed to decrypt stream key: {e}")
                raise WorkerManagerError(f"Failed to decrypt stream key: {str(e)}")
        # Otherwise worker will use YOUTUBE_STREAM_KEY from environment (if set)

        try:
            # Spawn worker process
            self.worker_process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            logger.info(f"Worker started with PID: {self.worker_process.pid}")

            # Update state (last_scheduled_start_date prevents scheduler from starting again same day)
            today = datetime.now().strftime("%Y-%m-%d")
            state = StreamState(
                status=StreamStatus.STARTING,
                worker_pid=self.worker_process.pid,
                started_at=datetime.now().isoformat(),
                media_key=config.media_key,
                last_scheduled_start_date=today,
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
            # Kill any orphaned FFmpeg processes
            await self._kill_orphaned_ffmpeg(pid)

            # Update state
            state = self.persistence.load_state()
            state.status = StreamStatus.STOPPED
            state.worker_pid = None
            state.exited_at = datetime.now().isoformat()
            self.persistence.save_state(state)
            self.worker_process = None

    async def _kill_orphaned_ffmpeg(self, worker_pid: int) -> None:
        """
        Kill any orphaned FFmpeg processes that might have been spawned by this worker.

        Args:
            worker_pid: The worker process PID
        """
        try:
            import psutil

            # Try to find FFmpeg as child of worker
            try:
                worker_proc = psutil.Process(worker_pid)
                children = worker_proc.children(recursive=True)

                for child in children:
                    try:
                        if 'ffmpeg' in child.name().lower():
                            logger.info(f"Killing orphaned FFmpeg child: {child.pid}")
                            child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        except ImportError:
            # Fallback: try to kill FFmpeg with similar start time
            logger.debug("psutil not available, using fallback orphan cleanup")
            # FFmpeg typically starts right after worker, so check PIDs close to worker PID
            for candidate_pid in range(worker_pid + 1, worker_pid + 10):
                try:
                    os.kill(candidate_pid, 0)  # Check if process exists
                    # Could be FFmpeg, try to kill it gracefully
                    os.kill(candidate_pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    continue
        except Exception as e:
            logger.error(f"Error killing orphaned FFmpeg: {e}")

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
        """Start reading worker stdout/stderr for logging and parsing FFmpeg output."""
        async def read_stream(stream, prefix):
            while not self._shutdown_event.is_set():
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    line_str = line.decode().strip()

                    # Parse FFmpeg output from worker stderr
                    # Worker prefixes FFmpeg stderr with "[FFMPEG] ERR"
                    # The line format from worker is like: "2026-02-22 11:16:53 - INFO - [FFMPEG] ERR frame=..."
                    if prefix == "WORKER_ERROR" and "[FFMPEG]" in line_str:
                        # Extract FFmpeg output after "[FFMPEG] ERR"
                        parts = line_str.split("[FFMPEG] ERR")
                        if len(parts) > 1:
                            ffmpeg_line = parts[-1].strip()
                            new_state = self.ffmpeg_monitor.add_line(ffmpeg_line)

                            # Update stream status based on FFmpeg state
                            if new_state:
                                await self._update_status_from_ffmpeg_state(new_state)

                    logger.info(f"[{prefix}] {line_str}")

                except Exception as e:
                    logger.error(f"Error reading stream: {e}")
                    continue

        if self.worker_process:
            if self.worker_process.stdout:
                asyncio.create_task(read_stream(self.worker_process.stdout, "WORKER"))
            if self.worker_process.stderr:
                asyncio.create_task(read_stream(self.worker_process.stderr, "WORKER_ERROR"))

    async def _update_status_from_ffmpeg_state(self, ffmpeg_state: StreamConnectionState) -> None:
        """
        Update stream status based on FFmpeg connection state.

        Args:
            ffmpeg_state: Detected FFmpeg connection state
        """
        try:
            state = self.persistence.load_state()

            # Don't update if worker already stopped/error
            if state.status in (StreamStatus.STOPPED, StreamStatus.ERROR, StreamStatus.FAILED):
                return

            if ffmpeg_state == StreamConnectionState.STREAMING:
                if state.status != StreamStatus.STREAMING:
                    logger.info("FFmpeg is streaming - updating status to STREAMING")
                    state.status = StreamStatus.STREAMING
                    self.persistence.save_state(state)

            elif ffmpeg_state == StreamConnectionState.FAILED:
                if state.status != StreamStatus.FAILED:
                    _, error_msg = self.ffmpeg_monitor.get_state()
                    logger.error(f"FFmpeg connection failed: {error_msg}")
                    state.status = StreamStatus.FAILED
                    state.error_message = error_msg or "FFmpeg connection failed"
                    self.persistence.save_state(state)

                    # Stop worker on connection failure
                    await self.stop_worker()

        except Exception as e:
            logger.error(f"Failed to update status from FFmpeg state: {e}")
