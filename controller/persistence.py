"""
File-based persistence for stream configuration and state.
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict

from .models import StreamConfig, StreamState, StreamProfile


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


class ProfileRegistry:
    """
    Manages profiles.json — the registry of all stream profiles.

    Each profile maps to a subdirectory with its own StreamPersistence.
    """

    PROFILES_FILE = "profiles.json"

    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = Path(config_dir or os.getenv(
            "STREAM_CONFIG_DIR",
            "/var/lib/stream-controller"
        ))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.config_dir / self.PROFILES_FILE
        logger.info(f"ProfileRegistry initialized in: {self.config_dir}")

    def _load_raw(self) -> List[Dict]:
        """Load raw profiles list from JSON."""
        if not self.profiles_path.exists():
            return []
        try:
            with open(self.profiles_path, 'r') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to load profiles: {e}")
            return []

    def _save_raw(self, profiles: List[Dict]) -> None:
        """Save profiles list to JSON atomically."""
        temp_path = self.profiles_path.with_suffix('.tmp')
        try:
            with open(temp_path, 'w') as f:
                json.dump(profiles, f, indent=2)
            temp_path.replace(self.profiles_path)
        except Exception as e:
            if temp_path.exists():
                temp_path.unlink()
            raise PersistenceError(f"Failed to save profiles: {e}")

    def list_profiles(self) -> List[StreamProfile]:
        """List all registered profiles."""
        raw = self._load_raw()
        profiles = []
        for item in raw:
            try:
                profiles.append(StreamProfile(**item))
            except Exception as e:
                logger.warning(f"Skipping invalid profile: {e}")
        return profiles

    def get_profile(self, profile_id: str) -> Optional[StreamProfile]:
        """Get a profile by ID."""
        for p in self.list_profiles():
            if p.id == profile_id:
                return p
        return None

    def create_profile(self, profile: StreamProfile) -> StreamProfile:
        """Create a new profile. Raises if ID already exists."""
        existing = self.get_profile(profile.id)
        if existing:
            raise PersistenceError(f"Profile '{profile.id}' already exists")

        raw = self._load_raw()
        raw.append(profile.model_dump(mode='json'))
        self._save_raw(raw)

        # Create profile config directory
        profile_dir = self.config_dir / f"profile_{profile.id}"
        profile_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Created profile: {profile.id} ({profile.name})")
        return profile

    def update_profile(self, profile: StreamProfile) -> StreamProfile:
        """Update an existing profile."""
        raw = self._load_raw()
        updated = False
        for i, item in enumerate(raw):
            if item.get('id') == profile.id:
                raw[i] = profile.model_dump(mode='json')
                updated = True
                break
        if not updated:
            raise PersistenceError(f"Profile '{profile.id}' not found")
        self._save_raw(raw)
        logger.info(f"Updated profile: {profile.id}")
        return profile

    def delete_profile(self, profile_id: str) -> None:
        """Delete a profile and its config directory."""
        raw = self._load_raw()
        new_raw = [item for item in raw if item.get('id') != profile_id]
        if len(new_raw) == len(raw):
            raise PersistenceError(f"Profile '{profile_id}' not found")
        self._save_raw(new_raw)

        # Remove profile config directory
        import shutil
        profile_dir = self.config_dir / f"profile_{profile_id}"
        if profile_dir.exists():
            shutil.rmtree(profile_dir)

        logger.info(f"Deleted profile: {profile_id}")

    def get_profile_persistence(self, profile_id: str) -> StreamPersistence:
        """Get a StreamPersistence instance for a specific profile."""
        profile_dir = str(self.config_dir / f"profile_{profile_id}")
        return StreamPersistence(config_dir=profile_dir)

    def auto_migrate_legacy(self) -> Optional[str]:
        """
        Auto-migrate legacy single-stream config to a 'default' profile.

        If profiles.json doesn't exist but stream_config.json does,
        creates a 'default' profile using env-based credentials.

        Returns:
            Profile ID of migrated profile, or None if no migration needed.
        """
        if self.profiles_path.exists():
            return None

        legacy_config = self.config_dir / StreamPersistence.CONFIG_FILE
        if not legacy_config.exists():
            return None

        logger.info("Auto-migrating legacy config to 'default' profile...")

        from .encryption import encrypt

        # Create default profile with env credentials
        profile = StreamProfile(
            id="default",
            name="Default Channel",
            enabled=True,
            storage_bucket=os.getenv("STORAGE_BUCKET", ""),
            storage_access_key_id=os.getenv("STORAGE_ACCESS_KEY_ID", ""),
            storage_secret_access_key_encrypted=encrypt(os.getenv("STORAGE_SECRET_ACCESS_KEY", "")),
            storage_endpoint=os.getenv("R2_ENDPOINT"),
            storage_provider=os.getenv("STORAGE_PROVIDER", "cloudflare"),
            storage_region=os.getenv("STORAGE_REGION", "auto"),
            youtube_api_key_encrypted=encrypt(os.getenv("YOUTUBE_API_KEY", "")) if os.getenv("YOUTUBE_API_KEY") else None,
        )

        # Create profile directory and copy legacy files
        profile_dir = self.config_dir / f"profile_{profile.id}"
        profile_dir.mkdir(parents=True, exist_ok=True)

        import shutil
        # Copy config
        if legacy_config.exists():
            shutil.copy2(str(legacy_config), str(profile_dir / StreamPersistence.CONFIG_FILE))
        # Copy state
        legacy_state = self.config_dir / StreamPersistence.STATE_FILE
        if legacy_state.exists():
            shutil.copy2(str(legacy_state), str(profile_dir / StreamPersistence.STATE_FILE))

        # Save profile registry
        self._save_raw([profile.model_dump(mode='json')])

        logger.info(f"Migrated legacy config to profile 'default'")
        return profile.id
