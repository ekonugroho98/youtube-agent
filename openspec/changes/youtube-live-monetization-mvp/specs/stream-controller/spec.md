# Spec: Stream Controller

## ADDED Requirements

### Requirement: Configuration persistence
The system SHALL persist stream configuration to JSON files on disk.

#### Scenario: Save stream configuration
- **WHEN** operator provides stream configuration (RTMP URL, stream key, media path)
- **THEN** system saves configuration to `stream_config.json` in configured directory
- **AND** file is atomically written to prevent corruption

#### Scenario: Load stream configuration
- **WHEN** system starts or needs configuration
- **THEN** system loads configuration from `stream_config.json`
- **AND** validates required fields are present
- **AND** returns error if file is missing or invalid

### Requirement: Stream lifecycle management
The system SHALL provide HTTP endpoints for starting and stopping stream worker processes.

#### Scenario: Start stream worker
- **WHEN** POST request is made to `/streams/start`
- **THEN** system launches worker subprocess with configuration
- **AND** updates stream state to "running" with worker PID
- **AND** returns HTTP 200 with worker status

#### Scenario: Start stream when already running
- **WHEN** POST request is made to `/streams/start` and worker is already running
- **THEN** system returns HTTP 409 Conflict
- **AND** includes current worker status in response

#### Scenario: Stop stream worker
- **WHEN** POST request is made to `/streams/stop`
- **THEN** system terminates worker process gracefully (SIGTERM)
- **AND** waits up to 10 seconds for clean shutdown
- **THEN** sends SIGKILL if process hasn't exited
- **AND** updates stream state to "stopped"
- **AND** returns HTTP 200

#### Scenario: Stop stream when not running
- **WHEN** POST request is made to `/streams/stop` and no worker is running
- **THEN** system returns HTTP 404 Not Found
- **AND** includes current stream state in response

### Requirement: Stream state tracking
The system SHALL maintain current stream state including status, worker PID, and last health check timestamp.

#### Scenario: Get current stream status
- **WHEN** GET request is made to `/streams/status`
- **THEN** system returns current stream state with:
  - status: "running" | "stopped" | "error"
  - worker_pid: process ID or null
  - last_health_check: ISO 8601 timestamp or null
  - uptime_seconds: seconds since worker started (if running)

#### Scenario: Worker process crashes
- **WHEN** worker process terminates unexpectedly
- **THEN** system detects process exit
- **AND** updates stream state to "error"
- **AND** records exit code and timestamp
- **AND** includes error details in next status query

### Requirement: Controller health endpoint
The system SHALL provide a health endpoint for monitoring controller availability.

#### Scenario: Controller is healthy
- **WHEN** GET request is made to `/health`
- **THEN** system returns HTTP 200
- **AND** response includes: `{"status": "healthy", "timestamp": "<ISO-8601>"}`

#### Scenario: Controller cannot access configuration directory
- **WHEN** GET request is made to `/health` and config directory is inaccessible
- **THEN** system returns HTTP 503 Service Unavailable
- **AND** response includes error details

### Requirement: Worker process supervision
The system SHALL track worker process lifecycle and detect unexpected termination.

#### Scenario: Worker process exit detection
- **WHEN** worker process exits (cleanly or via crash)
- **THEN** system detects exit within 5 seconds
- **AND** updates stream state file
- **AND** logs exit code and timestamp

#### Scenario: Cleanup orphaned workers on startup
- **WHEN** controller starts
- **THEN** system checks for existing worker PIDs from previous run
- **AND** terminates any orphaned FFmpeg processes
- **AND** resets stream state to "stopped"

### Requirement: Secure credential handling
The system SHALL never write YouTube stream key to configuration files or logs.

#### Scenario: Load credentials from environment
- **WHEN** system starts
- **THEN** system loads YouTube stream key from `YOUTUBE_STREAM_KEY` environment variable
- **AND** validates key is present and non-empty
- **AND** fails to start if key is missing

#### Scenario: Stream status response
- **WHEN** stream status is queried
- **THEN** system SHALL NOT include stream key in response
- **AND** only returns RTMP URL without key component
