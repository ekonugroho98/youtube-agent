"""
Configuration and state models for stream controller.
"""
import os
from datetime import datetime
from typing import Optional
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
    """
    youtube_rtmp_url: str = Field(
        ..., description="YouTube RTMP server URL (without stream key)"
    )
    media_key: str = Field(
        ..., description="Media file key in object storage bucket"
    )

    @field_validator('youtube_rtmp_url')
    @classmethod
    def validate_rtmp_url(cls, v: str) -> str:
        """Validate RTMP URL format."""
        if not v.startswith('rtmp://'):
            raise ValueError('RTMP URL must start with rtmp://')
        return v


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
