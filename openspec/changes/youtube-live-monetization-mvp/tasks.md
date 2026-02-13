# Implementation Tasks: YouTube Live Monetization MVP

## 1. Project Setup

- [x] 1.1 Create project directory structure (`controller/`, `worker/`, `storage/`, `scripts/`)
- [x] 1.2 Initialize Python virtual environment and requirements.txt
- [x] 1.3 Install FastAPI, uvicorn, boto3, pydantic dependencies
- [x] 1.4 Create `.env.example` file with all required environment variables
- [x] 1.5 Set up systemd service file template for controller

## 2. Storage Layer

- [x] 2.1 Implement S3-compatible storage client with boto3
- [x] 2.2 Add provider selection logic (Cloudflare R2 vs AWS S3)
- [x] 2.3 Implement `list_media()` function with file extension filtering
- [x] 2.4 Implement `get_stream_url()` function with 24-hour signed URL generation
- [x] 2.5 Add error handling (connection timeout, auth failure, bucket not found)
- [x] 2.6 Add environment variable validation for storage credentials
- [x] 2.7 Write unit tests for storage client operations

## 3. Controller Core

- [x] 3.1 Implement Pydantic configuration models (stream config, stream state)
- [x] 3.2 Implement file-based persistence for `stream_config.json`
- [x] 3.3 Implement file-based persistence for `stream_state.json`
- [x] 3.4 Add atomic file write operations to prevent corruption
- [x] 3.5 Implement config loading with validation on startup
- [x] 3.6 Implement stream state update operations
- [x] 3.7 Add error handling for missing/invalid config files

## 4. Controller API Endpoints

- [x] 4.1 Create FastAPI application with CORS and basic middleware
- [x] 4.2 Implement `GET /health` endpoint returning controller status
- [x] 4.3 Implement `POST /streams/start` endpoint to spawn worker
- [x] 4.4 Implement `POST /streams/stop` endpoint to terminate worker
- [x] 4.5 Implement `GET /streams/status` endpoint returning current state
- [x] 4.6 Add duplicate worker prevention (409 Conflict if already running)
- [x] 4.7 Add validation for stream configuration before starting worker
- [x] 4.8 Return HTTP 404 for stop operations on non-running workers

## 5. Worker Process Management

- [x] 5.1 Implement worker subprocess spawning via Python subprocess module
- [x] 5.2 Pass configuration to worker via environment/command-line (not secrets)
- [x] 5.3 Capture worker stdout/stderr for logging
- [x] 5.4 Track worker PID in stream state file
- [x] 5.5 Implement periodic worker health checks (every 30 seconds)
- [x] 5.6 Detect worker process exit within 5 seconds
- [x] 5.7 Update stream state to "error" on worker crash with exit code
- [x] 5.8 Implement orphan worker cleanup on controller startup
- [x] 5.9 Add SIGTERM/SIGKILL graceful shutdown logic with 10s timeout

## 6. Stream Worker

- [x] 6.1 Create worker.py entrypoint with argument parsing
- [x] 6.2 Implement storage client integration for media streaming
- [x] 6.3 Implement signed URL retrieval for media files
- [x] 6.4 Create FFmpeg subprocess wrapper with auto-reconnect flags
- [x] 6.5 Configure FFmpeg with `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`
- [x] 6.6 Implement codec copy for MP4 files (`-c copy`)
- [x] 6.7 Implement transcoding fallback for non-MP4 files (H.264/AAC @ 720p/3Mbps)
- [x] 6.8 Capture FFmpeg stdout/stderr with `[FFMPEG]` log prefix
- [x] 6.9 Implement FFmpeg process monitoring and exit detection

## 7. Worker Failure Recovery

- [x] 7.1 Implement exponential backoff retry logic (30s → 60s → 120s max)
- [x] 7.2 Track retry count in worker state
- [x] 7.3 Implement max retry limit (3 consecutive failures)
- [x] 7.4 Exit with error status after max retries (let systemd restart)
- [x] 7.5 Add structured error logging with error_type, retry_count, will_retry
- [x] 7.6 Implement graceful shutdown on SIGTERM/SIGINT
- [x] 7.7 Send SIGTERM to FFmpeg, wait 10s, then SIGKILL if needed

## 8. Monitoring & Logging

- [x] 8.1 Configure Python logging to stdout with JSON/structured format
- [x] 8.2 Implement log levels (INFO, WARNING, ERROR) with timestamps
- [ ] 8.3 Add request ID tracking for API endpoint logs
- [x] 8.4 Implement worker health check timestamp updates
- [ ] 8.5 Create systemd journal integration documentation
- [x] 8.6 Add `last_health_check` timestamp to stream state

## 9. Operational Scripts

- [x] 9.1 Create `scripts/start.sh` for systemd service start
- [x] 9.2 Create `scripts/stop.sh` for service stop
- [x] 9.3 Create `scripts/restart.sh` for service restart
- [x] 9.4 Create `scripts/check_status.sh` for human-readable status query
- [x] 9.5 Create `scripts/health.sh` for health check with exit codes
- [x] 9.6 Make all scripts executable with proper shebangs

## 10. Credential Security

- [x] 10.1 Ensure YouTube stream key only loaded from `YOUTUBE_STREAM_KEY` env var
- [x] 10.2 Ensure storage credentials only loaded from `STORAGE_*` env vars
- [x] 10.3 Validate all required credentials on controller startup
- [x] 10.4 Prevent credentials from appearing in logs or error messages
- [x] 10.5 Exclude stream key from `/streams/status` response (only RTMP URL)
- [x] 10.6 Add `.env` to `.gitignore` in project setup

## 11. Testing & Validation

- [ ] 11.1 Unit tests for storage client (mock boto3)
- [ ] 11.2 Unit tests for controller endpoint handlers
- [ ] 11.3 Unit tests for worker retry logic
- [ ] 11.4 Integration test: controller start/stop worker lifecycle
- [ ] 11.5 Integration test: worker handles FFmpeg crash
- [ ] 11.6 Integration test: orphan cleanup on startup
- [ ] 11.7 Test FFmpeg with sample MP4 file locally
- [ ] 11.8 Test with mock RTMP server (e.g., nginx-rtmp)

## 12. Deployment Configuration

- [ ] 12.1 Create systemd service file template (`stream-controller.service`)
- [ ] 12.2 Create environment file template with all required variables
- [ ] 12.3 Create deployment documentation for VPS setup
- [ ] 12.4 Document FFmpeg installation requirements
- [ ] 12.5 Document Python 3.11+ dependency
- [ ] 12.6 Create directory structure setup script (`/var/lib/stream-controller`, `/opt/stream-controller`)

## 13. Production Readiness

- [ ] 13.1 Test with Cloudflare R2 bucket and real media file
- [ ] 13.2 Test 24-hour continuous stream in staging environment
- [ ] 13.3 Verify FFmpeg auto-reconnect on network interruption
- [ ] 13.4 Verify worker restart after crash (systemd recovery)
- [ ] 13.5 Test controller start/stop/monitoring via API
- [ ] 13.6 Load test: verify single VPS can handle 720p stream
- [ ] 13.7 Validate no memory leaks in 24-hour soak test
- [ ] 13.8 Confirm log rotation via systemd/journald configuration

## 14. Documentation

- [ ] 14.1 Write README with architecture overview
- [ ] 14.2 Document API endpoints with curl examples
- [ ] 14.3 Document environment variables and configuration
- [ ] 14.4 Create troubleshooting guide (worker crashes, network issues)
- [ ] 14.5 Document rollback procedure for critical bugs
- [ ] 14.6 Document media upload process to object storage
