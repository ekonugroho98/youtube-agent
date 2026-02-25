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
from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError, BaseModel

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
from .encryption import encrypt, decrypt
from .auth import get_auth_manager, get_token_from_header

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
_youtube_monitor_task: Optional[asyncio.Task] = None
youtube_client = None  # Optional[YouTubeAPIClient]
auth_manager = get_auth_manager()


def validate_environment():
    """Validate required environment variables on startup."""
    required_vars = [
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

    # YOUTUBE_STREAM_KEY is optional (can be set from dashboard)
    if not os.getenv("YOUTUBE_STREAM_KEY"):
        logger.info("YOUTUBE_STREAM_KEY not set - can be configured from dashboard")

    # Log if dashboard PIN is set
    if os.getenv("DASHBOARD_PIN"):
        logger.info("DASHBOARD_PIN is set - dashboard requires authentication")
    else:
        logger.warning("DASHBOARD_PIN not set - dashboard is UNPROTECTED!")


# Pydantic models for auth
class LoginRequest(BaseModel):
    """Login request with PIN."""
    pin: str


class LoginResponse(BaseModel):
    """Login response with session token."""
    token: str
    expires_in: int  # seconds


def check_auth(request: Request) -> None:
    """
    Check authentication from request.

    Raises:
        HTTPException: If authentication fails
    """
    # Allow access if no PIN is set (dev mode)
    if not auth_manager.pin:
        return

    # Get token from Authorization header
    auth_header = request.headers.get("Authorization")
    token = get_token_from_header(auth_header)

    if not token or not auth_manager.validate_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token"
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

    # Initialize YouTube API client (optional)
    global youtube_client
    yt_api_key = os.getenv("YOUTUBE_API_KEY")
    if yt_api_key:
        try:
            from .youtube_api import YouTubeAPIClient
            yt_channel_id = os.getenv("YOUTUBE_CHANNEL_ID")
            youtube_client = YouTubeAPIClient(api_key=yt_api_key, channel_id=yt_channel_id)
            logger.info(f"YouTube API client initialized (channel: {yt_channel_id or 'not set'})")
        except Exception as e:
            logger.warning(f"Failed to initialize YouTube API client: {e}")
            youtube_client = None
    else:
        logger.info("YouTube API not configured (YOUTUBE_API_KEY not set)")

    # Start daily schedule task (start/stop stream by schedule)
    global _schedule_task
    _schedule_task = asyncio.create_task(_schedule_loop())

    # Start YouTube monitor task (if enabled)
    global _youtube_monitor_task
    _youtube_monitor_task = asyncio.create_task(_youtube_monitor_loop())


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


async def _youtube_monitor_loop() -> None:
    """
    Background task: poll YouTube API for live stream status, viewer count, etc.
    Only runs when youtube_api_enabled is True in config and youtube_client is available.
    """
    while True:
        try:
            await asyncio.sleep(3)  # Short initial delay before first poll
            if not persistence or not youtube_client:
                await asyncio.sleep(30)
                continue

            config = persistence.load_config_optional()
            if not config or not config.youtube_api_enabled:
                await asyncio.sleep(30)
                continue

            # Update channel_id if changed in config
            if config.youtube_channel_id and config.youtube_channel_id != youtube_client.channel_id:
                youtube_client.channel_id = config.youtube_channel_id

            if not youtube_client.channel_id:
                await asyncio.sleep(60)
                continue

            state = persistence.load_state()

            try:
                live_status = await youtube_client.get_live_status()

                state.youtube_is_live = live_status.get('is_live', False)
                state.youtube_video_id = live_status.get('video_id')
                state.youtube_concurrent_viewers = live_status.get('concurrent_viewers')
                state.youtube_view_count = live_status.get('view_count')
                state.youtube_like_count = live_status.get('like_count')
                state.youtube_stream_title = live_status.get('title')
                state.youtube_last_poll = datetime.now().isoformat()

                persistence.save_state(state)

                if live_status.get('is_live'):
                    viewers = live_status.get('concurrent_viewers', '?')
                    logger.debug(f"YouTube live: {viewers} viewers")

            except Exception as e:
                logger.warning(f"YouTube API poll error: {e}")
                # Don't update state on error, keep last known values

            # Wait for configured interval
            interval = config.youtube_monitor_interval if config else 30
            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"YouTube monitor loop error: {e}")
            await asyncio.sleep(30)


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    global _schedule_task, _youtube_monitor_task
    if _schedule_task:
        _schedule_task.cancel()
        try:
            await _schedule_task
        except asyncio.CancelledError:
            pass
    if _youtube_monitor_task:
        _youtube_monitor_task.cancel()
        try:
            await _youtube_monitor_task
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
    request: Request,
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
    # Check authentication
    check_auth(request)

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


@app.post("/auth/login")
async def login(request: LoginRequest):
    """
    Authenticate with PIN and get session token.

    Args:
        request: Login request with PIN

    Returns:
        Session token with expiry time

    Raises:
        401: Invalid PIN
    """
    pin = request.pin

    if not auth_manager.validate_pin(pin):
        logger.warning(f"Failed login attempt with invalid PIN")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PIN"
        )

    # Create session token
    token = auth_manager.create_token()

    logger.info("Successful login via dashboard")

    return LoginResponse(
        token=token,
        expires_in=24 * 60 * 60  # 24 hours in seconds
    )


@app.post("/auth/logout")
async def logout(request: Request):
    """
    Logout and revoke session token.

    Args:
        request: Request with Authorization header

    Returns:
        Success message
    """
    # Get token from header
    auth_header = request.headers.get("Authorization")
    token = get_token_from_header(auth_header)

    if token:
        auth_manager.revoke_token(token)
        logger.info("User logged out")

    return {"message": "Logged out successfully"}


@app.get("/auth/status")
async def auth_status():
    """
    Check if PIN authentication is required.

    Returns:
        JSON with pin_required flag
    """
    return {
        "pin_required": bool(auth_manager.pin)
    }


@app.post("/streams/start")
async def start_stream(request: Request):
    """
    Start the stream worker.

    Launches worker process with current configuration.

    Returns:
        200: Worker started successfully
        409: Worker already running
        500: Failed to start worker
    """
    # Check authentication
    check_auth(request)

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
                "always_on": existing.always_on,
                "keepalive_interval": existing.keepalive_interval,
                "youtube_api_enabled": existing.youtube_api_enabled,
                "youtube_channel_id": existing.youtube_channel_id,
                "youtube_monitor_interval": existing.youtube_monitor_interval,
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
            "always_on": False,
            "keepalive_interval": 300,
            "youtube_api_enabled": False,
            "youtube_channel_id": None,
            "youtube_monitor_interval": 30,
        }
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to get config", "message": str(e)}
        )


@app.post("/streams/config")
async def update_stream_config(
    request: Request,
    media_key: Optional[str] = Form(None),
    playlist: Optional[str] = Form(None),
    youtube_rtmp_url: Optional[str] = Form(None),
    youtube_stream_key: Optional[str] = Form(None),
    loop_streaming: Optional[bool] = Form(None),
    loop_delay: Optional[int] = Form(None),
    schedule_enabled: Optional[bool] = Form(None),
    schedule_start_time: Optional[str] = Form(None),
    schedule_duration_hours: Optional[float] = Form(None),
    always_on: Optional[bool] = Form(None),
    keepalive_interval: Optional[int] = Form(None),
):
    """
    Update stream configuration (file/playlist, loop, daily schedule, stream key, 24/7 mode).

    Args:
        media_key: Single media file key (e.g., "smaller.mp4")
        playlist: Comma-separated list of media keys
        youtube_rtmp_url: RTMP URL (optional)
        youtube_stream_key: YouTube stream key (optional, encrypted before saving)
        loop_streaming: Enable auto loop when video ends (optional)
        loop_delay: Seconds between loops 1-300 (optional)
        schedule_enabled: Enable daily schedule: start at same time, run N hours (optional)
        schedule_start_time: Daily start time HH:MM 24h (optional, e.g. 09:00)
        schedule_duration_hours: Hours to run each day 0.5-24 (optional, e.g. 8)
        always_on: Enable 24/7 mode - auto-restart on crash/error (optional)
        keepalive_interval: Keepalive check interval in seconds 60-3600 (optional, default 300)

    Returns:
        200: Config updated successfully
    """
    # Check authentication
    check_auth(request)

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
            # No media_key, playlist, or existing config - error
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

        always_on_val = always_on if always_on is not None else (existing.always_on if existing else False)
        keepalive_interval_val = keepalive_interval if keepalive_interval is not None else (existing.keepalive_interval if existing else 300)
        if keepalive_interval_val is not None and (keepalive_interval_val < 60 or keepalive_interval_val > 3600):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "keepalive_interval must be between 60 and 3600"}
            )

        # Handle stream key encryption
        stream_key_encrypted = None
        if youtube_stream_key:
            # New stream key provided - encrypt it
            stream_key_encrypted = encrypt(youtube_stream_key)
            logger.info("New stream key encrypted and saved")
        elif existing and existing.youtube_stream_key_encrypted:
            # Keep existing encrypted stream key
            stream_key_encrypted = existing.youtube_stream_key_encrypted

        config = StreamConfig(
            youtube_rtmp_url=rtmp_url,
            youtube_stream_key_encrypted=stream_key_encrypted,
            media_key=media_key,
            playlist=playlist_list,
            loop_streaming=loop_streaming_val,
            loop_delay=loop_delay_val,
            schedule_enabled=schedule_enabled_val,
            schedule_start_time=schedule_start_val,
            schedule_duration_hours=schedule_duration_val,
            always_on=always_on_val,
            keepalive_interval=keepalive_interval_val,
        )

        persistence.save_config(config)

        mode = "playlist" if playlist_list else "single"
        logger.info(f"Stream config updated: mode={mode}, media_key={media_key}, schedule_enabled={schedule_enabled_val}, always_on={always_on_val}")

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
            "always_on": config.always_on,
            "keepalive_interval": config.keepalive_interval,
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
async def stop_stream(request: Request):
    """
    Stop the stream worker.

    Terminates worker process gracefully.

    Returns:
        200: Worker stopped successfully
        404: No worker running
    """
    # Check authentication
    check_auth(request)

    try:
        state = persistence.load_state()

        # Can stop if status is one of the active states
        active_statuses = (StreamStatus.RUNNING, StreamStatus.STARTING, StreamStatus.STREAMING)
        if state.status not in active_statuses:
            logger.info(f"Attempted to stop stream while not active (status: {state.status})")
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
            always_on=config.always_on,
            always_on_restart_count=state.always_on_restart_count,
            # YouTube API monitoring
            youtube_api_enabled=config.youtube_api_enabled,
            youtube_is_live=state.youtube_is_live,
            youtube_video_id=state.youtube_video_id,
            youtube_concurrent_viewers=state.youtube_concurrent_viewers,
            youtube_view_count=state.youtube_view_count,
            youtube_like_count=state.youtube_like_count,
            youtube_stream_title=state.youtube_stream_title,
        )

    except (ConfigNotFoundError, InvalidConfigError) as e:
        logger.error(f"Config error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Configuration error", "message": str(e)}
        )


# ==================== YouTube API Endpoints ====================

@app.get("/youtube/status")
async def youtube_live_status():
    """
    Get YouTube live stream status from API.

    Returns real-time data: is_live, viewer count, likes, video title.
    Falls back to cached state if API call fails.
    """
    if not youtube_client:
        return {
            "enabled": False,
            "error": "YouTube API not configured (set YOUTUBE_API_KEY)"
        }

    config = persistence.load_config_optional()
    if not config or not config.youtube_api_enabled:
        return {
            "enabled": False,
            "error": "YouTube API monitoring is disabled"
        }

    # Return cached state from background monitor
    state = persistence.load_state()

    # If no poll yet, do a live API call now
    if not state.youtube_last_poll and youtube_client.channel_id:
        try:
            live_status = await youtube_client.get_live_status()
            state.youtube_is_live = live_status.get('is_live', False)
            state.youtube_video_id = live_status.get('video_id')
            state.youtube_concurrent_viewers = live_status.get('concurrent_viewers')
            state.youtube_view_count = live_status.get('view_count')
            state.youtube_like_count = live_status.get('like_count')
            state.youtube_stream_title = live_status.get('title')
            state.youtube_last_poll = datetime.now().isoformat()
            persistence.save_state(state)
        except Exception as e:
            logger.warning(f"YouTube live API call failed: {e}")

    return {
        "enabled": True,
        "is_live": state.youtube_is_live,
        "video_id": state.youtube_video_id,
        "concurrent_viewers": state.youtube_concurrent_viewers,
        "view_count": state.youtube_view_count,
        "like_count": state.youtube_like_count,
        "stream_title": state.youtube_stream_title,
        "last_poll": state.youtube_last_poll,
    }


@app.post("/youtube/config")
async def update_youtube_config(
    request: Request,
    youtube_api_enabled: Optional[bool] = Form(None),
    youtube_channel_id: Optional[str] = Form(None),
    youtube_monitor_interval: Optional[int] = Form(None),
):
    """
    Update YouTube API configuration.

    Args:
        youtube_api_enabled: Enable/disable YouTube API monitoring
        youtube_channel_id: YouTube channel ID
        youtube_monitor_interval: Polling interval in seconds (10-300)
    """
    check_auth(request)

    try:
        existing = persistence.load_config_optional()
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "No stream config exists yet. Set stream config first."}
            )

        if youtube_api_enabled is not None:
            existing.youtube_api_enabled = youtube_api_enabled
        if youtube_channel_id is not None:
            existing.youtube_channel_id = youtube_channel_id.strip() or None
        if youtube_monitor_interval is not None:
            if youtube_monitor_interval < 10 or youtube_monitor_interval > 300:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "youtube_monitor_interval must be between 10 and 300"}
                )
            existing.youtube_monitor_interval = youtube_monitor_interval

        persistence.save_config(existing)

        # Update youtube_client channel_id if changed
        if youtube_client and existing.youtube_channel_id:
            youtube_client.channel_id = existing.youtube_channel_id

        logger.info(f"YouTube API config updated: enabled={existing.youtube_api_enabled}, channel={existing.youtube_channel_id}")

        return {
            "status": "youtube_config_updated",
            "youtube_api_enabled": existing.youtube_api_enabled,
            "youtube_channel_id": existing.youtube_channel_id,
            "youtube_monitor_interval": existing.youtube_monitor_interval,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update YouTube config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to update YouTube config", "message": str(e)}
        )


@app.get("/youtube/validate")
async def validate_youtube_setup(request: Request):
    """
    Validate YouTube API key and channel ID.

    Returns validation results for troubleshooting.
    """
    check_auth(request)

    if not youtube_client:
        return {
            "api_key_valid": False,
            "error": "YouTube API not configured (set YOUTUBE_API_KEY)"
        }

    result = {"api_key_valid": False, "channel_valid": False, "channel_info": None}

    # Validate API key
    try:
        result["api_key_valid"] = await youtube_client.validate_api_key()
    except Exception as e:
        result["api_key_error"] = str(e)

    # Validate channel ID
    config = persistence.load_config_optional()
    channel_id = config.youtube_channel_id if config else None
    if channel_id and result["api_key_valid"]:
        try:
            channel_info = await youtube_client.validate_channel_id(channel_id)
            if channel_info:
                result["channel_valid"] = True
                result["channel_info"] = channel_info
            else:
                result["channel_error"] = "Channel not found"
        except Exception as e:
            result["channel_error"] = str(e)

    return result
