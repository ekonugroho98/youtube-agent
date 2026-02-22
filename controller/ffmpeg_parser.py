"""
FFmpeg output parser for detecting stream status.

Parses FFmpeg stderr to determine if stream is successfully
connecting to YouTube or failing with errors.
"""
import re
import logging
from typing import Optional, Tuple
from enum import Enum


logger = logging.getLogger(__name__)


class StreamConnectionState(Enum):
    """Stream connection state based on FFmpeg output."""
    UNKNOWN = "unknown"         # Not enough info yet
    CONNECTING = "connecting"   # FFmpeg trying to connect
    STREAMING = "streaming"     # Successfully sending data
    FAILED = "failed"           # Connection failed or error


# Patterns that indicate successful streaming
SUCCESS_PATTERNS = [
    # RTMP connection successful
    r"Connection successful",
    r"Server\s+returned:\s+200\s+OK",
    r"rtmp://.*:\s*OK",

    # FFmpeg is sending frames
    r"frame=\s+\d+\s+fps=",
    r"size=\s+\d+\s+time=",
    r"bitrate=\s+\d+\.?\d*kbits/s",

    # RTMP specific success messages
    r"rtmp\s+closing",
    r"rtmp\s+streaming",
    r"Progress:\s+\d+%",
]

# Patterns that indicate connection failure
FAILURE_PATTERNS = [
    # Connection errors
    r"Connection\s+refused",
    r"Connection\s+timed\s+out",
    r"Network\s+is\s+unreachable",
    r"No\s+route\s+to\s+host",
    r"Host\s+not\s+found",

    # HTTP/RTMP errors
    r"403\s+Forbidden",
    r"401\s+Unauthorized",
    r"404\s+Not\s+Found",
    r"503\s+Service\s+Unavailable",

    # Stream rejected
    r"Stream\s+key\s+invalid",
    r"Authentication\s+failed",
    r"Access\s+denied",

    # FFmpeg fatal errors
    r"Exiting\s+normally,\s+received\s+signal\s+15",
    r"Input/output\s+error",
    r"Broken\s+pipe",

    # RTMP specific errors
    r"rtmp\s+error",
    r"rtmp.*failed",
]

# Patterns that indicate FFmpeg is still starting up
STARTING_PATTERNS = [
    r"ffmpeg\s+version",
    r"Configuration:",
    r"lib.*\d+\.\d+",
    r"Input\s+#0",
    r"Output\s+#0",
    r"Press\s+\[q\]\s+to\s+stop",
]


def compile_patterns(patterns: list) -> list:
    """Compile regex patterns for efficiency."""
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Pre-compile patterns
SUCCESS_REGEX = compile_patterns(SUCCESS_PATTERNS)
FAILURE_REGEX = compile_patterns(FAILURE_PATTERNS)
STARTING_REGEX = compile_patterns(STARTING_PATTERNS)


def parse_line(line: str) -> Optional[StreamConnectionState]:
    """
    Parse a single line of FFmpeg output.

    Args:
        line: Line from FFmpeg stderr

    Returns:
        StreamConnectionState or None if line doesn't indicate state
    """
    if not line:
        return None

    line = line.strip()

    # Check for failure patterns (highest priority)
    for pattern in FAILURE_REGEX:
        if pattern.search(line):
            logger.debug(f"Detected failure pattern: {line[:100]}")
            return StreamConnectionState.FAILED

    # Check for success patterns
    for pattern in SUCCESS_REGEX:
        if pattern.search(line):
            logger.debug(f"Detected success pattern: {line[:100]}")
            return StreamConnectionState.STREAMING

    # Check for starting patterns (low priority - usually ignored)
    for pattern in STARTING_REGEX:
        if pattern.search(line):
            # Don't log for common startup messages
            return StreamConnectionState.CONNECTING

    return None


def determine_state(log_lines: list[str]) -> Tuple[StreamConnectionState, Optional[str]]:
    """
    Determine stream state from collected log lines.

    Args:
        log_lines: List of FFmpeg stderr lines

    Returns:
        Tuple of (state, error_message)
    """
    if not log_lines:
        return StreamConnectionState.UNKNOWN, None

    last_state = StreamConnectionState.UNKNOWN
    error_message = None

    for line in log_lines:
        state = parse_line(line)

        if state == StreamConnectionState.FAILED:
            # Once failed, stay failed
            return StreamConnectionState.FAILED, _extract_error_message(line)

        if state == StreamConnectionState.STREAMING:
            last_state = StreamConnectionState.STREAMING

        elif state == StreamConnectionState.CONNECTING:
            if last_state == StreamConnectionState.UNKNOWN:
                last_state = StreamConnectionState.CONNECTING

    return last_state, error_message


def _extract_error_message(line: str) -> str:
    """Extract meaningful error message from FFmpeg output."""
    # Common error patterns to extract
    error_patterns = [
        r"Connection\s+(refused|timed\s+out)",
        r"(403|401|404|503)\s+\w+",
        r"Stream\s+key\s+invalid",
        r"Authentication\s+failed",
        r"Access\s+denied",
        r"Input/output\s+error",
    ]

    for pattern in error_patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            return match.group(0)

    # Fallback: return first 100 chars
    return line[:100] if len(line) > 100 else line


class FFmpegLogMonitor:
    """
    Monitor FFmpeg log output and track stream state.

    Accumulates log lines and provides current state.
    """

    # Max lines to keep in memory (avoid unbounded growth)
    MAX_LOG_LINES = 500

    def __init__(self):
        """Initialize log monitor."""
        self.log_lines: list[str] = []
        self.current_state = StreamConnectionState.UNKNOWN
        self.error_message: Optional[str] = None

    def add_line(self, line: str) -> Optional[StreamConnectionState]:
        """
        Add a log line and update state.

        Args:
            line: Line from FFmpeg stderr

        Returns:
            New state if it changed, None otherwise
        """
        if not line:
            return None

        self.log_lines.append(line)

        # Keep only recent lines
        if len(self.log_lines) > self.MAX_LOG_LINES:
            self.log_lines = self.log_lines[-self.MAX_LOG_LINES:]

        # Parse and update state
        line_state = parse_line(line)

        if line_state == StreamConnectionState.FAILED:
            if self.current_state != StreamConnectionState.FAILED:
                self.current_state = StreamConnectionState.FAILED
                self.error_message = _extract_error_message(line)
                return StreamConnectionState.FAILED

        elif line_state == StreamConnectionState.STREAMING:
            if self.current_state != StreamConnectionState.STREAMING:
                self.current_state = StreamConnectionState.STREAMING
                return StreamConnectionState.STREAMING

        return None

    def get_state(self) -> Tuple[StreamConnectionState, Optional[str]]:
        """Get current state and error message."""
        return self.current_state, self.error_message

    def reset(self) -> None:
        """Reset monitor state."""
        self.log_lines = []
        self.current_state = StreamConnectionState.UNKNOWN
        self.error_message = None
