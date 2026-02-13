"""
S3-compatible storage client for media operations.

Supports Cloudflare R2, AWS S3, and Google Cloud Storage via S3 API.
"""
import os
import logging
from datetime import timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass

import boto3
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
    EndpointConnectionError,
    BotoCoreError,
)


logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base storage error."""
    pass


class StorageConnectionError(StorageError):
    """Storage provider is unreachable or connection failed."""
    pass


class StorageAuthError(StorageError):
    """Storage authentication failed."""
    pass


class StorageNotFoundError(StorageError):
    """Storage resource not found (bucket, object)."""
    pass


@dataclass
class MediaFile:
    """Media file metadata from storage."""
    key: str
    size: int
    last_modified: str


class StorageClient:
    """S3-compatible storage client."""

    # Media file extensions to filter
    MEDIA_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.flv', '.webm'}

    # Signed URL expiry (24 hours)
    URL_EXPIRY_SECONDS = 24 * 60 * 60

    # Cloudflare R2 endpoint
    R2_ENDPOINT = "https://<accountid>.r2.cloudflarestorage.com"

    def __init__(self):
        """Initialize storage client from environment variables."""
        self.provider = os.getenv("STORAGE_PROVIDER", "cloudflare")
        self.bucket_name = os.getenv("STORAGE_BUCKET")
        self.access_key = os.getenv("STORAGE_ACCESS_KEY_ID")
        self.secret_key = os.getenv("STORAGE_SECRET_ACCESS_KEY")
        self.region = os.getenv("STORAGE_REGION", "auto")

        # Validate required credentials
        if not all([self.bucket_name, self.access_key, self.secret_key]):
            missing = []
            if not self.bucket_name:
                missing.append("STORAGE_BUCKET")
            if not self.access_key:
                missing.append("STORAGE_ACCESS_KEY_ID")
            if not self.secret_key:
                missing.append("STORAGE_SECRET_ACCESS_KEY")
            raise StorageAuthError(
                f"Missing required storage credentials: {', '.join(missing)}. "
                "Check environment variables."
            )

        # Configure boto3 client
        self._init_client()

    def _init_client(self):
        """Initialize boto3 client based on provider."""
        config = {
            'aws_access_key_id': self.access_key,
            'aws_secret_access_key': self.secret_key,
        }

        if self.provider == "cloudflare":
            # Cloudflare R2 uses account-specific endpoint
            # User should set R2_ENDPOINT or use default
            endpoint = os.getenv("R2_ENDPOINT")
            if endpoint:
                config['endpoint_url'] = endpoint
            logger.info(f"Initialized Cloudflare R2 client for bucket: {self.bucket_name}")
        elif self.provider == "aws":
            config['region_name'] = self.region
            logger.info(f"Initialized AWS S3 client for bucket: {self.bucket_name} (region: {self.region})")
        elif self.provider == "gcs":
            # GCS interoperability mode
            endpoint = os.getenv("GCS_ENDPOINT", "https://storage.googleapis.com")
            config['endpoint_url'] = endpoint
            config['region_name'] = self.region if self.region != "auto" else "us"
            logger.info(f"Initialized GCS client for bucket: {self.bucket_name}")
        else:
            raise ValueError(f"Unknown storage provider: {self.provider}")

        try:
            self.client = boto3.client('s3', **config)
            self.resource = boto3.resource('s3', **config)
        except Exception as e:
            raise StorageConnectionError(
                f"Failed to initialize storage client: {str(e)}"
            )

    def list_media(self) -> List[MediaFile]:
        """
        List all media files in the configured bucket.

        Returns:
            List of MediaFile objects with metadata

        Raises:
            StorageConnectionError: Connection failed
            StorageAuthError: Authentication failed
            StorageNotFoundError: Bucket not found
        """
        try:
            bucket = self.resource.Bucket(self.bucket_name)
            media_files = []

            for obj in bucket.objects.all():
                # Filter by file extension
                _, ext = os.path.splitext(obj.key)
                if ext.lower() in self.MEDIA_EXTENSIONS:
                    media_files.append(MediaFile(
                        key=obj.key,
                        size=obj.size,
                        last_modified=obj.last_modified.isoformat()
                    ))

            logger.info(f"Found {len(media_files)} media files in bucket")
            return media_files

        except NoCredentialsError:
            raise StorageAuthError(
                "Storage credentials not found. Check STORAGE_ACCESS_KEY_ID and "
                "STORAGE_SECRET_ACCESS_KEY environment variables."
            )
        except PartialCredentialsError:
            raise StorageAuthError(
                "Incomplete storage credentials. Check both access key and secret key."
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucket':
                raise StorageNotFoundError(
                    f"Bucket '{self.bucket_name}' not found. Check STORAGE_BUCKET."
                )
            raise StorageConnectionError(f"Storage client error: {str(e)}")
        except (EndpointConnectionError, BotoCoreError) as e:
            raise StorageConnectionError(
                f"Failed to connect to storage provider: {str(e)}"
            )

    def get_stream_url(self, media_key: str) -> str:
        """
        Generate a signed URL for streaming media.

        Args:
            media_key: Object key in storage bucket

        Returns:
            Signed URL valid for 24 hours

        Raises:
            StorageConnectionError: URL generation failed
        """
        try:
            url = self.client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': media_key,
                },
                ExpiresIn=self.URL_EXPIRY_SECONDS
            )
            logger.debug(f"Generated signed URL for {media_key} (expires in 24h)")
            return url

        except ClientError as e:
            raise StorageConnectionError(
                f"Failed to generate signed URL for '{media_key}': {str(e)}"
            )
