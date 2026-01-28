"""YouTube video upload engine with resumable uploads and retry logic."""

import time
import httplib2
from pathlib import Path
from typing import Optional, Callable, Tuple
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from .metadata import VideoMetadata
from .utils import setup_logging, format_file_size

logger = setup_logging('uploader')

# Retry configuration
MAX_RETRIES = 3
RETRY_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, HttpError)
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]


@dataclass
class UploadResult:
    """Result of an upload attempt."""
    success: bool
    video_id: Optional[str]
    video_url: Optional[str]
    error_message: Optional[str]
    quota_used: int  # Estimated quota units used

    @property
    def shorts_url(self) -> Optional[str]:
        """Get the YouTube Shorts URL."""
        if self.video_id:
            return f"https://youtube.com/shorts/{self.video_id}"
        return None


class UploadError(Exception):
    """Exception raised when upload fails."""
    pass


class QuotaExceededError(Exception):
    """Exception raised when YouTube API quota is exceeded."""
    pass


class YouTubeUploader:
    """Handles YouTube video uploads with resumable upload support."""

    def __init__(self, credentials: Credentials):
        """Initialize uploader with credentials."""
        self.credentials = credentials
        self._service = None

    @property
    def service(self):
        """Get or create YouTube API service."""
        if self._service is None:
            self._service = build('youtube', 'v3', credentials=self.credentials)
        return self._service

    def upload_video(
        self,
        video_path: Path,
        metadata: VideoMetadata,
        progress_callback: Optional[Callable[[float], None]] = None
    ) -> UploadResult:
        """
        Upload a video to YouTube.

        Args:
            video_path: Path to the video file
            metadata: Video metadata (title, description, tags, etc.)
            progress_callback: Optional callback for progress updates (0.0 to 1.0)

        Returns:
            UploadResult with video ID and URL on success

        Raises:
            QuotaExceededError: If API quota is exceeded
            UploadError: If upload fails after all retries
        """
        if not video_path.exists():
            return UploadResult(
                success=False,
                video_id=None,
                video_url=None,
                error_message=f"Video file not found: {video_path}",
                quota_used=0
            )

        file_size = video_path.stat().st_size
        logger.info(f"Uploading: {video_path.name} ({format_file_size(file_size)})")

        # Prepare request body
        body = metadata.to_youtube_body()

        # Create media upload object with resumable upload
        media = MediaFileUpload(
            str(video_path),
            mimetype='video/mp4',
            resumable=True,
            chunksize=256 * 1024  # 256KB chunks
        )

        # Create upload request
        request = self.service.videos().insert(
            part='snippet,status',
            body=body,
            media_body=media
        )

        # Perform resumable upload with retries
        return self._resumable_upload(request, progress_callback)

    def _resumable_upload(
        self,
        request,
        progress_callback: Optional[Callable[[float], None]]
    ) -> UploadResult:
        """
        Execute a resumable upload with retry logic.

        Implements exponential backoff on transient failures.
        """
        response = None
        retry_count = 0
        quota_used = 1600  # Base cost for upload

        while response is None:
            try:
                status, response = request.next_chunk()

                if status:
                    progress = status.progress()
                    logger.debug(f"Upload progress: {int(progress * 100)}%")
                    if progress_callback:
                        progress_callback(progress)

            except HttpError as e:
                error_content = e.content.decode() if e.content else str(e)

                # Check for quota exceeded
                if e.resp.status == 403:
                    if 'quotaExceeded' in error_content or 'quota' in error_content.lower():
                        logger.error("YouTube API quota exceeded")
                        raise QuotaExceededError("Daily upload quota exceeded")

                    return UploadResult(
                        success=False,
                        video_id=None,
                        video_url=None,
                        error_message=f"Permission denied: {error_content}",
                        quota_used=quota_used
                    )

                # Check for bad request (invalid video, etc.)
                if e.resp.status == 400:
                    return UploadResult(
                        success=False,
                        video_id=None,
                        video_url=None,
                        error_message=f"Invalid request: {error_content}",
                        quota_used=quota_used
                    )

                # Check if error is retriable
                if e.resp.status in RETRIABLE_STATUS_CODES:
                    retry_count, should_continue = self._handle_retry(
                        retry_count, f"HTTP {e.resp.status}"
                    )
                    if not should_continue:
                        return UploadResult(
                            success=False,
                            video_id=None,
                            video_url=None,
                            error_message=f"Upload failed after {MAX_RETRIES} retries: {error_content}",
                            quota_used=quota_used
                        )
                else:
                    return UploadResult(
                        success=False,
                        video_id=None,
                        video_url=None,
                        error_message=f"HTTP error {e.resp.status}: {error_content}",
                        quota_used=quota_used
                    )

            except RETRY_EXCEPTIONS as e:
                retry_count, should_continue = self._handle_retry(
                    retry_count, str(e)
                )
                if not should_continue:
                    return UploadResult(
                        success=False,
                        video_id=None,
                        video_url=None,
                        error_message=f"Upload failed after {MAX_RETRIES} retries: {e}",
                        quota_used=quota_used
                    )

        # Upload successful
        if response:
            video_id = response.get('id')
            video_url = f"https://www.youtube.com/watch?v={video_id}"

            logger.info(f"Upload successful: {video_url}")

            if progress_callback:
                progress_callback(1.0)

            return UploadResult(
                success=True,
                video_id=video_id,
                video_url=video_url,
                error_message=None,
                quota_used=quota_used
            )

        return UploadResult(
            success=False,
            video_id=None,
            video_url=None,
            error_message="Upload completed but no response received",
            quota_used=quota_used
        )

    def _handle_retry(
        self,
        retry_count: int,
        error_description: str
    ) -> Tuple[int, bool]:
        """
        Handle retry logic with exponential backoff.

        Returns:
            Tuple of (new_retry_count, should_continue)
        """
        if retry_count >= MAX_RETRIES:
            return retry_count, False

        retry_count += 1
        wait_time = 2 ** retry_count  # 2, 4, 8 seconds

        logger.warning(
            f"Retriable error ({error_description}), "
            f"retry {retry_count}/{MAX_RETRIES} in {wait_time}s"
        )
        time.sleep(wait_time)

        return retry_count, True

    def get_video_info(self, video_id: str) -> Optional[dict]:
        """Get information about an uploaded video."""
        try:
            response = self.service.videos().list(
                part='snippet,status,statistics',
                id=video_id
            ).execute()

            if response.get('items'):
                return response['items'][0]
            return None

        except HttpError as e:
            logger.error(f"Failed to get video info: {e}")
            return None

    def delete_video(self, video_id: str) -> bool:
        """Delete a video (use with caution)."""
        try:
            self.service.videos().delete(id=video_id).execute()
            logger.info(f"Deleted video: {video_id}")
            return True
        except HttpError as e:
            logger.error(f"Failed to delete video: {e}")
            return False


def estimate_quota_usage(num_uploads: int) -> dict:
    """
    Estimate YouTube API quota usage for uploads.

    YouTube API quota costs:
    - videos.insert: 1,600 units
    - videos.list: 1 unit
    - channels.list: 1 unit

    Default daily quota: 10,000 units
    """
    upload_cost = 1600 * num_uploads
    overhead = 10  # For listing channels, etc.
    total = upload_cost + overhead

    return {
        'upload_cost': upload_cost,
        'overhead': overhead,
        'total': total,
        'daily_quota': 10000,
        'remaining': max(0, 10000 - total),
        'percent_used': min(100, (total / 10000) * 100)
    }
