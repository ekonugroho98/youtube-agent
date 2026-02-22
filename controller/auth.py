"""
Simple PIN-based authentication for dashboard.

Uses PIN from environment and session tokens for authentication.
"""
import os
import secrets
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict
from fastapi import HTTPException, status


logger = logging.getLogger(__name__)


# Token expiry: 24 hours
TOKEN_EXPIRY_SECONDS = 24 * 60 * 60


class AuthManager:
    """Manages PIN authentication and session tokens."""

    def __init__(self):
        """Initialize auth manager with PIN from environment."""
        self.pin = os.getenv("DASHBOARD_PIN")

        # Generate random secret for signing if not set
        self.secret = os.getenv("SESSION_SECRET", secrets.token_hex(32))

        # Store active tokens: {token: {"expiry": timestamp}}
        self._tokens: Dict[str, float] = {}

        if not self.pin:
            logger.warning("DASHBOARD_PIN not set - dashboard will be unprotected!")

    def validate_pin(self, pin: str) -> bool:
        """
        Validate PIN against environment variable.

        Args:
            pin: PIN to validate

        Returns:
            True if PIN is correct (or if no PIN is set)
        """
        if not self.pin:
            # No PIN set - allow access (dev mode)
            return True

        return pin == self.pin

    def create_token(self) -> str:
        """
        Create a new session token.

        Returns:
            Session token (random hex string)
        """
        # Generate random token
        token = secrets.token_hex(32)

        # Set expiry (24 hours from now)
        expiry = time.time() + TOKEN_EXPIRY_SECONDS
        self._tokens[token] = expiry

        logger.info(f"Created session token (expires: {datetime.fromtimestamp(expiry)})")
        return token

    def validate_token(self, token: str) -> bool:
        """
        Validate session token.

        Args:
            token: Session token to validate

        Returns:
            True if token is valid and not expired
        """
        if not token:
            return False

        # Clean expired tokens first
        self._cleanup_expired()

        expiry = self._tokens.get(token)
        if not expiry:
            return False

        # Check if expired
        if time.time() > expiry:
            del self._tokens[token]
            return False

        return True

    def revoke_token(self, token: str) -> bool:
        """
        Revoke (logout) a session token.

        Args:
            token: Token to revoke

        Returns:
            True if token was found and revoked
        """
        if token in self._tokens:
            del self._tokens[token]
            logger.info(f"Revoked session token")
            return True
        return False

    def _cleanup_expired(self) -> None:
        """Remove expired tokens from storage."""
        now = time.time()
        expired = [t for t, exp in self._tokens.items() if exp < now]
        for token in expired:
            del self._tokens[token]

        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired token(s)")


# Global auth manager instance
_auth_manager: Optional[AuthManager] = None


def get_auth_manager() -> AuthManager:
    """Get or create global auth manager instance."""
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager


def get_token_from_header(auth_header: Optional[str]) -> Optional[str]:
    """
    Extract token from Authorization header.

    Args:
        auth_header: Authorization header value (e.g., "Bearer token123")

    Returns:
        Token string or None
    """
    if not auth_header:
        return None

    if auth_header.startswith("Bearer "):
        return auth_header[7:]  # Remove "Bearer " prefix

    return None


def require_auth(pin_optional: bool = False):
    """
    Decorator to require authentication for endpoints.

    Args:
        pin_optional: If True, allow access without PIN when DASHBOARD_PIN is not set

    Usage:
        @app.get("/protected")
        @require_auth()
        async def protected_route(token: str = Depends(get_token_from_header_override)):
            ...
    """
    from fastapi import Request

    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Get auth manager
            auth = get_auth_manager()

            # If no PIN set and optional, allow access
            if not auth.pin and pin_optional:
                return await func(*args, **kwargs)

            # Get request from args (first arg after self for methods)
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break

            if not request:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not get request object"
                )

            # Get token from header
            auth_header = request.headers.get("Authorization")
            token = get_token_from_header(auth_header)

            if not token or not auth.validate_token(token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing authentication token"
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
