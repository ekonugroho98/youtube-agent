"""
FFmpeg subprocess wrapper with auto-reconnect and monitoring.
"""
import os
import signal
import asyncio
import logging
from typing import Optional, List


logger = logging.getLogger(__name__)


class FFmpegError(Exception):
    """FFmpeg error."""
    pass


class FFmpegRunner:
    """
    Manage FFmpeg subprocess for streaming to RTMP.

    Features:
    - Auto-reconnect on network drop
    - Codec copy for MP4 files
    - Transcode fallback for other formats
    - Process monitoring
    - Graceful shutdown
    """

    # FFmpeg path from environment
    FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

    # Shutdown timeout
    SHUTDOWN_TIMEOUT = 10

    def __init__(self, input_url: str, rtmp_url: str, codec_copy: bool = True):
        """
        Initialize FFmpeg runner.

        Args:
            input_url: Signed URL for media input
            rtmp_url: YouTube RTMP destination with stream key
            codec_copy: True to copy codec (MP4), False to transcode
        """
        self.input_url = input_url
        self.rtmp_url = rtmp_url
        self.codec_copy = codec_copy

        self.process: Optional[asyncio.subprocess.Process] = None
        self._shutdown_event = asyncio.Event()

    def _build_command(self) -> List[str]:
        """
        Build FFmpeg command with appropriate flags.

        Returns:
            List of command arguments
        """
        cmd = [
            self.FFMPEG_PATH,
            # Input
            "-re",  # Read input at native frame rate
            "-i", self.input_url,

            # Help YouTube go LIVE faster on first connect (avoid long "Preparing")
            "-flush_packets", "1",       # Flush packets immediately so ingest sees data sooner
            "-avoid_negative_ts", "make_zero",  # Avoid negative timestamps that can delay LIVE

            # Auto-reconnect flags
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]

        # Codec configuration
        if self.codec_copy:
            # Copy codec without re-encoding (MP4)
            cmd.extend(["-c", "copy"])
        else:
            # Transcode to H.264/AAC for YouTube compatibility; keyframe at start helps first connect
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-b:v", "3000k",  # 3Mbps target
                "-maxrate", "3000k",
                "-bufsize", "6000k",
                "-force_key_frames", "expr:gte(t,0)",  # Force keyframe at start for faster LIVE
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
            ])

        # Output
        cmd.extend(["-f", "flv", self.rtmp_url])

        return cmd

    async def run(self) -> None:
        """
        Run FFmpeg subprocess.

        Raises:
            FFmpegError: FFmpeg failed to start or exited with error
        """
        cmd = self._build_command()
        logger.info(f"Running FFmpeg: {' '.join(cmd[:5])}...")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            logger.info(f"FFmpeg started with PID: {self.process.pid}")

            # Start log reader
            asyncio.create_task(self._read_logs())

            # Wait for process exit
            await self.process.wait()

            # Check exit code
            if self.process.returncode != 0:
                raise FFmpegError(
                    f"FFmpeg exited with code {self.process.returncode}"
                )

            logger.info("FFmpeg completed successfully")

        except FileNotFoundError:
            raise FFmpegError(
                f"FFmpeg not found at {self.FFMPEG_PATH}. "
                "Install FFmpeg or set FFMPEG_PATH."
            )
        except Exception as e:
            raise FFmpegError(f"Failed to run FFmpeg: {str(e)}")

    async def stop(self) -> None:
        """
        Stop FFmpeg process gracefully.

        Sends SIGTERM, waits up to SHUTDOWN_TIMEOUT seconds,
        then sends SIGKILL if needed.
        """
        if not self.process:
            return

        pid = self.process.pid
        logger.info(f"Stopping FFmpeg PID {pid}...")

        try:
            # Send SIGTERM for graceful shutdown
            self.process.send_signal(signal.SIGTERM)

            # Wait for clean exit
            try:
                await asyncio.wait_for(
                    self.process.wait(),
                    timeout=self.SHUTDOWN_TIMEOUT
                )
                logger.info(f"FFmpeg {pid} shut down cleanly")

            except asyncio.TimeoutError:
                logger.warning(f"FFmpeg {pid} didn't shut down in {self.SHUTDOWN_TIMEOUT}s")
                # Force kill with SIGKILL
                self.process.kill()
                await self.process.wait()
                logger.info(f"FFmpeg {pid} killed after timeout")

        except Exception as e:
            logger.error(f"Failed to stop FFmpeg: {e}")
            raise FFmpegError(f"Failed to stop FFmpeg: {str(e)}")

    async def _read_logs(self) -> None:
        """Read FFmpeg stdout/stderr and log with [FFMPEG] prefix."""
        async def read_stream(stream, prefix=""):
            while not self._shutdown_event.is_set():
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    line_str = line.decode().strip()
                    if line_str:
                        logger.info(f"[FFMPEG]{prefix} {line_str}")
                except Exception:
                    break

        if self.process:
            if self.process.stdout:
                asyncio.create_task(read_stream(self.process.stdout, " "))
            if self.process.stderr:
                asyncio.create_task(read_stream(self.process.stderr, " ERR"))
