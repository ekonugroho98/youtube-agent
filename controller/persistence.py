"""
File-based persistence for stream configuration and state.
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional

from .models import StreamConfig, StreamState


logger = logging.getLogger(__name__)


class PersistenceError(Exception):
    """Base persistence error."""
    pass


class ConfigNotFoundError(PersistenceError):
    """Configuration file not found."""
    pass


class InvalidConfigError(PersistenceError):
    """Configuration file is invalid."""
    pass


class StreamPersistence:
    """
    File-based persistence for stream configuration and state.

    Uses atomic writes to prevent corruption.
    """

    # Default filenames
    CONFIG_FILE = "stream_config.json"
    STATE_FILE = "stream_state.json"

    def __init__(self, config_dir: Optional[str] = None):
        """
        Initialize persistence with config directory.

        Args:
            config_dir: Directory for config files (from env or default)
        """
        self.config_dir = Path(config_dir or os.getenv(
            "STREAM_CONFIG_DIR",
            "/var/lib/stream-controller"
        ))

        # Create directory if it doesn't exist
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.config_dir / self.CONFIG_FILE
        self.state_path = self.config_dir / self.STATE_FILE

        logger.info(f"Initialized persistence in: {self.config_dir}")

    def load_config(self) -> StreamConfig:
        """
        Load stream configuration from file.

        Returns:
            StreamConfig object

        Raises:
            ConfigNotFoundError: Config file doesn't exist
            InvalidConfigError: Config file is invalid JSON or fails validation
        """
        if not self.config_path.exists():
            raise ConfigNotFoundError(
                f"Configuration file not found: {self.config_path}. "
                "Create stream_config.json or set STREAM_CONFIG_DIR."
            )

        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
            config = StreamConfig(**data)
            logger.info(f"Loaded config from {self.config_path}")
            return config

        except json.JSONDecodeError as e:
            raise InvalidConfigError(
                f"Invalid JSON in {self.config_path}: {str(e)}"
            )
        except Exception as e:
            raise InvalidConfigError(
                f"Failed to validate config: {str(e)}"
            )

    def load_config_optional(self) -> Optional[StreamConfig]:
        """
        Load stream configuration if file exists.

        Returns:
            StreamConfig if file exists and is valid, None otherwise.
        """
        if not self.config_path.exists():
            return None
        try:
            return self.load_config()
        except (ConfigNotFoundError, InvalidConfigError):
            return None

    def save_config(self, config: StreamConfig) -> None:
        """
        Save stream configuration to file (atomic write).

        Args:
            config: StreamConfig object to save
        """
        data = config.model_dump(mode='json')
        self._atomic_write(self.config_path, data)
        logger.info(f"Saved config to {self.config_path}")

    def load_state(self) -> StreamState:
        """
        Load stream state from file.

        Returns:
            StreamState object (defaults to STOPPED if file doesn't exist)

        Raises:
            InvalidConfigError: State file is invalid JSON or fails validation
        """
        if not self.state_path.exists():
            # No state file = stopped (fresh start)
            logger.info("No state file, defaulting to STOPPED")
            return StreamState(status="stopped")

        try:
            with open(self.state_path, 'r') as f:
                data = json.load(f)
            state = StreamState(**data)
            logger.debug(f"Loaded state: {state.status}")
            return state

        except json.JSONDecodeError as e:
            raise InvalidConfigError(
                f"Invalid JSON in {self.state_path}: {str(e)}"
            )
        except Exception as e:
            raise InvalidConfigError(
                f"Failed to validate state: {str(e)}"
            )

    def save_state(self, state: StreamState) -> None:
        """
        Save stream state to file (atomic write).

        Args:
            state: StreamState object to save
        """
        data = state.model_dump(mode='json', exclude_none=True)
        self._atomic_write(self.state_path, data)
        logger.debug(f"Saved state: {state.status}")

    def _atomic_write(self, path: Path, data: dict) -> None:
        """
        Write data to file atomically to prevent corruption.

        Args:
            path: File path to write
            data: Dictionary data to write as JSON
        """
        # Write to temporary file first
        temp_path = path.with_suffix('.tmp')
        try:
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Atomic rename (overwrites target if exists)
            temp_path.replace(path)

        except Exception as e:
            # Clean up temp file on error
            if temp_path.exists():
                temp_path.unlink()
            raise PersistenceError(f"Failed to write {path}: {str(e)}")
