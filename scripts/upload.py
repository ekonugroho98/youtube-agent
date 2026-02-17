#!/usr/bin/env python3
"""
Upload media files to S3-compatible storage (R2/S3/GCS).

Usage:
    python scripts/upload.py <file_path> [object_key]

Examples:
    python scripts/upload.py final.mp4
    python scripts/upload.py /path/to/video.mp4 custom-name.mp4
    python scripts/upload.py murattal.mp3 live-media/murattal.mp3
"""
import os
import sys
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables from .env
from dotenv import load_dotenv
load_dotenv()

from storage.client import StorageClient, StorageError


def format_size(bytes_size: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} TB"


def format_speed(bytes_per_second: float) -> str:
    """Format bytes per second to human-readable speed."""
    return f"{format_size(bytes_per_second)}/s"


def format_time(seconds: float) -> str:
    """Format seconds to human-readable time."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


class ProgressTracker:
    """Track and display upload progress."""

    def __init__(self, total_size: int):
        self.total_size = total_size
        self.transferred = 0
        self.start_time = time.time()
        self.last_update = time.time()

    def __call__(self, bytes_transferred: int, total_bytes: int):
        """Update progress."""
        self.transferred = bytes_transferred
        current_time = time.time()

        # Update display every 0.5 seconds
        if current_time - self.last_update >= 0.5 or bytes_transferred >= total_bytes:
            elapsed = current_time - self.start_time
            progress = (bytes_transferred / total_bytes) * 100

            # Calculate speed and ETA
            speed = bytes_transferred / elapsed if elapsed > 0 else 0
            remaining_bytes = total_bytes - bytes_transferred
            eta = remaining_bytes / speed if speed > 0 else 0

            # Format progress bar
            bar_width = 40
            filled = int(bar_width * bytes_transferred / total_bytes)
            bar = '█' * filled + '░' * (bar_width - filled)

            # Display progress
            print(
                f"\r[{bar}] {progress:.1f}% | "
                f"{format_size(bytes_transferred)}/{format_size(total_bytes)} | "
                f"{format_speed(speed)} | "
                f"ETA: {format_time(eta)}",
                end='', flush=True
            )
            self.last_update = current_time

    def finish(self):
        """Display final upload summary."""
        elapsed = time.time() - self.start_time
        avg_speed = self.total_size / elapsed if elapsed > 0 else 0
        print()  # New line after progress bar
        print(f"✓ Upload completed in {format_time(elapsed)} (avg: {format_speed(avg_speed)})")


def main():
    """Main upload function."""
    if len(sys.argv) < 2:
        print("Usage: python scripts/upload.py <file_path> [object_key]")
        print("\nExamples:")
        print("  python scripts/upload.py final.mp4")
        print("  python scripts/upload.py /path/to/video.mp4 custom-name.mp4")
        print("  python scripts/upload.py murattal.mp3 live-media/murattal.mp3")
        sys.exit(1)

    file_path = sys.argv[1]
    object_key = sys.argv[2] if len(sys.argv) > 2 else None

    # Validate file exists
    if not os.path.exists(file_path):
        print(f"❌ Error: File not found: {file_path}")
        sys.exit(1)

    # Get file size
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    print(f"📁 File: {filename}")
    print(f"📏 Size: {format_size(file_size)}")
    print(f"🔑 Object Key: {object_key or filename}")
    print()

    try:
        # Initialize storage client
        print("⚙️  Connecting to storage...")
        client = StorageClient()
        print(f"✓ Connected to {client.provider} (bucket: {client.bucket_name})")
        print()

        # Create progress tracker
        progress = ProgressTracker(file_size)

        # Upload file
        print("📤 Uploading...")
        result = client.upload_file(
            file_path=file_path,
            object_key=object_key,
            progress_callback=progress
        )

        # Finish progress display
        progress.finish()

        # Display result
        print()
        print("✅ Upload successful!")
        print(f"   Key: {result.key}")
        print(f"   Size: {format_size(result.size)}")
        print(f"   Modified: {result.last_modified}")

        # Generate and display stream URL
        stream_url = client.get_stream_url(result.key)
        print(f"   Stream URL: {stream_url}")

    except StorageError as e:
        print()
        print(f"❌ Storage Error: {e}")
        print("\nTroubleshooting:")
        print("1. Check .env file has correct credentials")
        print("2. Verify bucket exists: " + (client.bucket_name if 'client' in locals() else "<check .env>"))
        print("3. Test connection:")
        if 'client' in locals() and client.provider == "cloudflare":
            r2_account = os.getenv("R2_ENDPOINT", "").split("//")[1].split(".")[0] if os.getenv("R2_ENDPOINT") else "<account-id>"
            print(f"   aws s3 ls s3://{client.bucket_name} --endpoint-url=https://{r2_account}.r2.cloudflarestorage.com")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        print("\n⚠️  Upload cancelled by user")
        sys.exit(1)
    except Exception as e:
        print()
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
