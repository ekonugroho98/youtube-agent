"""
YouTube Data API v3 client for monitoring live streams.

Uses API key (read-only) to:
- Detect active live streams on a channel
- Get concurrent viewer count
- Get video statistics (likes, views)
- Check YouTube live broadcast status

NOTE: API key only supports read operations.
Write operations (create/transition broadcasts) require OAuth 2.0.
"""
import asyncio
import logging
from typing import Optional, Dict, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class YouTubeAPIError(Exception):
    """Base YouTube API error."""
    pass


class YouTubeQuotaError(YouTubeAPIError):
    """YouTube API quota exceeded."""
    pass


class YouTubeAPIClient:
    """
    YouTube Data API v3 client (API key, read-only).

    Provides monitoring capabilities for live streams:
    - Find active live broadcast on a channel
    - Get concurrent viewer count
    - Get video statistics
    """

    def __init__(self, api_key: str, channel_id: Optional[str] = None):
        """
        Initialize YouTube API client.

        Args:
            api_key: YouTube Data API v3 key
            channel_id: YouTube channel ID (optional, for auto-detecting live streams)
        """
        self.api_key = api_key
        self.channel_id = channel_id
        self._youtube = None
        self._build_service()

    def _build_service(self) -> None:
        """Build the YouTube API service client."""
        try:
            self._youtube = build('youtube', 'v3', developerKey=self.api_key)
            logger.info("YouTube API service built successfully")
        except Exception as e:
            logger.error(f"Failed to build YouTube API service: {e}")
            raise YouTubeAPIError(f"Failed to initialize YouTube API: {e}")

    async def find_active_live_stream(self) -> Optional[Dict[str, Any]]:
        """
        Find the currently active live stream on the configured channel.

        Uses search.list with eventType=live to find active broadcasts.

        Returns:
            Dict with video_id, title, description, or None if no live stream found
        """
        if not self.channel_id:
            logger.warning("No channel_id configured, cannot search for live streams")
            return None

        try:
            def _search():
                request = self._youtube.search().list(
                    part='snippet',
                    channelId=self.channel_id,
                    eventType='live',
                    type='video',
                    maxResults=1
                )
                return request.execute()

            response = await asyncio.to_thread(_search)

            items = response.get('items', [])
            if not items:
                return None

            item = items[0]
            return {
                'video_id': item['id']['videoId'],
                'title': item['snippet']['title'],
                'description': item['snippet']['description'],
                'channel_title': item['snippet']['channelTitle'],
                'published_at': item['snippet']['publishedAt'],
                'thumbnail': item['snippet']['thumbnails'].get('high', {}).get('url', ''),
            }

        except HttpError as e:
            if e.resp.status == 403 and 'quotaExceeded' in str(e):
                raise YouTubeQuotaError("YouTube API quota exceeded")
            logger.error(f"YouTube search API error: {e}")
            raise YouTubeAPIError(f"Failed to search live streams: {e}")
        except Exception as e:
            logger.error(f"Failed to find live stream: {e}")
            raise YouTubeAPIError(f"Failed to find live stream: {e}")

    async def get_video_details(self, video_id: str) -> Optional[Dict[str, Any]]:
        """
        Get video details including live streaming info and statistics.

        Args:
            video_id: YouTube video ID

        Returns:
            Dict with viewer count, like count, status info, or None if not found
        """
        try:
            def _get():
                request = self._youtube.videos().list(
                    part='snippet,statistics,liveStreamingDetails',
                    id=video_id
                )
                return request.execute()

            response = await asyncio.to_thread(_get)

            items = response.get('items', [])
            if not items:
                return None

            item = items[0]
            snippet = item.get('snippet', {})
            stats = item.get('statistics', {})
            live_details = item.get('liveStreamingDetails', {})

            return {
                'video_id': video_id,
                'title': snippet.get('title', ''),
                'live_broadcast_content': snippet.get('liveBroadcastContent', 'none'),
                'concurrent_viewers': _safe_int(live_details.get('concurrentViewers')),
                'actual_start_time': live_details.get('actualStartTime'),
                'actual_end_time': live_details.get('actualEndTime'),
                'scheduled_start_time': live_details.get('scheduledStartTime'),
                'view_count': _safe_int(stats.get('viewCount')),
                'like_count': _safe_int(stats.get('likeCount')),
                'comment_count': _safe_int(stats.get('commentCount')),
            }

        except HttpError as e:
            if e.resp.status == 403 and 'quotaExceeded' in str(e):
                raise YouTubeQuotaError("YouTube API quota exceeded")
            logger.error(f"YouTube videos API error: {e}")
            raise YouTubeAPIError(f"Failed to get video details: {e}")
        except Exception as e:
            logger.error(f"Failed to get video details: {e}")
            raise YouTubeAPIError(f"Failed to get video details: {e}")

    async def get_live_status(self) -> Dict[str, Any]:
        """
        Get complete live stream status for the configured channel.

        Combines search + video details for full picture.

        Returns:
            Dict with is_live, video_id, viewers, likes, title, etc.
        """
        result = {
            'is_live': False,
            'video_id': None,
            'title': None,
            'concurrent_viewers': None,
            'view_count': None,
            'like_count': None,
            'comment_count': None,
            'actual_start_time': None,
            'thumbnail': None,
        }

        # Find active live stream
        live_stream = await self.find_active_live_stream()
        if not live_stream:
            return result

        result['is_live'] = True
        result['video_id'] = live_stream['video_id']
        result['title'] = live_stream['title']
        result['thumbnail'] = live_stream['thumbnail']

        # Get detailed stats
        details = await self.get_video_details(live_stream['video_id'])
        if details:
            result['concurrent_viewers'] = details['concurrent_viewers']
            result['view_count'] = details['view_count']
            result['like_count'] = details['like_count']
            result['comment_count'] = details['comment_count']
            result['actual_start_time'] = details['actual_start_time']

        return result

    async def get_viewer_count(self, video_id: str) -> Optional[int]:
        """
        Get concurrent viewer count for a specific video.

        Args:
            video_id: YouTube video ID

        Returns:
            Concurrent viewer count, or None if unavailable
        """
        details = await self.get_video_details(video_id)
        if details:
            return details.get('concurrent_viewers')
        return None

    async def validate_api_key(self) -> bool:
        """
        Validate the API key by making a simple API call.

        Returns:
            True if API key is valid
        """
        try:
            def _validate():
                request = self._youtube.videos().list(
                    part='id',
                    id='dQw4w9WgXcQ'  # A well-known public video
                )
                return request.execute()

            response = await asyncio.to_thread(_validate)
            return len(response.get('items', [])) > 0
        except HttpError as e:
            if e.resp.status in (400, 403):
                logger.error(f"YouTube API key validation failed: {e}")
                return False
            raise
        except Exception as e:
            logger.error(f"YouTube API key validation error: {e}")
            return False

    async def validate_channel_id(self, channel_id: str) -> Optional[Dict[str, str]]:
        """
        Validate a channel ID and return channel info.

        Args:
            channel_id: YouTube channel ID to validate

        Returns:
            Dict with channel title and thumbnail, or None if invalid
        """
        try:
            def _validate():
                request = self._youtube.channels().list(
                    part='snippet',
                    id=channel_id
                )
                return request.execute()

            response = await asyncio.to_thread(_validate)

            items = response.get('items', [])
            if not items:
                return None

            snippet = items[0].get('snippet', {})
            return {
                'channel_id': channel_id,
                'title': snippet.get('title', ''),
                'thumbnail': snippet.get('thumbnails', {}).get('default', {}).get('url', ''),
            }

        except HttpError as e:
            logger.error(f"Channel validation failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Channel validation error: {e}")
            return None


def _safe_int(value) -> Optional[int]:
    """Safely convert a value to int."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
