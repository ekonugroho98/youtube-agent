"""Unit tests for storage client."""

import os
from unittest.mock import Mock, patch, MagicMock
import pytest

from storage import StorageClient, MediaFile, StorageAuthError, StorageConnectionError, StorageNotFoundError


@pytest.fixture
def mock_env_vars():
    """Set up required environment variables for testing."""
    env = {
        'STORAGE_PROVIDER': 'aws',
        'STORAGE_BUCKET': 'test-bucket',
        'STORAGE_ACCESS_KEY_ID': 'test-key',
        'STORAGE_SECRET_ACCESS_KEY': 'test-secret',
        'STORAGE_REGION': 'us-east-1',
    }
    with patch.dict(os.environ, env, clear=True):
        yield env


@pytest.fixture
def storage_client(mock_env_vars):
    """Create storage client with mocked boto3."""
    with patch('storage.client.boto3') as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        mock_boto3.resource.return_value = MagicMock()

        client = StorageClient()
        client.client = mock_s3
        return client


class TestStorageClientInit:
    """Tests for StorageClient initialization."""

    def test_init_with_all_required_vars(self, mock_env_vars):
        """Should initialize successfully with all required environment variables."""
        with patch('storage.client.boto3'):
            client = StorageClient()
            assert client.bucket_name == 'test-bucket'
            assert client.access_key == 'test-key'
            assert client.secret_key == 'test-secret'
            assert client.provider == 'aws'

    def test_init_missing_bucket(self, mock_env_vars):
        """Should raise StorageAuthError when STORAGE_BUCKET is missing."""
        env_missing_bucket = mock_env_vars.copy()
        env_missing_bucket.pop('STORAGE_BUCKET')

        with patch.dict(os.environ, env_missing_bucket, clear=True):
            with pytest.raises(StorageAuthError) as exc:
                StorageClient()

            assert 'STORAGE_BUCKET' in str(exc.value)

    def test_init_missing_access_key(self, mock_env_vars):
        """Should raise StorageAuthError when STORAGE_ACCESS_KEY_ID is missing."""
        env_missing_key = mock_env_vars.copy()
        env_missing_key.pop('STORAGE_ACCESS_KEY_ID')

        with patch.dict(os.environ, env_missing_key, clear=True):
            with pytest.raises(StorageAuthError) as exc:
                StorageClient()

            assert 'STORAGE_ACCESS_KEY_ID' in str(exc.value)


class TestListMedia:
    """Tests for list_media() method."""

    def test_list_media_filters_by_extension(self, storage_client):
        """Should only return files with media extensions."""
        mock_bucket = MagicMock()
        mock_objects = [
            MagicMock(key='video.mp4', size=1000, last_modified='2024-01-01'),
            MagicMock(key='video.mkv', size=2000, last_modified='2024-01-02'),
            MagicMock(key='video.mov', size=3000, last_modified='2024-01-03'),
            MagicMock(key='document.pdf', size=500, last_modified='2024-01-04'),  # Should be filtered
            MagicMock(key='image.jpg', size=250, last_modified='2024-01-05'),  # Should be filtered
        ]
        mock_bucket.objects.all.return_value = mock_objects
        storage_client.resource.Bucket.return_value = mock_bucket

        result = storage_client.list_media()

        assert len(result) == 3  # Only media files
        assert all(isinstance(f, MediaFile) for f in result)
        assert [f.key for f in result] == ['video.mp4', 'video.mkv', 'video.mov']

    def test_list_media_empty_bucket(self, storage_client):
        """Should return empty list when no media files exist."""
        mock_bucket = MagicMock()
        mock_bucket.objects.all.return_value = []
        storage_client.resource.Bucket.return_value = mock_bucket

        result = storage_client.list_media()

        assert result == []


class TestGetStreamUrl:
    """Tests for get_stream_url() method."""

    def test_get_stream_url_generates_signed_url(self, storage_client):
        """Should generate presigned URL with 24 hour expiry."""
        storage_client.client.generate_presigned_url.return_value = (
            'https://example.com/video.mp4?signature=abc123'
        )

        url = storage_client.get_stream_url('video.mp4')

        assert url == 'https://example.com/video.mp4?signature=abc123'
        storage_client.client.generate_presigned_url.assert_called_once()
        call_args = storage_client.client.generate_presigned_url.call_args
        assert call_args[0][0] == 'get_object'
        assert call_args[1]['Params']['Bucket'] == 'test-bucket'
        assert call_args[1]['Params']['Key'] == 'video.mp4'
        assert call_args[1]['ExpiresIn'] == 24 * 60 * 60  # 24 hours


class TestErrorHandling:
    """Tests for error handling."""

    @patch('storage.client.boto3')
    def test_auth_error_credential_failure(self, mock_boto3, mock_env_vars):
        """Should raise StorageAuthError when credentials are invalid."""
        from botocore.exceptions import NoCredentialsError

        mock_boto3.exceptions.NoCredentialsError = NoCredentialsError
        mock_boto3.client.side_effect = NoCredentialsError()

        with pytest.raises(StorageAuthError):
            StorageClient()

    @patch('storage.client.boto3')
    def test_bucket_not_found(self, mock_boto3, mock_env_vars):
        """Should raise StorageNotFoundError when bucket doesn't exist."""
        from botocore.exceptions import ClientError

        mock_boto3.exceptions.ClientError = ClientError
        error_response = {
            'Error': {'Code': 'NoSuchBucket', 'Message': 'Bucket not found'}
        }
        mock_boto3.client.side_effect = ClientError(error_response, 'HeadBucket')

        with pytest.raises(StorageNotFoundError) as exc:
            client = StorageClient()
            client.list_media()

        assert 'not found' in str(exc.value).lower()
