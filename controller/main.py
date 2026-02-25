"""
Stream controller FastAPI application.

Provides HTTP API for managing multiple live stream profiles (multi-account).
Each profile has its own storage bucket, stream key, YouTube API key, and worker.
"""
import asyncio
import os
import logging
import tempfile
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Load environment variables from .env file
load_dotenv()

from .models import (
    StreamStatus,
    StreamConfig,
    StreamState,
    StreamProfile,
    ProfileSummary,
    HealthResponse,
    StreamStatusResponse,
)
from .persistence import (
    StreamPersistence,
    ProfileRegistry,
    ConfigNotFoundError,
    InvalidConfigError,
    PersistenceError,
)
from .worker_manager import WorkerManager, WorkerManagerError
from .encryption import encrypt, decrypt
from .auth import get_auth_manager, get_token_from_header
try:
    from .youtube_api import YouTubeAPIClient
except ImportError:
    YouTubeAPIClient = None
    logging.getLogger(__name__).warning("youtube_api module not available (google-api-python-client not installed)")

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
    description="API for managing YouTube live stream workers (multi-profile)",
    version="2.0.0",
)

# CORS middleware
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


# ==================== Profile Runtime ====================

@dataclass
class ProfileRuntime:
    """Runtime state for a single stream profile."""
    profile: StreamProfile
    persistence: StreamPersistence
    worker_manager: WorkerManager
    storage_client: Optional[StorageClient] = None
    youtube_client: Optional[YouTubeAPIClient] = None
    schedule_task: Optional[asyncio.Task] = None


# Global state
profile_registry: Optional[ProfileRegistry] = None
profiles: Dict[str, ProfileRuntime] = {}
auth_manager = get_auth_manager()


def _default_rtmp_url() -> str:
    """Return RTMP URL from env or standard YouTube URL."""
    return os.getenv("YOUTUBE_RTMP_URL") or "rtmp://a.rtmp.youtube.com/live2"


def check_auth(request: Request) -> None:
    """Check authentication from request."""
    if not auth_manager.pin:
        return
    auth_header = request.headers.get("Authorization")
    token = get_token_from_header(auth_header)
    if not token or not auth_manager.validate_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token"
        )


def _get_profile_runtime(profile_id: str) -> ProfileRuntime:
    """Get a profile runtime or raise 404."""
    rt = profiles.get(profile_id)
    if not rt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Profile '{profile_id}' not found"}
        )
    return rt


def _get_default_profile_id() -> str:
    """Get the default (first) profile ID for legacy endpoints."""
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "No profiles configured. Create a profile first."}
        )
    return next(iter(profiles))


# ==================== Profile Initialization ====================

async def _init_profile_runtime(profile: StreamProfile) -> ProfileRuntime:
    """Initialize runtime components for a profile."""
    logger.info(f"Initializing profile: {profile.id} ({profile.name})")

    # Persistence (per-profile directory)
    persistence = profile_registry.get_profile_persistence(profile.id)

    # Worker manager
    wm = WorkerManager(persistence)
    await wm.cleanup_orphans()

    # Storage client
    sc = None
    try:
        secret_key = decrypt(profile.storage_secret_access_key_encrypted)
        sc = StorageClient.from_config(
            bucket=profile.storage_bucket,
            access_key=profile.storage_access_key_id,
            secret_key=secret_key,
            endpoint=profile.storage_endpoint,
            provider=profile.storage_provider,
            region=profile.storage_region,
        )
        logger.info(f"  Storage: {profile.storage_provider}/{profile.storage_bucket}")
    except Exception as e:
        logger.warning(f"  Storage init failed for profile {profile.id}: {e}")

    # YouTube API client
    yt = None
    if profile.youtube_api_key_encrypted and YouTubeAPIClient:
        try:
            api_key = decrypt(profile.youtube_api_key_encrypted)
            config = persistence.load_config_optional()
            channel_id = config.youtube_channel_id if config else None
            yt = YouTubeAPIClient(api_key=api_key, channel_id=channel_id)
            logger.info(f"  YouTube API: channel={channel_id or 'not set'}")
        except Exception as e:
            logger.warning(f"  YouTube API init failed for profile {profile.id}: {e}")

    rt = ProfileRuntime(
        profile=profile,
        persistence=persistence,
        worker_manager=wm,
        storage_client=sc,
        youtube_client=yt,
    )

    # Start background tasks
    rt.schedule_task = asyncio.create_task(_schedule_loop(rt))

    return rt


async def _destroy_profile_runtime(profile_id: str) -> None:
    """Shut down runtime components for a profile."""
    rt = profiles.get(profile_id)
    if not rt:
        return

    logger.info(f"Destroying profile runtime: {profile_id}")

    # Cancel background tasks
    for task in [rt.schedule_task]:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Shutdown worker manager
    await rt.worker_manager.shutdown()

    del profiles[profile_id]


# ==================== Background Tasks (per-profile) ====================

async def _schedule_loop(rt: ProfileRuntime) -> None:
    """Per-profile daily schedule loop."""
    while True:
        try:
            await asyncio.sleep(60)
            config = rt.persistence.load_config_optional()
            if not config or not config.schedule_enabled or not config.effective_media_key:
                continue
            state = rt.persistence.load_state()
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

            if state.status != StreamStatus.RUNNING:
                if now_minutes >= start_minutes and (
                    state.last_scheduled_start_date is None or state.last_scheduled_start_date < today
                ):
                    try:
                        await rt.worker_manager.start_worker(config)
                        logger.info(f"[{rt.profile.id}] Schedule: started stream")
                    except WorkerManagerError as e:
                        logger.warning(f"[{rt.profile.id}] Schedule start failed: {e}")
                continue

            if state.started_at:
                try:
                    started = datetime.fromisoformat(state.started_at)
                    elapsed = (now - started).total_seconds()
                    if elapsed >= duration_seconds:
                        await rt.worker_manager.stop_worker()
                        logger.info(f"[{rt.profile.id}] Schedule: stopped stream after {config.schedule_duration_hours}h")
                except (ValueError, TypeError):
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[{rt.profile.id}] Schedule loop error: {e}")


# ==================== App Lifecycle ====================

@app.on_event("startup")
async def startup_event():
    """Initialize controller on startup."""
    global profile_registry

    logger.info("Starting stream controller (multi-profile)...")

    # Log if dashboard PIN is set
    if os.getenv("DASHBOARD_PIN"):
        logger.info("DASHBOARD_PIN is set - dashboard requires authentication")
    else:
        logger.warning("DASHBOARD_PIN not set - dashboard is UNPROTECTED!")

    # Initialize profile registry
    profile_registry = ProfileRegistry()
    logger.info("Profile registry initialized")

    # Auto-migrate legacy config if needed
    migrated = profile_registry.auto_migrate_legacy()
    if migrated:
        logger.info(f"Legacy config migrated to profile '{migrated}'")

    # Initialize all enabled profiles
    for profile in profile_registry.list_profiles():
        if not profile.enabled:
            logger.info(f"Skipping disabled profile: {profile.id}")
            continue
        try:
            rt = await _init_profile_runtime(profile)
            profiles[profile.id] = rt
        except Exception as e:
            logger.error(f"Failed to initialize profile {profile.id}: {e}")

    logger.info(f"Initialized {len(profiles)} profile(s)")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    logger.info("Shutting down stream controller...")
    for pid in list(profiles.keys()):
        await _destroy_profile_runtime(pid)


# ==================== Dashboard ====================

@app.get("/")
async def dashboard():
    """Serve dashboard HTML."""
    static_file = static_dir / "index.html"
    if static_file.exists():
        return FileResponse(str(static_file))
    raise HTTPException(status_code=404, detail="Dashboard not found")


# ==================== Auth Endpoints ====================

class LoginRequest(BaseModel):
    pin: str

class LoginResponse(BaseModel):
    token: str
    expires_in: int

@app.post("/auth/login")
async def login(request: LoginRequest):
    """Authenticate with PIN and get session token."""
    if not auth_manager.validate_pin(request.pin):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid PIN")
    token = auth_manager.create_token()
    return LoginResponse(token=token, expires_in=24 * 60 * 60)

@app.post("/auth/logout")
async def logout(request: Request):
    """Logout and revoke session token."""
    auth_header = request.headers.get("Authorization")
    token = get_token_from_header(auth_header)
    if token:
        auth_manager.revoke_token(token)
    return {"message": "Logged out successfully"}

@app.get("/auth/status")
async def auth_status():
    """Check if PIN authentication is required."""
    return {"pin_required": bool(auth_manager.pin)}

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(status="healthy", timestamp=datetime.now().isoformat())


# ==================== Profile CRUD Endpoints ====================

@app.get("/profiles")
async def list_profiles():
    """List all profiles with summary status."""
    summaries = []
    for pid, rt in profiles.items():
        state = rt.persistence.load_state()
        summaries.append(ProfileSummary(
            id=rt.profile.id,
            name=rt.profile.name,
            enabled=rt.profile.enabled,
            status=state.status,
            is_live=state.youtube_is_live,
            concurrent_viewers=state.youtube_concurrent_viewers,
        ).model_dump())

    # Include disabled profiles too
    if profile_registry:
        for p in profile_registry.list_profiles():
            if p.id not in profiles:
                summaries.append(ProfileSummary(
                    id=p.id,
                    name=p.name,
                    enabled=p.enabled,
                ).model_dump())

    return {"profiles": summaries}


@app.post("/profiles")
async def create_profile(
    request: Request,
    name: str = Form(...),
    storage_bucket: str = Form(...),
    storage_access_key_id: str = Form(...),
    storage_secret_access_key: str = Form(...),
    storage_endpoint: Optional[str] = Form(None),
    storage_provider: str = Form("cloudflare"),
    storage_region: str = Form("auto"),
    youtube_api_key: Optional[str] = Form(None),
):
    """Create a new stream profile."""
    check_auth(request)

    # Generate slug from name
    profile_id = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50]
    if not profile_id:
        profile_id = f"profile-{len(profiles) + 1}"

    # Ensure unique
    if profile_registry.get_profile(profile_id):
        profile_id = f"{profile_id}-{len(profiles) + 1}"

    try:
        profile = StreamProfile(
            id=profile_id,
            name=name,
            enabled=True,
            storage_bucket=storage_bucket,
            storage_access_key_id=storage_access_key_id,
            storage_secret_access_key_encrypted=encrypt(storage_secret_access_key),
            storage_endpoint=storage_endpoint or None,
            storage_provider=storage_provider,
            storage_region=storage_region,
            youtube_api_key_encrypted=encrypt(youtube_api_key) if youtube_api_key else None,
        )
        profile_registry.create_profile(profile)

        # Initialize runtime
        rt = await _init_profile_runtime(profile)
        profiles[profile.id] = rt

        logger.info(f"Created profile: {profile.id}")
        return {"status": "created", "profile_id": profile.id, "name": name}

    except PersistenceError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to create profile: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.get("/profiles/{profile_id}")
async def get_profile(profile_id: str):
    """Get profile details."""
    rt = _get_profile_runtime(profile_id)
    p = rt.profile
    return {
        "id": p.id,
        "name": p.name,
        "enabled": p.enabled,
        "storage_bucket": p.storage_bucket,
        "storage_provider": p.storage_provider,
        "storage_region": p.storage_region,
        "storage_endpoint": p.storage_endpoint,
        "has_youtube_api_key": bool(p.youtube_api_key_encrypted),
        "created_at": p.created_at,
    }


@app.put("/profiles/{profile_id}")
async def update_profile(
    profile_id: str,
    request: Request,
    name: Optional[str] = Form(None),
    storage_bucket: Optional[str] = Form(None),
    storage_access_key_id: Optional[str] = Form(None),
    storage_secret_access_key: Optional[str] = Form(None),
    storage_endpoint: Optional[str] = Form(None),
    storage_provider: Optional[str] = Form(None),
    storage_region: Optional[str] = Form(None),
    youtube_api_key: Optional[str] = Form(None),
    enabled: Optional[bool] = Form(None),
):
    """Update profile settings. Reinitializes runtime if credentials change."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)
    p = rt.profile

    changed_creds = False
    if name is not None:
        p.name = name
    if storage_bucket is not None:
        p.storage_bucket = storage_bucket
        changed_creds = True
    if storage_access_key_id is not None:
        p.storage_access_key_id = storage_access_key_id
        changed_creds = True
    if storage_secret_access_key is not None:
        p.storage_secret_access_key_encrypted = encrypt(storage_secret_access_key)
        changed_creds = True
    if storage_endpoint is not None:
        p.storage_endpoint = storage_endpoint or None
        changed_creds = True
    if storage_provider is not None:
        p.storage_provider = storage_provider
        changed_creds = True
    if storage_region is not None:
        p.storage_region = storage_region
        changed_creds = True
    if youtube_api_key is not None:
        p.youtube_api_key_encrypted = encrypt(youtube_api_key) if youtube_api_key else None
        changed_creds = True
    if enabled is not None:
        p.enabled = enabled

    profile_registry.update_profile(p)

    # Reinitialize runtime if credentials changed
    if changed_creds:
        await _destroy_profile_runtime(profile_id)
        new_rt = await _init_profile_runtime(p)
        profiles[profile_id] = new_rt

    return {"status": "updated", "profile_id": profile_id}


@app.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request):
    """Delete a profile. Must be stopped first."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    # Check if stream is running
    state = rt.persistence.load_state()
    active_statuses = (StreamStatus.RUNNING, StreamStatus.STARTING, StreamStatus.STREAMING)
    if state.status in active_statuses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Stop the stream before deleting the profile"}
        )

    await _destroy_profile_runtime(profile_id)
    profile_registry.delete_profile(profile_id)

    return {"status": "deleted", "profile_id": profile_id}


# ==================== Per-Profile Stream Endpoints ====================

@app.post("/profiles/{profile_id}/start")
async def profile_start_stream(profile_id: str, request: Request):
    """Start stream for a specific profile."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    try:
        config = rt.persistence.load_config()
        state = rt.persistence.load_state()

        if state.status == StreamStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "Stream already running", "current_status": state.status}
            )

        await rt.worker_manager.start_worker(config)
        new_state = rt.persistence.load_state()
        return {"status": new_state.status, "worker_pid": new_state.worker_pid, "started_at": new_state.started_at}

    except (ConfigNotFoundError, InvalidConfigError) as e:
        raise HTTPException(status_code=500, detail={"error": "Invalid configuration", "message": str(e)})
    except WorkerManagerError as e:
        raise HTTPException(status_code=500, detail={"error": "Failed to start worker", "message": str(e)})


@app.post("/profiles/{profile_id}/stop")
async def profile_stop_stream(profile_id: str, request: Request):
    """Stop stream for a specific profile."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    try:
        state = rt.persistence.load_state()
        active_statuses = (StreamStatus.RUNNING, StreamStatus.STARTING, StreamStatus.STREAMING)
        if state.status not in active_statuses:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "No worker running", "current_status": state.status}
            )

        await rt.worker_manager.stop_worker()
        new_state = rt.persistence.load_state()
        return {"status": new_state.status, "stopped_at": new_state.exited_at}

    except WorkerManagerError as e:
        raise HTTPException(status_code=500, detail={"error": "Failed to stop worker", "message": str(e)})


@app.get("/profiles/{profile_id}/status")
async def profile_get_status(profile_id: str):
    """Get stream status for a specific profile."""
    rt = _get_profile_runtime(profile_id)

    try:
        state = rt.persistence.load_state()
        config = rt.persistence.load_config_optional()

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
            rtmp_url=config.youtube_rtmp_url if config else _default_rtmp_url(),
            always_on=config.always_on if config else False,
            always_on_restart_count=state.always_on_restart_count,
            youtube_api_enabled=config.youtube_api_enabled if config else False,
            youtube_is_live=state.youtube_is_live,
            youtube_video_id=state.youtube_video_id,
            youtube_concurrent_viewers=state.youtube_concurrent_viewers,
            youtube_view_count=state.youtube_view_count,
            youtube_like_count=state.youtube_like_count,
            youtube_stream_title=state.youtube_stream_title,
        )

    except (ConfigNotFoundError, InvalidConfigError) as e:
        raise HTTPException(status_code=500, detail={"error": "Configuration error", "message": str(e)})


@app.get("/profiles/{profile_id}/config")
async def profile_get_config(profile_id: str):
    """Get stream config for a specific profile."""
    rt = _get_profile_runtime(profile_id)

    existing = rt.persistence.load_config_optional()
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
        "media_key": None, "playlist": None,
        "youtube_rtmp_url": _default_rtmp_url(),
        "loop_streaming": False, "loop_delay": 5,
        "schedule_enabled": False, "schedule_start_time": "09:00", "schedule_duration_hours": 8,
        "always_on": False, "keepalive_interval": 300,
        "youtube_api_enabled": False, "youtube_channel_id": None, "youtube_monitor_interval": 30,
    }


@app.post("/profiles/{profile_id}/config")
async def profile_update_config(
    profile_id: str,
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
    """Update stream configuration for a specific profile."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    try:
        existing = rt.persistence.load_config_optional()
        rtmp_url = youtube_rtmp_url or (existing.youtube_rtmp_url if existing else None) or _default_rtmp_url()

        playlist_list = None
        if playlist:
            playlist_list = [p.strip() for p in playlist.split(",")]
            media_key = media_key or (playlist_list[0] if playlist_list else None)
        elif media_key:
            pass
        elif existing:
            media_key = existing.media_key
            playlist_list = existing.playlist
        else:
            raise HTTPException(status_code=400, detail={"error": "Either media_key or playlist must be provided"})

        loop_streaming_val = loop_streaming if loop_streaming is not None else (existing.loop_streaming if existing else False)
        loop_delay_val = loop_delay if loop_delay is not None else (existing.loop_delay if existing else 5)
        schedule_enabled_val = schedule_enabled if schedule_enabled is not None else (existing.schedule_enabled if existing else False)
        schedule_start_val = schedule_start_time if schedule_start_time is not None else (existing.schedule_start_time if existing else "09:00")
        schedule_duration_val = schedule_duration_hours if schedule_duration_hours is not None else (existing.schedule_duration_hours if existing else 8)
        always_on_val = always_on if always_on is not None else (existing.always_on if existing else False)
        keepalive_interval_val = keepalive_interval if keepalive_interval is not None else (existing.keepalive_interval if existing else 300)

        stream_key_encrypted = None
        if youtube_stream_key:
            stream_key_encrypted = encrypt(youtube_stream_key)
        elif existing and existing.youtube_stream_key_encrypted:
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
        rt.persistence.save_config(config)

        return {"status": "config_updated", "profile_id": profile_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Failed to update config", "message": str(e)})


# ==================== Per-Profile Storage Endpoints ====================

@app.get("/profiles/{profile_id}/storage/files")
async def profile_list_files(profile_id: str):
    """List media files in a profile's storage bucket."""
    rt = _get_profile_runtime(profile_id)
    if not rt.storage_client:
        raise HTTPException(status_code=503, detail={"error": "Storage not configured for this profile"})

    try:
        files = rt.storage_client.list_media()
        return {"files": [{"key": f.key, "size": f.size, "last_modified": f.last_modified} for f in files]}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Failed to list files", "message": str(e)})


@app.post("/profiles/{profile_id}/upload")
async def profile_upload_file(
    profile_id: str,
    request: Request,
    file: UploadFile = File(...),
    object_key: Optional[str] = Form(None),
):
    """Upload file to a profile's storage bucket."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)
    if not rt.storage_client:
        raise HTTPException(status_code=503, detail={"error": "Storage not configured for this profile"})

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename}") as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name

        result = rt.storage_client.upload_file(file_path=tmp_file_path, object_key=object_key)
        os.unlink(tmp_file_path)

        return {"key": result.key, "size": result.size, "last_modified": result.last_modified}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Upload failed", "message": str(e)})


# ==================== Per-Profile YouTube Endpoints ====================

@app.get("/profiles/{profile_id}/youtube/status")
async def profile_youtube_status(profile_id: str):
    """
    Get YouTube live status for a specific profile.

    Always does a live API call (on-demand, no background polling).
    This saves API quota — only called when user clicks Refresh.
    """
    rt = _get_profile_runtime(profile_id)

    if not rt.youtube_client:
        return {"enabled": False, "error": "YouTube API not configured for this profile"}

    config = rt.persistence.load_config_optional()
    if not config or not config.youtube_api_enabled:
        return {"enabled": False, "error": "YouTube API monitoring is disabled"}

    # Update channel_id if changed in config
    if config.youtube_channel_id and config.youtube_channel_id != rt.youtube_client.channel_id:
        rt.youtube_client.channel_id = config.youtube_channel_id

    if not rt.youtube_client.channel_id:
        return {"enabled": True, "error": "YouTube Channel ID not set"}

    # Live API call (on-demand)
    state = rt.persistence.load_state()
    try:
        live_status = await rt.youtube_client.get_live_status()
        state.youtube_is_live = live_status.get('is_live', False)
        state.youtube_video_id = live_status.get('video_id')
        state.youtube_concurrent_viewers = live_status.get('concurrent_viewers')
        state.youtube_view_count = live_status.get('view_count')
        state.youtube_like_count = live_status.get('like_count')
        state.youtube_stream_title = live_status.get('title')
        state.youtube_last_poll = datetime.now().isoformat()
        rt.persistence.save_state(state)
    except Exception as e:
        logger.warning(f"[{rt.profile.id}] YouTube API call failed: {e}")
        return {
            "enabled": True,
            "error": str(e),
            "is_live": state.youtube_is_live,
            "video_id": state.youtube_video_id,
            "concurrent_viewers": state.youtube_concurrent_viewers,
            "view_count": state.youtube_view_count,
            "like_count": state.youtube_like_count,
            "stream_title": state.youtube_stream_title,
            "last_poll": state.youtube_last_poll,
        }

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


@app.post("/profiles/{profile_id}/youtube/config")
async def profile_youtube_config(
    profile_id: str,
    request: Request,
    youtube_api_enabled: Optional[bool] = Form(None),
    youtube_channel_id: Optional[str] = Form(None),
    youtube_monitor_interval: Optional[int] = Form(None),
):
    """Update YouTube API configuration for a specific profile."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    existing = rt.persistence.load_config_optional()
    if not existing:
        raise HTTPException(status_code=400, detail={"error": "No stream config exists. Set stream config first."})

    if youtube_api_enabled is not None:
        existing.youtube_api_enabled = youtube_api_enabled
    if youtube_channel_id is not None:
        existing.youtube_channel_id = youtube_channel_id.strip() or None
    if youtube_monitor_interval is not None:
        if youtube_monitor_interval < 10 or youtube_monitor_interval > 300:
            raise HTTPException(status_code=400, detail={"error": "youtube_monitor_interval must be between 10 and 300"})
        existing.youtube_monitor_interval = youtube_monitor_interval

    rt.persistence.save_config(existing)

    if rt.youtube_client and existing.youtube_channel_id:
        rt.youtube_client.channel_id = existing.youtube_channel_id

    return {
        "status": "youtube_config_updated",
        "youtube_api_enabled": existing.youtube_api_enabled,
        "youtube_channel_id": existing.youtube_channel_id,
        "youtube_monitor_interval": existing.youtube_monitor_interval,
    }


@app.get("/profiles/{profile_id}/youtube/validate")
async def profile_youtube_validate(profile_id: str, request: Request):
    """Validate YouTube API key and channel ID for a specific profile."""
    check_auth(request)
    rt = _get_profile_runtime(profile_id)

    if not rt.youtube_client:
        return {"api_key_valid": False, "error": "YouTube API not configured for this profile"}

    result = {"api_key_valid": False, "channel_valid": False, "channel_info": None}

    try:
        result["api_key_valid"] = await rt.youtube_client.validate_api_key()
    except Exception as e:
        result["api_key_error"] = str(e)

    config = rt.persistence.load_config_optional()
    channel_id = config.youtube_channel_id if config else None
    if channel_id and result["api_key_valid"]:
        try:
            channel_info = await rt.youtube_client.validate_channel_id(channel_id)
            if channel_info:
                result["channel_valid"] = True
                result["channel_info"] = channel_info
            else:
                result["channel_error"] = "Channel not found"
        except Exception as e:
            result["channel_error"] = str(e)

    return result


# ==================== Legacy Endpoints (backward compatible) ====================
# These route to the default (first) profile for backward compatibility.

@app.post("/streams/start")
async def start_stream(request: Request):
    """Legacy: Start stream on default profile."""
    return await profile_start_stream(_get_default_profile_id(), request)

@app.post("/streams/stop")
async def stop_stream(request: Request):
    """Legacy: Stop stream on default profile."""
    return await profile_stop_stream(_get_default_profile_id(), request)

@app.get("/streams/status", response_model=StreamStatusResponse)
async def get_stream_status():
    """Legacy: Get status of default profile."""
    return await profile_get_status(_get_default_profile_id())

@app.get("/streams/config")
async def get_stream_config():
    """Legacy: Get config of default profile."""
    return await profile_get_config(_get_default_profile_id())

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
    """Legacy: Update config of default profile."""
    return await profile_update_config(
        _get_default_profile_id(), request,
        media_key, playlist, youtube_rtmp_url, youtube_stream_key,
        loop_streaming, loop_delay, schedule_enabled, schedule_start_time,
        schedule_duration_hours, always_on, keepalive_interval,
    )

@app.get("/storage/files")
async def list_storage_files():
    """Legacy: List files on default profile."""
    return await profile_list_files(_get_default_profile_id())

@app.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    object_key: Optional[str] = Form(None),
):
    """Legacy: Upload to default profile."""
    return await profile_upload_file(_get_default_profile_id(), request, file, object_key)

@app.get("/youtube/status")
async def youtube_live_status():
    """Legacy: YouTube status of default profile."""
    return await profile_youtube_status(_get_default_profile_id())

@app.post("/youtube/config")
async def update_youtube_config(
    request: Request,
    youtube_api_enabled: Optional[bool] = Form(None),
    youtube_channel_id: Optional[str] = Form(None),
    youtube_monitor_interval: Optional[int] = Form(None),
):
    """Legacy: YouTube config of default profile."""
    return await profile_youtube_config(
        _get_default_profile_id(), request,
        youtube_api_enabled, youtube_channel_id, youtube_monitor_interval,
    )

@app.get("/youtube/validate")
async def validate_youtube_setup(request: Request):
    """Legacy: YouTube validate on default profile."""
    return await profile_youtube_validate(_get_default_profile_id(), request)
