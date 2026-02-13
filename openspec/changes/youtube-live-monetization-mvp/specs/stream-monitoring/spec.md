# Spec: Stream Monitoring

## ADDED Requirements

### Requirement: Controller health check
The system SHALL provide HTTP health endpoint for controller availability monitoring.

#### Scenario: Health check succeeds
- **WHEN** GET request is made to `/health`
- **THEN** system returns HTTP 200 OK
- **AND** response body contains: `{"status": "healthy", "timestamp": "<ISO-8601>"}`

#### Scenario: Health check when config directory missing
- **WHEN** GET request is made to `/health` and config directory is inaccessible
- **THEN** system returns HTTP 503 Service Unavailable
- **AND** response includes error details

### Requirement: Stream status endpoint
The system SHALL provide HTTP endpoint for querying current stream status.

#### Scenario: Query running stream status
- **WHEN** GET request is made to `/streams/status` while worker is running
- **THEN** system returns HTTP 200 OK
- **AND** response includes:
  - status: "running"
  - worker_pid: process ID
  - started_at: ISO 8601 timestamp
  - uptime_seconds: seconds since start
  - media_key: media being streamed
  - last_health_check: ISO 8601 timestamp

#### Scenario: Query stopped stream status
- **WHEN** GET request is made to `/streams/status` while no worker is running
- **THEN** system returns HTTP 200 OK
- **AND** response includes:
  - status: "stopped"
  - worker_pid: null
  - exited_at: ISO 8601 timestamp (if previously ran)

#### Scenario: Query error stream status
- **WHEN** GET request is made to `/streams/status` after worker crash
- **THEN** system returns HTTP 200 OK (not an error to query)
- **AND** response includes:
  - status: "error"
  - worker_pid: null
  - exited_at: ISO 8601 timestamp
  - exit_code: worker exit code
  - error_message: error details

### Requirement: Worker health monitoring
The system SHALL monitor worker process health and update status.

#### Scenario: Periodic health check
- **WHEN** worker process is running
- **THEN** controller checks process status every 30 seconds
- **AND** updates `last_health_check` timestamp in stream state
- **AND** logs health check result

#### Scenario: Worker becomes unresponsive
- **WHEN** worker process exists but is not consuming CPU (zombie/hang)
- **THEN** health check detects process state
- **AND** status reflects "running" but may include warning
- **AND** operator can manually stop/restart if needed

#### Scenario: Worker process disappears
- **WHEN** worker process exit is detected during health check
- **THEN** controller updates stream state to "error"
- **AND** records exit code and timestamp
- **AND** triggers process exit handling

### Requirement: Operational status scripts
The system SHALL provide shell scripts for quick operational status checks.

#### Scenario: Quick status check script
- **WHEN** operator runs `./scripts/check_status.sh`
- **THEN** script queries `/streams/status` endpoint
- **AND** displays human-readable status:
  - Stream status: running/stopped/error
  - Worker PID (if running)
  - Uptime (if running)
  - Last error (if error state)

#### Scenario: Health check script
- **WHEN** operator runs `./scripts/health.sh`
- **THEN** script queries `/health` endpoint
- **AND** displays "OK" if healthy
- **AND** displays error details if unhealthy
- **AND** exits with code 0 if healthy, 1 if unhealthy

### Requirement: Log aggregation and access
The system SHALL write logs to stdout for systemd journal capture.

#### Scenario: Controller logs
- **WHEN** controller performs operations (start/stop/status)
- **THEN** controller logs to stdout with structured format:
  - timestamp: ISO 8601
  - level: INFO/WARNING/ERROR
  - component: controller
  - message: human-readable description
  - context: relevant details (media key, PID, etc.)

#### Scenario: Worker logs via journal
- **WHEN** worker process writes to stdout
- **THEN** logs are captured by systemd journal
- **AND** accessible via `journalctl -u stream-controller`
- **AND** FFmpeg output is prefixed with `[FFMPEG]` for filtering

#### Scenario: Filter logs by component
- **WHEN** operator wants to see only FFmpeg logs
- **THEN** `journalctl -u stream-controller | grep FFMPEG` shows only FFmpeg output
- **AND** `journalctl -u stream-controller | grep -v FFMPEG` shows only controller logs

### Requirement: No external monitoring dependencies
The system SHALL not require external monitoring services (Prometheus, Datadog, etc.).

#### Scenario: Self-contained monitoring
- **WHEN** monitoring is needed
- **THEN** system provides HTTP endpoints for status/health
- **AND** operator can use existing tools (curl, wget, monitoring services)
- **AND** no third-party monitoring integration required

#### Scenario: Integration with external monitoring (optional)
- **WHEN** operator wants external monitoring
- **THEN** `/health` endpoint is compatible with standard HTTP monitors
- **AND** `/streams/status` provides detailed status for custom dashboards
- **AND** systemd integration works with standard service monitors
