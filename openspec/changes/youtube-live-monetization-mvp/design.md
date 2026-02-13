# Design: YouTube Live Monetization MVP

## Context

**Current State**: Greenfield project—no existing streaming infrastructure or components.

**Problem**: Need to validate that continuous YouTube live streaming can generate 4,000 watch hours for monetization before investing in a scalable platform. The system must be reliable enough to run 24/7 with minimal operational overhead, but simple enough to build and deploy quickly.

**Constraints**:
- **Speed over elegance**: Revenue validation is the priority, not engineering perfection
- **Single-channel scope**: No multi-tenant or multi-channel orchestration
- **Manual operations acceptable**: Human intervention is acceptable for recovery and updates
- **Disposable compute**: Workers must be replaceable without data loss
- **Decoupled storage**: All media lives outside compute infrastructure
- **Budget-conscious**: Avoid expensive managed services when simple alternatives exist

**Stakeholders**:
- YouTube channel operator (needs reliable streams, simple operations)
- Platform user (watches monetization progress)
- Developer/maintainer (needs simple codebase, easy debugging)

## Goals / Non-Goals

**Goals:**

- Build a minimal reliable system that can sustain 24/7 YouTube live streaming
- Enable fast recovery from failures (worker crash, network issues, YouTube drops)
- Provide simple operational interfaces (start/stop streams, update media)
- Decouple storage from compute to enable disposable workers
- Accumulate watch hours toward YouTube Partner Program thresholds
- Validate revenue model before scaling architecture

**Non-Goals:**

- Multi-channel orchestration or management
- AI-driven content selection or scheduling
- Kubernetes or complex orchestration infrastructure
- Full automation of media updates or schedule changes
- Advanced observability, analytics, or dashboards
- Auto-scaling or high-availability clustering
- Complex agent frameworks or decision engines
- Beautiful UI—CLI and simple API sufficient

## Decisions

### 1. Controller Backend: FastAPI over Node.js

**Decision**: Use FastAPI (Python) for the stream controller.

**Rationale**:
- **FFmpeg integration**: Python has excellent FFmpeg wrapper libraries (ffmpeg-python) and subprocess handling
- **Type safety**: Pydantic provides built-in validation for configuration models
- **Async support**: Native async/await for concurrent stream operations
- **Deployment simplicity**: Single binary with Uvicorn, no build step
- **Developer velocity**: Less boilerplate than Node/Express for simple CRUD operations

**Alternatives considered**:
- Node.js/Express: Good streaming ecosystem, but more boilerplate for simple APIs
- Go: Excellent performance, but slower development velocity for MVP validation

### 2. Streaming Model: Long-Running Worker Process

**Decision**: Single long-running Python worker process using FFmpeg subprocess.

**Rationale**:
- **Simplicity**: One process per stream = easy to debug and monitor
- **FFmpeg reliability**: Battle-tested for RTMP streaming with auto-reconnect
- **Process supervision**: Can use systemd or Docker restart policies for crash recovery
- **Resource visibility**: Simple `ps` or `docker ps` to check stream health

**Alternatives considered**:
- Task queue (Celery/RQ): Overkill for single-process model, adds Redis dependency
- Multi-threaded worker: Adds complexity without benefit—YouTube RTMP is single connection

### 3. Storage: Cloudflare R2 (Primary), S3 API Compatible (Fallback)

**Decision**: Use Cloudflare R2 for object storage with S3-compatible API for portability.

**Rationale**:
- **Zero egress fees**: R2 doesn't charge for bandwidth—critical for streaming media
- **S3 API compatible**: Use boto3 library, can switch to S3/GCS later if needed
- **Global edge caching**: Built-in CDN for faster media pulls by worker
- **Cost-effective**: Cheaper than S3 for high-bandwidth use case

**Alternatives considered**:
- AWS S3: Expensive egress fees for streaming media
- Google Cloud Storage: Good, but less cost-effective than R2 for bandwidth-heavy workloads
- Local disk on worker: Violates disposability constraint—data loss on VPS failure

### 4. Deployment: Single VPS + systemd over Docker Compose

**Decision**: Deploy controller and worker on single VPS using systemd for service supervision.

**Rationale**:
- **Simplicity**: No container orchestration complexity for single-machine deployment
- **Native process supervision**: systemd handles restarts, logging, and crash recovery
- **Resource efficiency**: No container overhead for CPU-intensive FFmpeg workloads
- **Easy debugging**: Direct access to logs and processes

**Alternatives considered**:
- Docker Compose: Good for local dev, but adds unnecessary layer for production deployment
- Kubernetes: Violates "no premature scaling" principle—massive overkill for single service

### 5. Stream State: File-Based Persistence over Database

**Decision**: Store stream configuration and state in JSON files, not a database.

**Rationale**:
- **Simplicity**: No database migration, backup, or connection management
- **Version control friendly**: Configs can be tracked in git (secrets excluded)
- **Sufficient for MVP**: Single channel has minimal state complexity
- **Atomic updates**: File writes are atomic enough for this use case

**Alternatives considered**:
- PostgreSQL/SQLite: Unnecessary complexity for single-channel configuration
- Redis: Adds operational dependency without benefit for non-distributed system

### 6. Monitoring: HTTP Health Endpoint + Process Monitoring

**Decision**: Simple `/health` endpoint on controller + process supervision by systemd.

**Rationale**:
- **Sufficient**: Binary "stream is alive or dead" status is all we need for MVP
- **No external dependencies**: No Prometheus, Grafana, or third-party services
- **Operable**: Can script simple checks or use existing monitoring tools
- **Fast**: No complex queries or metrics aggregation

**Alternatives considered**:
- Full observability stack (Prometheus/Grafana): Overkill for MVP validation phase
- Third-party APM: Expensive and unnecessary for single-service system

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Operator                                  │
│  (CLI / API calls to manage streams)                              │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Stream Controller                            │
│                      (FastAPI + Uvicorn)                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Configuration Store (JSON files)                         │   │
│  │  - stream_config.json (RTMP URL, stream key, media path)  │   │
│  │  - stream_state.json (status, PID, last health check)     │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  HTTP Endpoints                                           │   │
│  │  POST /streams/start   - Launch worker process            │   │
│  │  POST /streams/stop    - Terminate worker process          │   │
│  │  GET  /streams/status - Stream health/status              │   │
│  │  GET  /health          - Controller health                │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              │ starts/stops
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Stream Worker Process                          │
│                    (Python + FFmpeg)                             │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Media Storage Client (boto3 + R2/S3)                     │   │
│  │  - Downloads/streams media directly from object storage   │   │
│  │  - No local caching—stream directly via pipe             │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  FFmpeg Subprocess                                        │   │
│  │  - Pulls media from storage URL                           │   │
│  │  - Transcodes to RTMP                                     │   │
│  │  - Auto-reconnect on network drop                         │   │
│  │  - Logs to stdout (captured by journald)                  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              │ RTMP stream
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        YouTube Live                               │
│                    (rtmp.youtube.com/live)                       │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### Stream Controller (`/controller`)

**Responsibilities**:
- Serve HTTP API for stream management
- Persist and retrieve stream configuration (JSON files)
- Start/stop worker processes via subprocess management
- Provide health endpoints for monitoring

**Key Files**:
- `main.py`: FastAPI app with endpoints
- `config.py`: Configuration models (Pydantic)
- `storage.py`: Read/write stream state files
- `worker.py`: Worker process management (spawn, kill, check status)

**Endpoints**:
- `POST /streams/start` → Launch worker process, update state
- `POST /streams/stop` → Terminate worker process, update state
- `GET /streams/status` → Return current stream state (running/stopped/error)
- `GET /health` → Return 200 if controller is healthy

### Stream Worker (`/worker`)

**Responsibilities**:
- Pull media URL from object storage
- Spawn FFmpeg subprocess with RTMP destination
- Monitor subprocess health and auto-restart on failure
- Log errors and status updates

**Key Files**:
- `worker.py`: Main worker process entrypoint
- `ffmpeg.py`: FFmpeg subprocess wrapper with auto-reconnect
- `storage.py`: Object storage client (boto3 wrapper)

**FFmpeg Configuration**:
- Input: Media URL from object storage (direct stream, no download)
- Output: YouTube RTMP URL with stream key
- Flags: `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`
- Codec: Copy or simple transcode depending on source format

**Failure Recovery**:
- If FFmpeg exits with error → wait 30s, retry
- If 3 consecutive failures → exit with error, let systemd restart
- Exponential backoff for restarts (30s, 60s, 120s max)

### Media Storage (`/storage`)

**Responsibilities**:
- Provide S3-compatible API for media operations
- Generate signed URLs for worker to stream media directly
- Abstract storage provider (R2, S3, GCS)

**Configuration**:
- Provider: `cloudflare` (R2) or `aws` (S3)
- Bucket name: from environment variable
- Access key/secret: from environment variable

**Operations**:
- `list_media()`: List available media files in bucket
- `get_stream_url(key)`: Generate signed URL for streaming

### Operational Scripts (`/scripts`)

- `start.sh`: Start controller via systemd
- `stop.sh`: Stop controller
- `restart.sh`: Restart controller
- `update_media.sh`: Manually update media playlist
- `check_status.sh`: Quick health check

## Deployment Model

### Infrastructure

**Single VPS** (e.g., Hetzner, DigitalOcean, Linode):
- 2-4 vCPUs (FFmpeg is CPU-intensive for transcoding)
- 4-8 GB RAM (sufficient for single stream)
- 100 GB SSD (OS + logs + minimal local cache)
- Ubuntu 24.04 LTS or Debian 12

**Network Requirements**:
- Stable outbound internet connection (RTMP requires consistent upload)
- Open port: Controller API (default 8000, can be firewalled)

### Systemd Services

**Controller Service** (`stream-controller.service`):
```ini
[Unit]
Description=YouTube Stream Controller
After=network.target

[Service]
Type=simple
User=stream
WorkingDirectory=/opt/stream-controller
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=/etc/stream-controller/.env
ExecStart=/opt/stream-controller/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Worker Service** (managed by controller, not separate systemd unit):
- Controller spawns worker subprocess
- Worker runs as child process of controller
- Controller manages worker lifecycle

### Environment Variables

```
# Controller
STREAM_CONTROLLER_PORT=8000
STREAM_CONFIG_DIR=/var/lib/stream-controller
LOG_LEVEL=info

# Storage
STORAGE_PROVIDER=cloudflare  # or aws
STORAGE_BUCKET=my-stream-media
STORAGE_ACCESS_KEY_ID=xxx
STORAGE_SECRET_ACCESS_KEY=xxx
STORAGE_REGION=auto

# YouTube (WARNING: Keep secret, never commit)
YOUTUBE_RTMP_URL=rtmp://a.rtmp.youtube.com/live2
YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx

# Worker
FFMPEG_PATH=/usr/bin/ffmpeg
WORKER_RETRY_DELAY=30
WORKER_MAX_RETRIES=3
```

## Risks / Trade-offs

### Risk 1: VPS Network Instability Causes Stream Drops

**Impact**: Lost watch hours if streams drop and don't recover quickly.

**Mitigation**:
- FFmpeg auto-reconnect flags enabled (`-reconnect 1`)
- Worker auto-restart on failure with exponential backoff
- systemd restart policy for controller
- Consider VPS with stable network (Hetzner, DigitalOcean)

### Risk 2: YouTube Terminates Stream for Policy Violation

**Impact**: Channel strike, lost streaming capability.

**Mitigation**:
- Use only licensed/public domain content for MVP validation
- Monitor YouTube Studio for stream health warnings
- Implement graceful stop on API signal (not kill -9)
- Design allows quick content swap via storage update

### Risk 3: Object Storage Egress Costs Blow Budget

**Impact**: High operational costs negate revenue model.

**Mitigation**:
- **Primary mitigation**: Use Cloudflare R2 (zero egress fees)
- Stream media directly from storage (no intermediate download)
- Consider lower bitrate for media if bandwidth costs rise
- Monitor storage usage daily in first week

### Risk 4: FFmpeg Subprocess Zombies Accumulate

**Impact**: Resource exhaustion, VPS crash.

**Mitigation**:
- Controller tracks worker PIDs explicitly
- Cleanup on startup: Kill orphaned FFmpeg processes
- Process supervision: Controller checks worker health periodically
- systemd `Restart=on-failure` for controller cleanup

### Risk 5: Stream Key Leak Enables Unauthorized Streaming

**Impact**: Malicious actors stream to your channel, policy violations.

**Mitigation**:
- Stream key only in environment variables, never in code/config
- Restrict controller API with firewall (localhost only or VPN)
- Rotate YouTube stream key immediately if suspected leak
- Use separate YouTube account for MVP (protect main channel)

### Trade-off: Manual Operations vs. Automation

**Decision**: Accept manual operations for media updates and schedule changes.

**Rationale**: Automation is premature—workflow is unproven. Building automation before validating revenue model risks wasted effort.

**Revisit criteria**: If manual operations become bottleneck after monetization, then automate.

### Trade-off: Single Point of Failure (Single VPS)

**Decision**: Accept single VPS as SPOF for MVP.

**Rationale**: High availability costs money. Revenue validation is the goal, not 99.9% uptime. If VPS dies, manual restart in 1-2 hours is acceptable.

**Revisit criteria**: If downtime cost > revenue, then add backup worker.

## Migration Plan

### Phase 1: Local Development (Week 1)

1. Set up local FastAPI controller with basic endpoints
2. Implement FFmpeg worker that streams to test RTMP server
3. Mock storage with local files for testing
4. Test failure scenarios (kill worker, network drop)

### Phase 2: Staging Deployment (Week 1-2)

1. Provision VPS (Hetzner/DigitalOcean)
2. Set up Cloudflare R2 bucket, upload test media
3. Deploy controller with systemd
4. Test worker lifecycle (start/stop/restart)
5. 24-hour soak test with monitoring

### Phase 3: Production Launch (Week 2)

1. Set up YouTube channel, generate stream key
4. Deploy production controller with real credentials
5. Upload licensed media to R2 bucket
6. Start first continuous stream
7. Monitor YouTube Studio for watch hours

### Rollback Strategy

**If critical bug discovered in production**:
1. `POST /streams/stop` to halt worker
2. SSH into VPS, `systemctl stop stream-controller`
3. Fix bug locally, test
4. Deploy fix, `systemctl start stream-controller`
5. `POST /streams/start` to resume

**Maximum expected downtime**: 1-2 hours (acceptable for MVP validation phase)

## Open Questions

1. **Media format requirements**: What video codec, bitrate, and format will YouTube accept for optimal streaming quality vs. bandwidth trade-off?
   - **Decision point**: Run test streams with 720p @ 3Mbps as starting point
   - **Revisit**: If bandwidth costs high or quality issues arise

2. **Media licensing for MVP**: What content will we stream without risking YouTube policy violations?
   - **Decision point**: Source public domain or properly licensed content
   - **Revisit**: None—this is a legal/compliance requirement

3. **Monetization tracking**: How will we track progress toward 4,000 watch hours?
   - **Decision point**: Manual check in YouTube Studio for MVP
   - **Revisit**: Build automated watch-hour tracking if MVP validates model

4. **Cost threshold for "too expensive"**: At what monthly cost do we pivot to different architecture?
   - **Decision point**: Set budget limit of $50/month for infrastructure
   - **Revisit**: If R2 + VPS exceeds budget, optimize or reconsider approach
