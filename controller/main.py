"""
Stream controller FastAPI application.

Provides HTTP API for managing live stream worker processes.
"""
import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from .models import (
    StreamStatus,
    StreamConfig,
    StreamState,
    HealthResponse,
    StreamStatusResponse,
)
from .persistence import StreamPersistence, ConfigNotFoundError, InvalidConfigError
from .worker_manager import WorkerManager, WorkerManagerError


# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Stream Controller",
    description="API for managing YouTube live stream workers",
    version="1.0.0",
)

# CORS middleware (allow all origins for MVP - restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global components
persistence: Optional[StreamPersistence] = None
worker_manager: Optional[WorkerManager] = None


def validate_environment():
    """Validate required environment variables on startup."""
    required_vars = [
        "YOUTUBE_RTMP_URL",
        "YOUTUBE_STREAM_KEY",
        "STORAGE_BUCKET",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
    ]

    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


@app.on_event("startup")
async def startup_event():
    """Initialize controller on startup."""
    global persistence, worker_manager

    logger.info("Starting stream controller...")

    # Validate environment
    validate_environment()

    # Initialize persistence
    try:
        persistence = StreamPersistence()
        logger.info("Persistence initialized")
    except Exception as e:
        logger.error(f"Failed to initialize persistence: {e}")
        raise

    # Initialize worker manager
    try:
        worker_manager = WorkerManager(persistence)
        logger.info("Worker manager initialized")

        # Clean up orphaned workers from previous run
        await worker_manager.cleanup_orphans()

    except Exception as e:
        logger.error(f"Failed to initialize worker manager: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    logger.info("Shutting down stream controller...")
    if worker_manager:
        await worker_manager.shutdown()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.

    Returns 200 if controller is healthy, 503 if config directory is inaccessible.
    """
    try:
        # Check if persistence is accessible
        if persistence:
            _ = persistence.config_dir.exists()

        return HealthResponse(status="healthy", timestamp=datetime.now().isoformat())

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Controller unhealthy", "message": str(e)}
        )


@app.post("/streams/start")
async def start_stream():
    """
    Start the stream worker.

    Launches worker process with current configuration.

    Returns:
        200: Worker started successfully
        409: Worker already running
        500: Failed to start worker
    """
    try:
        # Load config
        config = persistence.load_config()

        # Check if already running
        state = persistence.load_state()
        if state.status == StreamStatus.RUNNING:
            logger.warning("Attempted to start stream while already running")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "Stream already running",
                    "current_status": state.status,
                    "worker_pid": state.worker_pid,
                }
            )

        # Start worker
        await worker_manager.start_worker(config)

        # Get updated state
        new_state = persistence.load_state()
        return {
            "status": new_state.status,
            "worker_pid": new_state.worker_pid,
            "started_at": new_state.started_at,
        }

    except (ConfigNotFoundError, InvalidConfigError) as e:
        logger.error(f"Config error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Invalid configuration", "message": str(e)}
        )
    except WorkerManagerError as e:
        logger.error(f"Failed to start worker: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to start worker", "message": str(e)}
        )


@app.post("/streams/config")
async def update_stream_config(
    media_key: Optional[str] = None,
    playlist: Optional[str] = None,
    youtube_rtmp_url: Optional[str] = None
):
    """
    Update stream configuration (which file/playlist to stream).

    Args:
        media_key: Single media file key (e.g., "smaller.mp4")
        playlist: Comma-separated list of media keys (e.g., "surah_1.mp4,surah_2.mp4,surah_3.mp4")
        youtube_rtmp_url: RTMP URL (optional, uses env default if not provided)

    Returns:
        200: Config updated successfully
    """
    try:
        # Use provided RTMP URL or get from environment
        rtmp_url = youtube_rtmp_url or os.getenv("YOUTUBE_RTMP_URL")

        if not rtmp_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "RTMP URL not provided and YOUTUBE_RTMP_URL not set"}
            )

        # Parse playlist if provided
        playlist_list = None
        if playlist:
            playlist_list = [p.strip() for p in playlist.split(",")]
            if not media_key:
                media_key = playlist_list[0]  # Use first track as default
            logger.info(f"Playlist mode: {len(playlist_list)} files")
        elif not media_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "Either media_key or playlist must be provided"}
            )

        # Create config
        config = StreamConfig(
            youtube_rtmp_url=rtmp_url,
            media_key=media_key,
            playlist=playlist_list
        )

        # Save config
        persistence.save_config(config)

        mode = "playlist" if playlist_list else "single"
        logger.info(f"Stream config updated: mode={mode}, media_key={media_key}")

        return {
            "status": "config_updated",
            "mode": mode,
            "media_key": config.media_key,
            "playlist": config.playlist,
            "youtube_rtmp_url": config.youtube_rtmp_url
        }

    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to update config", "message": str(e)}
        )


@app.post("/streams/stop")
async def stop_stream():
    """
    Stop the stream worker.

    Terminates worker process gracefully.

    Returns:
        200: Worker stopped successfully
        404: No worker running
    """
    try:
        state = persistence.load_state()

        if state.status != StreamStatus.RUNNING:
            logger.info("Attempted to stop stream while not running")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "No worker running",
                    "current_status": state.status,
                }
            )

        await worker_manager.stop_worker()

        new_state = persistence.load_state()
        return {
            "status": new_state.status,
            "stopped_at": new_state.exited_at,
        }

    except WorkerManagerError as e:
        logger.error(f"Failed to stop worker: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to stop worker", "message": str(e)}
        )


@app.get("/streams/status", response_model=StreamStatusResponse)
async def get_stream_status():
    """
    Get current stream status.

    Returns:
        200: Stream status (running/stopped/error)
    """
    try:
        state = persistence.load_state()
        config = persistence.load_config()

        return StreamStatusResponse(
            status=state.status,
            worker_pid=state.worker_pid,
            started_at=state.started_at,
            uptime_seconds=state.uptime_seconds,
            last_health_check=state.last_health_check,
            exited_at=state.exited_at,
            exit_code=state.exit_code,
            error_message=state.error_message,
            media_key=state.media_key,
            rtmp_url=config.youtube_rtmp_url,  # RTMP URL WITHOUT stream key
        )

    except (ConfigNotFoundError, InvalidConfigError) as e:
        logger.error(f"Config error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Configuration error", "message": str(e)}
        )
