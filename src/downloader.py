"""Google Drive video downloader with retry logic."""

import os
import io
import time
from pathlib import Path
from typing import Optional, Callable

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .utils import (
    get_config,
    setup_logging,
    get_temp_dir,
    extract_file_id_from_drive_url,
    clean_filename,
    format_file_size
)

logger = setup_logging('downloader')


class DownloadError(Exception):
    """Exception raised when download fails."""
    pass


class DriveDownloader:
    """Downloads videos from Google Drive with retry logic."""

    def __init__(self, credentials: Optional[Credentials] = None):
        """Initialize downloader."""
        self.credentials = credentials
        self._service = None
        self.config = get_config()

    @property
    def service(self):
        """Get or create Drive API service."""
        if self._service is None and self.credentials:
            self._service = build('drive', 'v3', credentials=self.credentials)
        return self._service

    def download_from_url(
        self,
        drive_url: str,
        output_path: Optional[Path] = None,
        progress_callback: Optional[Callable[[float], None]] = None,
        max_retries: int = 3
    ) -> Path:
        """
        Download a video from a Google Drive URL.

        Args:
            drive_url: Google Drive link (various formats supported)
            output_path: Where to save the file (auto-generated if not provided)
            progress_callback: Optional callback for progress updates (0.0 to 1.0)
            max_retries: Maximum number of retry attempts

        Returns:
            Path to the downloaded file

        Raises:
            DownloadError: If download fails after all retries
        """
        file_id = extract_file_id_from_drive_url(drive_url)

        if not file_id:
            raise DownloadError(f"Could not extract file ID from URL: {drive_url}")

        # Try Drive API first if we have credentials
        if self.service:
            try:
                return self._download_via_api(
                    file_id, output_path, progress_callback, max_retries
                )
            except Exception as e:
                logger.warning(f"Drive API download failed, falling back to direct: {e}")

        # Fall back to direct download
        return self._download_direct(
            file_id, output_path, progress_callback, max_retries
        )

    def _download_via_api(
        self,
        file_id: str,
        output_path: Optional[Path],
        progress_callback: Optional[Callable[[float], None]],
        max_retries: int
    ) -> Path:
        """Download using Google Drive API."""
        # Get file metadata
        file_metadata = self.service.files().get(
            fileId=file_id,
            fields='name,mimeType,size'
        ).execute()

        filename = file_metadata.get('name', f'{file_id}.mp4')
        file_size = int(file_metadata.get('size', 0))

        if output_path is None:
            output_path = get_temp_dir() / clean_filename(filename)

        logger.info(f"Downloading: {filename} ({format_file_size(file_size)})")

        # Create request for file content
        request = self.service.files().get_media(fileId=file_id)

        for attempt in range(max_retries):
            try:
                # Download with progress tracking
                fh = io.FileIO(output_path, 'wb')
                downloader = MediaIoBaseDownload(fh, request)

                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status and progress_callback:
                        progress_callback(status.progress())

                fh.close()

                # Verify file was downloaded
                if output_path.exists() and output_path.stat().st_size > 0:
                    logger.info(f"Downloaded: {output_path} ({format_file_size(output_path.stat().st_size)})")
                    return output_path

            except Exception as e:
                logger.warning(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    raise DownloadError(f"Download failed after {max_retries} attempts: {e}")

        raise DownloadError("Download failed: unknown error")

    def _download_direct(
        self,
        file_id: str,
        output_path: Optional[Path],
        progress_callback: Optional[Callable[[float], None]],
        max_retries: int
    ) -> Path:
        """Download directly using requests (for publicly shared files)."""
        # Build direct download URL
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        if output_path is None:
            output_path = get_temp_dir() / f"{file_id}.mp4"

        for attempt in range(max_retries):
            try:
                session = requests.Session()

                # Initial request
                response = session.get(download_url, stream=True)
                response.raise_for_status()

                # Check for confirmation page (large files)
                if 'text/html' in response.headers.get('Content-Type', ''):
                    # Look for confirmation token
                    confirm_token = self._get_confirm_token(response)
                    if confirm_token:
                        download_url = f"{download_url}&confirm={confirm_token}"
                        response = session.get(download_url, stream=True)
                        response.raise_for_status()

                # Get file size if available
                file_size = int(response.headers.get('Content-Length', 0))

                # Download with progress
                downloaded = 0
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=32768):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and file_size:
                                progress_callback(downloaded / file_size)

                # Verify download
                if output_path.exists() and output_path.stat().st_size > 0:
                    logger.info(f"Downloaded: {output_path} ({format_file_size(output_path.stat().st_size)})")
                    return output_path

                raise DownloadError("Downloaded file is empty")

            except requests.RequestException as e:
                logger.warning(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    raise DownloadError(f"Download failed after {max_retries} attempts: {e}")

        raise DownloadError("Download failed: unknown error")

    def _get_confirm_token(self, response: requests.Response) -> Optional[str]:
        """Extract confirmation token from Google Drive warning page."""
        for key, value in response.cookies.items():
            if key.startswith('download_warning'):
                return value

        # Try to find token in response text
        import re
        match = re.search(r'confirm=([0-9A-Za-z_-]+)', response.text)
        if match:
            return match.group(1)

        # New format token
        match = re.search(r'name="uuid" value="([^"]+)"', response.text)
        if match:
            return match.group(1)

        return None

    def cleanup_temp_file(self, file_path: Path) -> bool:
        """Remove a temporary file."""
        try:
            if file_path.exists():
                file_path.unlink()
                logger.debug(f"Cleaned up temp file: {file_path}")
                return True
        except Exception as e:
            logger.warning(f"Failed to clean up temp file {file_path}: {e}")
        return False

    def cleanup_all_temp_files(self) -> int:
        """Remove all files from the temp directory."""
        temp_dir = get_temp_dir()
        count = 0

        for file_path in temp_dir.iterdir():
            if file_path.is_file():
                try:
                    file_path.unlink()
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete {file_path}: {e}")

        logger.info(f"Cleaned up {count} temp files")
        return count
