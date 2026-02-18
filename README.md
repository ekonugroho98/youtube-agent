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

### Web Dashboard (Recommended)

Access the web dashboard for easy configuration:

```bash
# Start the controller
python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000

# Open dashboard in browser
open http://localhost:8000
```

**Dashboard Features:**
- 📤 **Upload Files** - Drag & drop or browse to upload media
- 📁 **Storage Browser** - View all files in your storage bucket
- ⚙️ **Config Panel** - Set single file or playlist mode
- 🎮 **Stream Controls** - Start/stop streaming with one click
- 📊 **Status Monitor** - Real-time stream status, uptime, PID
- 🔄 **Auto-refresh** - Status updates every 5 seconds

**Using the Dashboard:**
1. Open `http://localhost:8000` in your browser
2. Upload files via the Upload panel
3. Configure stream (Single File or Playlist tab)
4. Click "Start Stream"
5. Monitor status in real-time
6. Stop when done

### 1. Upload Media to Storage

Upload your video file to your S3-compatible storage bucket:

#### Using Python Script (Recommended)

```bash
# Upload file with progress bar
python scripts/upload.py final.mp4

# Upload with custom name
python scripts/upload.py /path/to/video.mp4 custom-name.mp4

# Upload to folder in bucket
python scripts/upload.py murattal.mp3 live-media/murattal.mp3
```

Features:
- Progress bar with speed & ETA
- Multipart upload for large files
- Automatic retry on connection failure
- Generates stream URL after upload

#### Using CLI Tools

```bash
# Using AWS CLI
aws s3 cp video.mp4 s3://your-bucket/video.mp4

# Using rclone for R2
rclone copy video.mp4 r2:your-bucket/video.mp4
```

#### Using Dashboard (Browser)

1. Login to [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Navigate to **R2** → Select bucket
3. Click **Upload** → Select file

> **Note:** Dashboard has size limitations for large files. Use Python script or CLI for files >100MB.

### 2. Configure Stream (Set Which File to Stream)

Before starting, you need to specify which file to stream:

#### Single File Mode

```bash
# Set single file to stream (e.g., smaller.mp4)
curl -X POST "http://localhost:8000/streams/config?media_key=smaller.mp4"
```

Response:
```json
{
  "status": "config_updated",
  "mode": "single",
  "media_key": "smaller.mp4",
  "youtube_rtmp_url": "rtmp://a.rtmp.youtube.com/live2"
}
```

#### Playlist Mode (Multiple Files Sequentially)

Stream multiple files in sequence (e.g., different surahs):

```bash
# Set playlist (comma-separated)
curl -X POST "http://localhost:8000/streams/config?playlist=surah_1.mp4,surah_2.mp4,surah_3.mp4"
```

Response:
```json
{
  "status": "config_updated",
  "mode": "playlist",
  "media_key": "surah_1.mp4",
  "playlist": ["surah_1.mp4", "surah_2.mp4", "surah_3.mp4"],
  "youtube_rtmp_url": "rtmp://a.rtmp.youtube.com/live2"
}
```

**Playlist Behavior:**
- Plays files sequentially in order
- Shows progress: `[1/3] surah_1.mp4` → `[2/3] surah_2.mp4` → `[3/3] surah_3.mp4`
- 3-second delay between tracks (configurable via `PLAYLIST_DELAY`)
- On error: skips to next track by default (configurable via `PLAYLIST_ON_ERROR`)
- When combined with `LOOP_STREAMING=true`: restarts playlist from beginning after completion

**Use Cases:**
- Quran recitation (multiple surahs in sequence)
- Podcast episodes
- Music albums
- Lecture series

### 3. Start Streaming

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

The worker will:
1. Fetch signed URL from storage
2. Start FFmpeg to stream to YouTube
3. Auto-retry on failure (up to 3 times)
4. Log streaming progress

#### Loop Streaming (Optional)

Enable automatic looping to restart the stream when video ends.

**From Dashboard (recommended):** Open the Stream Configuration panel → check "Auto loop when video ends" → set "Delay between loops (seconds)" → click "Save loop" or "Set Config"/"Set Playlist". The setting is stored in config and applied when you start the stream.

**From .env (fallback):** If no config is set, the worker uses environment variables:

```bash
# Enable loop streaming
LOOP_STREAMING=true

# Delay between loops (seconds, default: 5)
LOOP_DELAY=5
```

When enabled:
- Stream automatically restarts when video ends
- Infinite loop until manually stopped
- Useful for 24/7 live streams with repeat content
- Worker logs each loop iteration

**Use cases:**
- 24/7 radio stations with looping audio
- Background music/ambiance streams
- Waiting screen streams
- Scheduled content loops

#### Daily Schedule (e.g. 8 jam/hari, auto tiap hari)

Atur stream agar **otomatis mulai di jam yang sama setiap hari** dan berjalan **N jam**, lalu stop; besok di jam yang sama mulai lagi.

**Dari Dashboard:** Stream Configuration → centang "Daily schedule" → isi **Mulai jam** (HH:MM, mis. 09:00) dan **Durasi (jam/hari)** (mis. 8) → "Save schedule". Pastikan config media/playlist sudah di-set dan controller berjalan (systemd/cron).

**Perilaku:**
- Controller mengecek setiap menit. Saat jam sudah mencapai "Mulai jam" dan stream belum jalan (dan hari ini belum pernah start), stream di-start.
- Setelah **Durasi** jam, stream di-stop otomatis.
- Besok di jam yang sama proses diulang (start → jalan N jam → stop).

**Contoh:** Mulai jam 09:00, durasi 8 jam → live 09:00–17:00 setiap hari.

### 4. Check Status

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
  "media_key": "smaller.mp4",
  "rtmp_url": "rtmp://a.rtmp.youtube.com/live2"
}
```

### 5. Stop Streaming

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

### Complete Example: Stream `smaller.mp4`

```bash
# 1. Upload file to storage
python scripts/upload.py smaller.mp4

# 2. Setup config directory
sudo mkdir -p /var/lib/stream-controller
sudo chown $USER:$USER /var/lib/stream-controller

# 3. Start controller (if not running)
python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000

# 4. Configure which file to stream
curl -X POST "http://localhost:8000/streams/config?media_key=smaller.mp4"

# 5. Start streaming
curl -X POST http://localhost:8000/streams/start

# 6. Check status
curl http://localhost:8000/streams/status

# 7. Open YouTube Studio to see your live stream!

# 8. Stop when done
curl -X POST http://localhost:8000/streams/stop
```

### Complete Example: Playlist Mode (Multiple Surahs)

```bash
# 1. Upload all files to storage
python scripts/upload.py surah_1.mp4
python scripts/upload.py surah_2.mp4
python scripts/upload.py surah_3.mp4

# 2. Setup config directory
sudo mkdir -p /var/lib/stream-controller
sudo chown $USER:$USER /var/lib/stream-controller

# 3. Start controller
python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000

# 4. Configure playlist
curl -X POST "http://localhost:8000/streams/config?playlist=surah_1.mp4,surah_2.mp4,surah_3.mp4"

# 5. (Optional) Enable looping for 24/7 stream
# Edit .env: LOOP_STREAMING=true

# 6. Start streaming
curl -X POST http://localhost:8000/streams/start

# Worker will play:
# [1/3] surah_1.mp4 → [2/3] surah_2.mp4 → [3/3] surah_3.mp4 → (repeat if LOOP_STREAMING=true)

# 7. Check status
curl http://localhost:8000/streams/status

# 8. Stop when done
curl -X POST http://localhost:8000/streams/stop
```

## API Endpoints

### Web Interface
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard (UI) |

### Stream Management
| Endpoint | Method | Parameters | Description |
|----------|--------|------------|-------------|
| `/health` | GET | - | Health check |
| `/streams/config` | POST | `media_key` (query, optional), `playlist` (query, optional), `youtube_rtmp_url` (query, optional) | Set which file/playlist to stream |
| `/streams/start` | POST | - | Start streaming |
| `/streams/stop` | POST | - | Stop streaming |
| `/streams/status` | GET | - | Get stream status |

### Storage Operations
| Endpoint | Method | Parameters | Description |
|----------|--------|------------|-------------|
| `/upload` | POST | `file` (form-data), `object_key` (form-data, optional) | Upload file to storage |
| `/storage/files` | GET | - | List all media files in storage |

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
