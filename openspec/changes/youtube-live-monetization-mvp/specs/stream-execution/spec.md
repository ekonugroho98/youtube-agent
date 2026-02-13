# Spec: Stream Execution

## ADDED Requirements

### Requirement: Worker process spawning
The system SHALL spawn worker processes as child subprocesses of the controller.

#### Scenario: Spawn worker on stream start
- **WHEN** controller receives request to start stream
- **THEN** controller spawns worker process using subprocess module
- **AND** passes configuration via command-line arguments or stdin
- **AND** captures worker stdout/stderr for logging
- **AND** records worker PID in stream state

#### Scenario: Worker spawn failure
- **WHEN** worker process fails to start (missing dependencies, invalid path)
- **THEN** controller logs error with details
- **AND** returns HTTP 500 Internal Server Error
- **AND** updates stream state to "error"

#### Scenario: Worker process inherits environment
- **WHEN** worker process is spawned
- **THEN** worker inherits controller's environment variables
- **AND** worker has access to STORAGE_* and YOUTUBE_* variables
- **AND** controller does NOT pass sensitive values via CLI args

### Requirement: Process lifecycle tracking
The system SHALL track worker process state from spawn through termination.

#### Scenario: Track running worker
- **WHEN** worker process is running
- **THEN** controller periodically checks process status via polling
- **AND** updates stream state file with alive status
- **AND** records last health check timestamp

#### Scenario: Detect worker termination
- **WHEN** worker process exits (cleanly or via crash)
- **THEN** controller detects exit within 5 seconds
- **AND** retrieves process exit code
- **AND** updates stream state with exit status
- **AND** logs termination event with context

#### Scenario: Multiple workers blocked
- **WHEN** start request is received while worker is already running
- **THEN** controller rejects request with HTTP 409 Conflict
- **AND** returns current worker status including PID and uptime
- **AND** does NOT spawn second worker process

### Requirement: Graceful worker termination
The system SHALL terminate worker processes gracefully when stopping streams.

#### Scenario: Graceful shutdown via SIGTERM
- **WHEN** controller sends stop signal to worker
- **THEN** controller sends SIGTERM to worker process
- **AND** waits up to 10 seconds for process to exit
- **AND** checks process status every second

#### Scenario: Force kill after timeout
- **WHEN** worker process does not exit within 10 seconds of SIGTERM
- **THEN** controller sends SIGKILL to worker process
- **AND** logs forced termination
- **AND** updates stream state to "stopped"

#### Scenario: Worker responds to SIGTERM
- **WHEN** worker receives SIGTERM
- **THEN** worker terminates FFmpeg subprocess
- **AND** cleans up resources
- **AND** exits with status 0

### Requirement: Orphan cleanup on startup
The system SHALL detect and clean up orphaned worker processes from previous controller runs.

#### Scenario: Detect orphaned workers on startup
- **WHEN** controller starts
- **THEN** controller reads stream state file for previous worker PID
- **AND** checks if process with that PID exists
- **AND** verifies process is actually a worker (command-line inspection)

#### Scenario: Terminate orphaned worker
- **WHEN** orphaned worker process is found
- **THEN** controller sends SIGTERM to orphaned process
- **AND** waits up to 5 seconds for clean exit
- **THEN** sends SIGKILL if process persists
- **AND** logs cleanup action
- **AND** resets stream state to "stopped"

#### Scenario: PID reused by different process
- **WHEN** PID from state file exists but is not a worker process
- **THEN** controller does NOT kill the process
- **AND** resets stream state to "stopped"
- **AND** logs PID reuse detected

### Requirement: Worker restart on crash
The system SHALL NOT automatically restart crashed workers (handled by systemd).

#### Scenario: Worker crashes and exits
- **WHEN** worker process exits with error status
- **THEN** controller updates stream state to "error"
- **AND** records exit code and timestamp
- **AND** does NOT automatically restart worker
- **AND** relies on systemd to restart controller if needed

#### Scenario: Manual restart after crash
- **WHEN** operator calls `/streams/start` after worker crash
- **THEN** controller spawns new worker process
- **AND** resets stream state to "running"
- **AND** logs manual restart event

### Requirement: Process resource limits
The system SHALL enforce resource limits on worker processes to prevent resource exhaustion.

#### Scenario: Set CPU priority
- **WHEN** worker process is spawned
- **THEN** controller sets process nice value to 10 (lower priority)
- **AND** allows controller to remain responsive

#### Scenario: Limit worker subprocesses
- **WHEN** worker spawns FFmpeg subprocess
- **THEN** worker process monitors subprocess tree
- **AND** ensures only one FFmpeg process runs per worker
- **AND** FFmpeg inherits same resource constraints

### Requirement: Stream state persistence
The system SHALL persist stream state to disk for recovery across controller restarts.

#### Scenario: Persist state on worker start
- **WHEN** worker is successfully spawned
- **THEN** controller writes stream state file with:
  - status: "running"
  - worker_pid: process ID
  - started_at: ISO 8601 timestamp
  - media_key: media being streamed
- **AND** file is atomically written

#### Scenario: Persist state on worker stop
- **WHEN** worker exits (cleanly or via crash)
- **THEN** controller updates stream state file with:
  - status: "stopped" or "error"
  - worker_pid: null
  - exited_at: ISO 8601 timestamp
  - exit_code: process exit code

#### Scenario: Load state on controller startup
- **WHEN** controller starts
- **THEN** controller loads stream state file if exists
- **AND** validates state (checks if PID still exists)
- **AND** cleans up orphaned workers if needed
