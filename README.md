# YouTube Live Stream Agent

Automated YouTube live streaming system using FFmpeg and S3-compatible storage (Cloudflare R2, AWS S3, or Google Cloud Storage).

## Architecture

```
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   HTTP API      │─────▶│ Stream Controller│─────▶│  FFmpeg Worker  │
│  (FastAPI)      │      │    (Process)     │      │  (Stream to YT) │
└─────────────────┘      └──────────────────┘      └─────────────────┘
                                  │
                                  ▼
                          ┌──────────────────┐
                          │ S3-Compatible     │
                          │ Storage (R2/S3)   │
                          └──────────────────┘
```

## Prerequisites

- **OS**: Linux (Ubuntu 20.04+ or similar)
- **Python**: 3.8 or higher
- **FFmpeg**: Installed and accessible in PATH
- **YouTube Live Stream**: Enabled channel with stream key
- **Storage**: Cloudflare R2, AWS S3, or GCS bucket

## Installation

### 1. Get YouTube Stream Key

1. Go to [YouTube Studio](https://studio.youtube.com)
2. Navigate to **Create** → **Go Live**
3. Copy your **Stream Key** (format: `xxxx-xxxx-xxxx-xxxx`)
4. Note the **RTMP URL**: `rtmp://a.rtmp.youtube.com/live2`

### 2. Setup Storage (Choose One)

#### Cloudflare R2 (Recommended)

1. Create a Cloudflare R2 bucket
2. Get your Account ID from R2 dashboard
3. Create API Token with R2 permissions
4. Note your R2 endpoint: `https://<accountid>.r2.cloudflarestorage.com`

#### AWS S3

1. Create an S3 bucket
2. Create IAM user with S3 permissions
3. Get Access Key ID and Secret Access Key

#### Google Cloud Storage

1. Create a GCS bucket
2. Enable Interoperability Access
3. Get Access Key and Secret

### 3. Clone and Setup

```bash
# Clone repository
git clone https://github.com/ekonugroho98/youtube-agent.git
cd youtube-agent

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit with your credentials
nano .env
```

Update the following variables:

```bash
# Controller Configuration
STREAM_CONTROLLER_PORT=8000
STREAM_CONFIG_DIR=/var/lib/stream-controller
LOG_LEVEL=info

# Storage Configuration
STORAGE_PROVIDER=cloudflare  # or "aws" or "gcs"
STORAGE_BUCKET=live-media
STORAGE_ACCESS_KEY_ID=your_access_key_here
STORAGE_SECRET_ACCESS_KEY=your_secret_key_here
STORAGE_REGION=auto
R2_ENDPOINT=https://your_account_id.r2.cloudflarestorage.com  # For R2 only

# YouTube Configuration
YOUTUBE_RTMP_URL=rtmp://a.rtmp.youtube.com/live2
YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx  # Your stream key

# Worker Configuration
FFMPEG_PATH=/usr/bin/ffmpeg
WORKER_RETRY_DELAY=30
WORKER_MAX_RETRIES=3
```

### 5. Setup System Directory (Optional)

For production deployment, create the config directory:

```bash
# Create directory
sudo mkdir -p /var/lib/stream-controller
sudo chown $USER:$USER /var/lib/stream-controller

# Update .env with the directory path
echo "STREAM_CONFIG_DIR=/var/lib/stream-controller" >> .env
```

## Running the Service

### Option 1: Direct (Development)

```bash
# Activate virtual environment
source venv/bin/activate

# Start the controller
python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000
```

### Option 2: Using Scripts

```bash
# Make scripts executable
chmod +x scripts/*.sh

# Start the service
./scripts/start.sh

# Check status
./scripts/check_status.sh

# Stop the service
./scripts/stop.sh

# Restart
./scripts/restart.sh

# Health check
./scripts/health.sh
```

### Option 3: Systemd Service (Production)

```bash
# Install as systemd service
sudo cp scripts/stream-controller.service /etc/systemd/system/

# Create service user
sudo useradd -r -s /bin/false stream

# Setup environment file
sudo mkdir -p /etc/stream-controller
sudo cp .env /etc/stream-controller/.env
sudo chown stream:stream /etc/stream-controller/.env
chmod 600 /etc/stream-controller/.env

# Copy application files
sudo mkdir -p /opt/stream-controller
sudo cp -r . /opt/stream-controller/
sudo chown -R stream:stream /opt/stream-controller

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable stream-controller
sudo systemctl start stream-controller

# Check status
sudo systemctl status stream-controller
```

## Usage

### 1. Upload Media to Storage

Upload your video file to your S3-compatible storage bucket:

```bash
# Using AWS CLI
aws s3 cp video.mp4 s3://your-bucket/video.mp4

# Using rclone for R2
rclone copy video.mp4 r2:your-bucket/video.mp4
```

### 2. Start Streaming

```bash
# Start the stream worker
curl -X POST http://localhost:8000/streams/start
```

Response:
```json
{
  "status": "running",
  "worker_pid": 12345,
  "started_at": "2024-02-17T10:30:00"
}
```

### 3. Check Status

```bash
# Get current stream status
curl http://localhost:8000/streams/status
```

Response:
```json
{
  "status": "running",
  "worker_pid": 12345,
  "started_at": "2024-02-17T10:30:00",
  "uptime_seconds": 300,
  "last_health_check": "2024-02-17T10:35:00",
  "rtmp_url": "rtmp://a.rtmp.youtube.com/live2"
}
```

### 4. Stop Streaming

```bash
# Stop the stream worker
curl -X POST http://localhost:8000/streams/stop
```

Response:
```json
{
  "status": "stopped",
  "stopped_at": "2024-02-17T11:00:00"
}
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/streams/start` | POST | Start streaming |
| `/streams/stop` | POST | Stop streaming |
| `/streams/status` | GET | Get stream status |

## Troubleshooting

### Service won't start

1. Check if port 8000 is available:
   ```bash
   lsof -i :8000
   ```

2. Check environment variables:
   ```bash
   cat /etc/stream-controller/.env
   ```

3. Check service logs:
   ```bash
   sudo journalctl -u stream-controller -f
   ```

### Stream not working on YouTube

1. Verify stream key is correct
2. Check YouTube Studio for error messages
3. Verify FFmpeg is installed:
   ```bash
   ffmpeg -version
   ```

4. Check worker logs for FFmpeg errors

### Storage connection failed

1. Verify credentials in `.env`
2. Check bucket name is correct
3. For R2, verify `R2_ENDPOINT` format
4. Test connection manually:
   ```bash
   aws s3 ls s3://your-bucket --endpoint-url=https://your_account_id.r2.cloudflarestorage.com
   ```

## Development

### Running Tests

```bash
# Activate virtual environment
source venv/bin/activate

# Run tests
pytest tests/

# Run with coverage
pytest --cov=controller --cov=worker --cov=storage tests/
```

### Project Structure

```
youtube-agent/
├── controller/          # Stream controller API
│   ├── main.py         # FastAPI application
│   ├── models.py       # Data models
│   ├── persistence.py  # State management
│   └── worker_manager.py # Worker process manager
├── worker/             # FFmpeg worker
│   ├── worker.py       # Worker logic
│   └── ffmpeg.py       # FFmpeg wrapper
├── storage/            # Storage client
│   └── client.py       # S3-compatible client
├── scripts/            # Utility scripts
├── tests/              # Test suite
├── .env.example        # Environment template
└── requirements.txt    # Python dependencies
```

## License

MIT
