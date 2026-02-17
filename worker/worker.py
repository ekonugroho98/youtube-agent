"""
Stream worker - manages FFmpeg subprocess for streaming to YouTube.

Handles media streaming from object storage, FFmpeg process management,
and failure recovery with exponential backoff.
"""
import os
import sys
import signal
import asyncio
import logging
import argparse
from datetime import datetime
from typing import Optional

from storage import StorageClient, StorageConnectionError
from .ffmpeg import FFmpegRunner, FFmpegError


# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class WorkerError(Exception):
    """Worker error."""
    pass


class StreamWorker:
    """
    Stream worker manages FFmpeg subprocess for YouTube live streaming.

    Handles:
    - Media URL retrieval from storage
    - FFmpeg subprocess spawning and monitoring
    - Failure recovery with exponential backoff
    - Graceful shutdown
    - Loop streaming (restart when video ends)
    """

    # Retry configuration
    MAX_RETRIES = int(os.getenv("WORKER_MAX_RETRIES", "3"))
    INITIAL_RETRY_DELAY = int(os.getenv("WORKER_RETRY_DELAY", "30"))

    # Backoff sequence (seconds): 30 -> 60 -> 120 (capped)
    BACKOFF_SEQUENCE = [30, 60, 120]

    # Loop streaming configuration
    LOOP_STREAMING = os.getenv("LOOP_STREAMING", "false").lower() == "true"
    LOOP_DELAY = int(os.getenv("LOOP_DELAY", "5"))  # seconds between loops

    def __init__(self, media_key: str, rtmp_url: str):
        """
        Initialize stream worker.

        Args:
            media_key: Media file key in object storage
            rtmp_url: YouTube RTMP URL (without stream key)
        """
        self.media_key = media_key
        self.rtmp_url = rtmp_url

        # Load stream key from environment
        self.stream_key = os.getenv("YOUTUBE_STREAM_KEY")
        if not self.stream_key:
            raise WorkerError("YOUTUBE_STREAM_KEY environment variable not set")

        # Full RTMP URL with stream key
        self.rtmp_destination = f"{rtmp_url}/{self.stream_key}"

        # Initialize components
        self.storage = StorageClient()
        self.ffmpeg: Optional[FFmpegRunner] = None

        # State
        self._shutdown_event = asyncio.Event()
        self._retry_count = 0
        self._loop_count = 0

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._shutdown_event.set()
        if self.ffmpeg:
            asyncio.create_task(self.ffmpeg.stop())

    async def run(self) -> int:
        """
        Run worker with retry logic and optional looping.

        Returns:
            Exit code (0 for success, non-zero for failure)

        Raises:
            WorkerError: Fatal error preventing retries
        """
        logger.info(f"Starting worker for media: {self.media_key}")
        if self.LOOP_STREAMING:
            logger.info(f"Loop streaming ENABLED (delay: {self.LOOP_DELAY}s)")

        while self._retry_count < self.MAX_RETRIES:
            try:
                await self._stream_media()

                # Stream completed successfully
                self._loop_count += 1
                logger.info(f"Stream completed successfully (loop #{self._loop_count})")

                # Check if we should loop
                if self.LOOP_STREAMING:
                    # Reset retry counter on successful loop
                    self._retry_count = 0

                    # Check for shutdown signal before next loop
                    try:
                        logger.info(f"Restarting in {self.LOOP_DELAY}s...")
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=self.LOOP_DELAY
                        )
                        # Shutdown signal received
                        logger.info("Shutdown signal received during loop delay")
                        return 0
                    except asyncio.TimeoutError:
                        # Delay complete, continue to next loop
                        logger.info(f"Starting loop #{self._loop_count + 1}...")
                        continue
                else:
                    # No looping, exit successfully
                    return 0

            except FFmpegError as e:
                logger.error(f"FFmpeg error: {e}")

                self._retry_count += 1

                if self._retry_count >= self.MAX_RETRIES:
                    logger.error(f"Max retries ({self.MAX_RETRIES}) exceeded")
                    return 1

                # Calculate backoff delay
                delay = self.BACKOFF_SEQUENCE[
                    min(self._retry_count - 1, len(self.BACKOFF_SEQUENCE) - 1)
                ]

                logger.info(f"Retrying in {delay}s (attempt {self._retry_count}/{self.MAX_RETRIES})")
                self._log_structured_error(
                    error_type="ffmpeg",
                    error_message=str(e),
                    retry_count=self._retry_count,
                    will_retry=True
                )

                # Wait for delay or shutdown signal
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=delay
                    )
                    # Shutdown signal received
                    logger.info("Shutdown during backoff, exiting")
                    return 0
                except asyncio.TimeoutError:
                    # Backoff complete, retry
                    pass

            except StorageConnectionError as e:
                logger.error(f"Storage error: {e}")
                self._log_structured_error(
                    error_type="storage",
                    error_message=str(e),
                    retry_count=self._retry_count,
                    will_retry=self._retry_count < self.MAX_RETRIES
                )
                return 1

        return 1

    async def _stream_media(self) -> None:
        """
        Stream media from storage to YouTube.

        Raises:
            StorageConnectionError: Failed to get media URL
            FFmpegError: FFmpeg streaming failed
        """
        logger.info(f"Fetching stream URL for: {self.media_key}")

        # Get signed URL from storage
        media_url = self.storage.get_stream_url(self.media_key)
        logger.info(f"Media URL: {media_url[:50]}...")

        # Detect if MP4 for codec copy
        is_mp4 = self.media_key.lower().endswith('.mp4')

        # Start FFmpeg
        self.ffmpeg = FFmpegRunner(
            input_url=media_url,
            rtmp_url=self.rtmp_destination,
            codec_copy=is_mp4
        )

        logger.info(f"Starting FFmpeg (codec_copy={is_mp4})")
        await self.ffmpeg.run()

    def _log_structured_error(self, error_type: str, error_message: str,
                           retry_count: int, will_retry: bool) -> None:
        """Log structured error for parsing."""
        logger.error(
            f"ERROR: type={error_type}, "
            f"message={error_message}, "
            f"retry_count={retry_count}, "
            f"will_retry={will_retry}"
        )


async def main():
    """Main entrypoint."""
    parser = argparse.ArgumentParser(description="YouTube stream worker")
    parser.add_argument("--media-key", required=True, help="Media file key in storage")
    parser.add_argument("--rtmp-url", required=True, help="YouTube RTMP URL")

    args = parser.parse_args()

    try:
        worker = StreamWorker(media_key=args.media_key, rtmp_url=args.rtmp_url)
        exit_code = await worker.run()
        sys.exit(exit_code)

    except WorkerError as e:
        logger.error(f"Worker error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
