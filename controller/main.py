"""
Stream controller FastAPI application.

Provides HTTP API for managing live stream worker processes.
"""
import asyncio
import os
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

# Load environment variables from .env file
load_dotenv()

from .models import (
    StreamStatus,
    StreamConfig,
    StreamState,
    HealthResponse,
    StreamStatusResponse,
)
from .persistence import StreamPersistence, ConfigNotFoundError, InvalidConfigError
from .worker_manager import WorkerManager, WorkerManagerError

# Import storage client for file operations
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from storage.client import StorageClient


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

# Mount static files for dashboard
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Global components
persistence: Optional[StreamPersistence] = None
worker_manager: Optional[WorkerManager] = None
storage_client: Optional[StorageClient] = None
_schedule_task: Optional[asyncio.Task] = None


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
    global persistence, worker_manager, storage_client

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

    # Initialize storage client
    try:
        storage_client = StorageClient()
        logger.info("Storage client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize storage client: {e}")
        raise

    # Start daily schedule task (start/stop stream by schedule)
    global _schedule_task
    _schedule_task = asyncio.create_task(_schedule_loop())


async def _schedule_loop() -> None:
    """
    Background task: every minute, apply daily schedule (auto start at start_time, stop after duration_hours).
    Uses local timezone for schedule_start_time.
    """
    while True:
        try:
            await asyncio.sleep(60)  # run every 60 seconds
            if not persistence or not worker_manager:
                continue
            config = persistence.load_config_optional()
            if not config or not config.schedule_enabled or not config.effective_media_key:
                continue
            state = persistence.load_state()
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            start_h, start_m = 9, 0
            try:
                parts = config.schedule_start_time.strip().split(":")
                if len(parts) >= 2:
                    start_h, start_m = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass
            now_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            duration_seconds = config.schedule_duration_hours * 3600

            # Should we start? (not running, past start time, not already started today)
            if state.status != StreamStatus.RUNNING:
                if now_minutes >= start_minutes and (
                    state.last_scheduled_start_date is None or state.last_scheduled_start_date < today
                ):
                    try:
                        await worker_manager.start_worker(config)
                        logger.info(f"Schedule: started stream at {now.isoformat()} (daily {config.schedule_start_time}, {config.schedule_duration_hours}h)")
                    except WorkerManagerError as e:
                        logger.warning(f"Schedule start failed: {e}")
                continue

            # Stream is running: stop after duration
            if state.started_at:
                try:
                    started = datetime.fromisoformat(state.started_at)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= duration_seconds:
                        await worker_manager.stop_worker()
                        logger.info(f"Schedule: stopped stream after {config.schedule_duration_hours}h (started {state.started_at})")
                except (ValueError, TypeError):
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Schedule loop error: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    global _schedule_task
    if _schedule_task:
        _schedule_task.cancel()
        try:
            await _schedule_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutting down stream controller...")
    if worker_manager:
        await worker_manager.shutdown()


@app.get("/")
async def dashboard():
    """Serve dashboard HTML."""
    static_file = static_dir / "index.html"
    if static_file.exists():
        return FileResponse(str(static_file))
    raise HTTPException(status_code=404, detail="Dashboard not found")


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    object_key: Optional[str] = Form(None)
):
    """
    Upload file to storage.

    Args:
        file: File to upload
        object_key: Custom object key (optional, uses filename if not provided)

    Returns:
        Uploaded file metadata
    """
    if not storage_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Storage client not initialized"}
        )

    try:
        # Save uploaded file to temporary location
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename}") as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name

        # Upload to storage
        result = storage_client.upload_file(
            file_path=tmp_file_path,
            object_key=object_key
        )

        # Clean up temp file
        import os
        os.unlink(tmp_file_path)

        logger.info(f"File uploaded: {result.key} ({result.size} bytes)")

        return {
            "key": result.key,
            "size": result.size,
            "last_modified": result.last_modified
        }

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Upload failed", "message": str(e)}
        )


@app.get("/storage/files")
async def list_storage_files():
    """
    List all media files in storage.

    Returns:
        List of media files with metadata
    """
    if not storage_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Storage client not initialized"}
        )

    try:
        files = storage_client.list_media()
        return {
            "files": [
                {
                    "key": f.key,
                    "size": f.size,
                    "last_modified": f.last_modified
                }
                for f in files
            ]
        }
    except Exception as e:
        logger.error(f"Failed to list files: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to list files", "message": str(e)}
        )


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


def _default_rtmp_url() -> str:
    """Return RTMP URL from env or standard YouTube URL."""
    return os.getenv("YOUTUBE_RTMP_URL") or "rtmp://a.rtmp.youtube.com/live2"


@app.get("/streams/config")
async def get_stream_config():
    """
    Get current stream configuration (for dashboard).

    Returns saved config or defaults when no config file exists.
    """
    try:
        existing = persistence.load_config_optional()
        if existing:
            return {
                "media_key": existing.media_key,
                "playlist": existing.playlist,
                "youtube_rtmp_url": existing.youtube_rtmp_url,
                "loop_streaming": existing.loop_streaming,
                "loop_delay": existing.loop_delay,
                "schedule_enabled": existing.schedule_enabled,
                "schedule_start_time": existing.schedule_start_time,
                "schedule_duration_hours": existing.schedule_duration_hours,
            }
        return {
            "media_key": None,
            "playlist": None,
            "youtube_rtmp_url": _default_rtmp_url(),
            "loop_streaming": False,
            "loop_delay": 5,
            "schedule_enabled": False,
            "schedule_start_time": "09:00",
            "schedule_duration_hours": 8,
        }
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to get config", "message": str(e)}
        )


@app.post("/streams/config")
async def update_stream_config(
    media_key: Optional[str] = None,
    playlist: Optional[str] = None,
    youtube_rtmp_url: Optional[str] = None,
    loop_streaming: Optional[bool] = None,
    loop_delay: Optional[int] = None,
    schedule_enabled: Optional[bool] = None,
    schedule_start_time: Optional[str] = None,
    schedule_duration_hours: Optional[float] = None,
):
    """
    Update stream configuration (file/playlist, loop, daily schedule).

    Args:
        media_key: Single media file key (e.g., "smaller.mp4")
        playlist: Comma-separated list of media keys
        youtube_rtmp_url: RTMP URL (optional)
        loop_streaming: Enable auto loop when video ends (optional)
        loop_delay: Seconds between loops 1-300 (optional)
        schedule_enabled: Enable daily schedule: start at same time, run N hours (optional)
        schedule_start_time: Daily start time HH:MM 24h (optional, e.g. 09:00)
        schedule_duration_hours: Hours to run each day 0.5-24 (optional, e.g. 8)

    Returns:
        200: Config updated successfully
    """
    try:
        existing = persistence.load_config_optional()

        rtmp_url = youtube_rtmp_url or (existing.youtube_rtmp_url if existing else None) or _default_rtmp_url()
        if not rtmp_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "RTMP URL not provided and YOUTUBE_RTMP_URL not set"}
            )

        # Resolve media_key and playlist from request or existing
        playlist_list = None
        if playlist:
            playlist_list = [p.strip() for p in playlist.split(",")]
            media_key = media_key or (playlist_list[0] if playlist_list else None)
            logger.info(f"Playlist mode: {len(playlist_list)} files")
        elif media_key:
            pass  # single file
        elif existing:
            media_key = existing.media_key
            playlist_list = existing.playlist
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "Either media_key or playlist must be provided when no config exists"}
            )

        loop_streaming_val = loop_streaming if loop_streaming is not None else (existing.loop_streaming if existing else False)
        loop_delay_val = loop_delay if loop_delay is not None else (existing.loop_delay if existing else 5)
        if loop_delay_val is not None and (loop_delay_val < 1 or loop_delay_val > 300):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "loop_delay must be between 1 and 300"}
            )

        schedule_enabled_val = schedule_enabled if schedule_enabled is not None else (existing.schedule_enabled if existing else False)
        schedule_start_val = schedule_start_time if schedule_start_time is not None else (existing.schedule_start_time if existing else "09:00")
        schedule_duration_val = schedule_duration_hours if schedule_duration_hours is not None else (existing.schedule_duration_hours if existing else 8)
        if schedule_duration_val is not None and (schedule_duration_val < 0.5 or schedule_duration_val > 24):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "schedule_duration_hours must be between 0.5 and 24"}
            )

        config = StreamConfig(
            youtube_rtmp_url=rtmp_url,
            media_key=media_key,
            playlist=playlist_list,
            loop_streaming=loop_streaming_val,
            loop_delay=loop_delay_val,
            schedule_enabled=schedule_enabled_val,
            schedule_start_time=schedule_start_val,
            schedule_duration_hours=schedule_duration_val,
        )

        persistence.save_config(config)

        mode = "playlist" if playlist_list else "single"
        logger.info(f"Stream config updated: mode={mode}, media_key={media_key}, schedule_enabled={schedule_enabled_val}")

        return {
            "status": "config_updated",
            "mode": mode,
            "media_key": config.media_key,
            "playlist": config.playlist,
            "youtube_rtmp_url": config.youtube_rtmp_url,
            "loop_streaming": config.loop_streaming,
            "loop_delay": config.loop_delay,
            "schedule_enabled": config.schedule_enabled,
            "schedule_start_time": config.schedule_start_time,
            "schedule_duration_hours": config.schedule_duration_hours,
        }

    except HTTPException:
        raise
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
