# Proposal: YouTube Live Monetization MVP

## Why

YouTube requires 4,000 watch hours and 1,000 subscribers to qualify for the Partner Program. Continuous live streaming is an efficient way to accumulate watch hours, but operating a reliable 24/7 live channel requires infrastructure that can sustain long-running streams, recover from failures, and be repeatable without requiring constant manual intervention. We need to validate that this approach can generate revenue before investing in a scalable automated platform.

## What Changes

This change introduces a minimal, production-safe system for operating a continuous YouTube live channel:

- **New backend controller** (lightweight API) to manage stream lifecycle, configuration, and triggering start/stop operations
- **New streaming worker** that pulls media from object storage and streams to YouTube via RTMP with auto-reconnect capability
- **New storage integration** for durable, globally accessible media hosting (decoupled from compute)
- **New job execution model** for long-running stream processes with failure recovery
- **New operational model** supporting manual intervention (restarts, media updates, schedule adjustments)
- **New basic monitoring** for stream health and operational status

This is a greenfield implementation—no existing components are being replaced.

## Capabilities

### New Capabilities

- **stream-controller**: Lightweight backend API responsible for stream configuration storage, lifecycle management (start/stop), minimal logging, and safe restart operations

- **stream-worker**: Long-running process that pulls media from object storage, streams to YouTube via FFmpeg/RTMP, handles auto-reconnect on interruptions, and runs indefinitely with disposability in mind

- **media-storage**: External object storage integration (S3/R2/GCS) providing durable, globally accessible media hosting decoupled from compute infrastructure

- **stream-execution**: Job execution model for managing long-duration streaming processes with failure recovery, restart capability, and operational safety

- **stream-monitoring**: Basic health monitoring and operational status visibility for streams (not full observability—just enough to know if streams are alive)

### Modified Capabilities

None—this is a new system with no existing specification changes.

## Impact

### New Code/Components
- Stream controller backend (FastAPI or lightweight Node service)
- Streaming worker service (FFmpeg-based RTMP streaming)
- Storage client integration (S3/R2/GCS SDK)

### External Dependencies
- YouTube Live RTMP endpoint (requires YouTube channel setup and stream key)
- Object storage provider (AWS S3, Cloudflare R2, or GCS)
- FFmpeg for media streaming

### Operational Changes
- Introduces long-running processes that require restart/recovery procedures
- Requires manual operations for media updates and schedule changes
- Adds basic monitoring obligations

### Infrastructure
- Single VPS or container for controller + worker (no Kubernetes)
- External object storage bucket (decoupled from compute)
- Network access to YouTube RTMP endpoints

### Does NOT Impact
- No multi-tenant or multi-channel concerns
- No AI/automation infrastructure
- No microservices or complex orchestration
- No existing systems (greenfield)
