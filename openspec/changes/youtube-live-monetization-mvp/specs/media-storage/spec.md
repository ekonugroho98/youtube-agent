# Spec: Media Storage

## ADDED Requirements

### Requirement: S3-compatible storage abstraction
The system SHALL provide S3-compatible API abstraction for object storage operations.

#### Scenario: Initialize storage client
- **WHEN** system starts
- **THEN** system initializes storage client from environment configuration:
  - STORAGE_PROVIDER: "cloudflare" or "aws"
  - STORAGE_BUCKET: bucket name
  - STORAGE_ACCESS_KEY_ID: access credentials
  - STORAGE_SECRET_ACCESS_KEY: secret credentials
  - STORAGE_REGION: region or "auto"
- **AND** validates all required fields are present
- **AND** fails to start if any required field is missing

#### Scenario: Connect to Cloudflare R2
- **WHEN** STORAGE_PROVIDER is "cloudflare"
- **THEN** system uses S3-compatible endpoint for R2
- **AND** configures boto3 client with R2 endpoint

#### Scenario: Connect to AWS S3
- **WHEN** STORAGE_PROVIDER is "aws"
- **THEN** system uses standard AWS S3 endpoint
- **AND** configures boto3 client with specified region

### Requirement: List available media
The system SHALL list all media files available in the configured storage bucket.

#### Scenario: List media files
- **WHEN** `list_media()` is called
- **THEN** system queries storage bucket for objects
- **AND** filters to media file extensions (.mp4, .mkv, .mov, .avi)
- **AND** returns list of media keys with metadata:
  - key: object key/path in bucket
  - size: file size in bytes
  - last_modified: ISO 8601 timestamp

#### Scenario: Empty bucket
- **WHEN** `list_media()` is called and bucket contains no media files
- **THEN** system returns empty list
- **AND** logs info message about empty bucket

#### Scenario: Storage connection error
- **WHEN** storage provider is unreachable or credentials are invalid
- **THEN** system raises StorageConnectionError
- **AND** includes underlying error details
- **AND** caller handles error gracefully

### Requirement: Generate signed streaming URLs
The system SHALL generate time-limited signed URLs for direct media streaming from storage.

#### Scenario: Generate stream URL
- **WHEN** `get_stream_url(media_key)` is called
- **THEN** system generates presigned URL for the media object
- **AND** URL expires after 24 hours
- **AND** URL is valid for HTTP GET requests
- **AND** URL allows direct streaming without download

#### Scenario: Invalid media key
- **WHEN** `get_stream_url()` is called with non-existent key
- **THEN** system returns URL that will return 404 when accessed
- **AND** does NOT validate key existence at generation time
- **AND** allows storage provider to return 404 at access time

#### Scenario: URL expiration
- **WHEN** signed URL expires after 24 hours
- **THEN** URL no longer grants access to media
- **AND** storage provider returns 403 Forbidden
- **AND** worker must request new URL from controller

### Requirement: Storage provider compatibility
The system SHALL support multiple storage providers via S3-compatible interface.

#### Scenario: Switch from R2 to S3
- **WHEN** STORAGE_PROVIDER is changed from "cloudflare" to "aws"
- **THEN** system reinitializes client with S3 endpoint
- **AND** all other operations remain identical
- **AND** no code changes required

#### Scenario: Use Google Cloud Storage
- **WHEN** STORAGE_PROVIDER is set to "gcs" with GCS-compatible endpoint
- **THEN** system uses GCS interoperability mode with S3 client
- **AND** operations work identically to R2/S3

### Requirement: Credential security
The system SHALL load storage credentials from environment variables only.

#### Scenario: Load credentials on startup
- **WHEN** system initializes storage client
- **THEN** system reads credentials from environment variables
- **AND** never writes credentials to files or logs
- **AND** never includes credentials in error messages

#### Scenario: Missing credentials
- **WHEN** required environment variables are not set
- **THEN** system fails to start
- **AND** logs error indicating which credential is missing
- **AND** does NOT reveal partial credential values

### Requirement: Error handling
The system SHALL provide clear error types for storage operations.

#### Scenario: Connection timeout
- **WHEN** storage provider is unreachable
- **THEN** system raises StorageConnectionError
- **AND** includes timeout duration and endpoint

#### Scenario: Authentication failure
- **WHEN** storage provider rejects credentials
- **THEN** system raises StorageAuthError
- **AND** does NOT log credential values
- **AND** suggests checking environment variables

#### Scenario: Bucket not found
- **WHEN** specified bucket does not exist
- **THEN** system raises StorageNotFoundError
- **AND** includes bucket name in error message
