"""
Configuration and state models for stream controller.
"""
import os
from datetime import datetime
from typing import Optional, List
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class StreamStatus(str, Enum):
    """Stream status values."""
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class StreamConfig(BaseModel):
    """
    Stream configuration (persisted to stream_config.json).

    Note: Stream key is NOT persisted - only loaded from environment.

    Supports two modes:
    1. Single file: Set media_key
    2. Playlist: Set playlist (list of media keys)
    """
    youtube_rtmp_url: str = Field(
        ..., description="YouTube RTMP server URL (without stream key)"
    )
    media_key: Optional[str] = Field(
        default=None, description="Single media file key (use for single file mode)"
    )
    playlist: Optional[List[str]] = Field(
        default=None, description="List of media keys to play sequentially (use for playlist mode)"
    )

    @field_validator('youtube_rtmp_url')
    @classmethod
    def validate_rtmp_url(cls, v: str) -> str:
        """Validate RTMP URL format."""
        if not v.startswith('rtmp://'):
            raise ValueError('RTMP URL must start with rtmp://')
        return v

    @field_validator('playlist')
    @classmethod
    def validate_playlist(cls, v, info):
        """Validate that either media_key or playlist is set."""
        if v is not None and len(v) == 0:
            raise ValueError('Playlist cannot be empty')
        return v

    @property
    def is_playlist(self) -> bool:
        """Check if config is in playlist mode."""
        return self.playlist is not None and len(self.playlist) > 0

    @property
    def effective_media_key(self) -> str:
        """Get the media key to use (for backwards compatibility)."""
        if self.is_playlist:
            return self.playlist[0] if self.playlist else ""
        return self.media_key or ""


class StreamState(BaseModel):
    """
    Current stream state (persisted to stream_state.json).

    Updated by controller based on worker process status.
    """
    status: StreamStatus = Field(
        default=StreamStatus.STOPPED, description="Current stream status"
    )
    worker_pid: Optional[int] = Field(
        default=None, description="Worker process ID (if running)"
    )
    started_at: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp when worker started"
    )
    exited_at: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp when worker exited"
    )
    last_health_check: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp of last health check"
    )
    exit_code: Optional[int] = Field(
        default=None, description="Worker exit code (if crashed/stopped)"
    )
    error_message: Optional[str] = Field(
        default=None, description="Error message (if in error state)"
    )
    media_key: Optional[str] = Field(
        default=None, description="Media key being streamed"
    )
    playlist_index: Optional[int] = Field(
        default=None, description="Current playlist index (0-based)"
    )
    playlist_completed: List[str] = Field(
        default_factory=list, description="List of completed media keys in playlist"
    )

    @property
    def uptime_seconds(self) -> Optional[int]:
        """Calculate uptime in seconds if stream is running."""
        if self.status != StreamStatus.RUNNING or not self.started_at:
            return None
        try:
            started = datetime.fromisoformat(self.started_at)
            uptime = (datetime.now() - started).total_seconds()
            return int(uptime)
        except (ValueError, TypeError):
            return None

    class Config:
        """Pydantic config."""
        use_enum_values = True


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(default="healthy")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class StreamStatusResponse(BaseModel):
    """Stream status API response (excludes stream key)."""
    status: StreamStatus
    worker_pid: Optional[int]
    started_at: Optional[str]
    uptime_seconds: Optional[int]
    last_health_check: Optional[str]
    exited_at: Optional[str]
    exit_code: Optional[int]
    error_message: Optional[str]
    media_key: Optional[str]
    rtmp_url: str  # Note: RTMP URL WITHOUT stream key for security
