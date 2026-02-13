# Spec: Stream Worker

## ADDED Requirements

### Requirement: Media streaming from object storage
The system SHALL stream media from object storage directly to YouTube via RTMP using FFmpeg.

#### Scenario: Stream media from storage URL
- **WHEN** worker starts with media key from object storage
- **THEN** worker generates signed URL for media
- **AND** invokes FFmpeg with storage URL as input
- **AND** pipes media to YouTube RTMP endpoint
- **AND** does not download media to local disk

#### Scenario: Invalid media URL
- **WHEN** storage URL is inaccessible or returns 404
- **THEN** FFmpeg fails to start
- **AND** worker logs error with details
- **AND** worker exits with non-zero status code

### Requirement: FFmpeg auto-reconnect on network interruption
The system SHALL configure FFmpeg to automatically reconnect to YouTube RTMP endpoint on network drop.

#### Scenario: Network drops during stream
- **WHEN** network connection to YouTube RTMP is interrupted
- **THEN** FFmpeg automatically reconnects within 5 seconds
- **AND** resumes streaming from interruption point
- **AND** worker process continues running

#### Scenario: YouTube terminates stream
- **WHEN** YouTube closes RTMP connection (e.g., policy violation, stream key reset)
- **THEN** FFmpeg fails to reconnect after retry attempts
- **AND** worker detects FFmpeg exit
- **AND** worker exits with error status

### Requirement: FFmpeg subprocess monitoring
The system SHALL monitor FFmpeg subprocess health and detect process crashes.

#### Scenario: Monitor FFmpeg process health
- **WHEN** FFmpeg subprocess is running
- **THEN** worker monitors process status via polling or async wait
- **AND** detects process termination within 5 seconds

#### Scenario: FFmpeg crashes unexpectedly
- **WHEN** FFmpeg subprocess exits with error code
- **THEN** worker logs error with exit code
- **AND** worker implements retry logic with exponential backoff
- **AND** retry sequence: 30s, 60s, 120s (max backoff)

### Requirement: Failure recovery with exponential backoff
The system SHALL retry failed streams with exponential backoff up to a maximum retry limit.

#### Scenario: Retry on transient failure
- **WHEN** FFmpeg exits with transient error (network, temporary storage issue)
- **THEN** worker waits 30 seconds before first retry
- **AND** logs retry attempt number
- **AND** restarts FFmpeg with same configuration

#### Scenario: Exponential backoff between retries
- **WHEN** previous retry attempt failed
- **THEN** worker doubles wait time from previous attempt
- **AND** sequence: 30s, 60s, 120s, 120s (capped)
- **AND** logs each retry delay

#### Scenario: Max retries exceeded
- **WHEN** worker has failed 3 consecutive times
- **THEN** worker exits with error status
- **AND** does not attempt further retries
- **AND** relies on controller/systemd for process restart

### Requirement: Worker logging
The system SHALL log all worker operations and FFmpeg output to stdout for systemd journal capture.

#### Scenario: Log FFmpeg stdout
- **WHEN** FFmpeg writes to stdout
- **THEN** worker captures and logs all output
- **AND** prefixes lines with `[FFMPEG]` for filtering

#### Scenario: Log worker events
- **WHEN** worker performs significant actions (start, retry, exit)
- **THEN** worker logs event with timestamp
- **AND** includes relevant context (media key, retry count, exit code)

#### Scenario: Log structured errors
- **WHEN** error occurs (storage access, FFmpeg failure)
- **THEN** worker logs error with:
  - error_type: classification (storage, network, ffmpeg)
  - error_message: human-readable description
  - retry_count: current retry attempt
  - will_retry: boolean indicating if retry will occur

### Requirement: Graceful shutdown handling
The system SHALL handle shutdown signals (SIGTERM, SIGINT) and terminate FFmpeg gracefully.

#### Scenario: Receive SIGTERM from controller
- **WHEN** worker receives SIGTERM signal
- **THEN** worker sends SIGTERM to FFmpeg subprocess
- **AND** waits up to 10 seconds for FFmpeg to exit cleanly
- **THEN** sends SIGKILL if FFmpeg hasn't exited
- **AND** worker exits with status 0

#### Scenario: Receive SIGINT (Ctrl+C)
- **WHEN** worker receives SIGINT signal
- **THEN** worker behaves identically to SIGTERM (graceful shutdown)

### Requirement: Media codec configuration
The system SHALL support streaming common video formats to YouTube Live.

#### Scenario: Stream MP4 video
- **WHEN** media source is MP4 format (H.264/AAC)
- **THEN** worker configures FFmpeg to copy codec without re-encoding
- **AND** uses `-c copy` for efficiency

#### Scenario: Stream non-MP4 video
- **WHEN** media source requires transcoding for YouTube compatibility
- **THEN** worker configures FFmpeg to transcode to H.264/AAC
- **AND** uses preset targeting 720p @ 3Mbps
- **AND** adjusts for YouTube Live requirements
