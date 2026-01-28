"""Google Sheets integration for reading video queues and updating status."""

from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .utils import get_config, setup_logging, parse_duration

logger = setup_logging('sheets')

# Expected column mapping (0-indexed)
COLUMNS = {
    'post_id': 0,        # A: Post ID
    'video_link': 1,     # B: Video Drive Link
    'views': 2,          # C: Views
    'likes': 3,          # D: Likes
    'comments': 4,       # E: Comments
    'caption': 5,        # F: Caption
    'post_date': 6,      # G: Post Date
    'duration': 7,       # H: Video Duration
    'thumbnail': 8,      # I: Thumbnail URL
    'original_url': 9,   # J: Original Post URL
    'posted_to_yt': 10,  # K: Posted to YT
    'yt_video_id': 11,   # L: YT Video ID (added by us)
    'yt_upload_date': 12,  # M: YT Upload Date (added by us)
    'upload_status': 13,   # N: Upload Status (added by us)
}


@dataclass
class VideoEntry:
    """Represents a video entry from the spreadsheet."""
    row_number: int  # 1-indexed row number in sheet
    post_id: str
    video_link: str
    views: int
    likes: int
    comments: int
    caption: str
    post_date: str
    duration: float  # seconds
    thumbnail_url: str
    original_url: str
    posted_to_yt: bool
    yt_video_id: Optional[str]
    yt_upload_date: Optional[str]
    upload_status: Optional[str]

    @property
    def is_short(self) -> bool:
        """Check if this video qualifies as a YouTube Short (< 60 seconds)."""
        config = get_config()
        max_duration = config['defaults']['max_video_duration']
        return self.duration < max_duration

    @property
    def is_pending(self) -> bool:
        """Check if this video is pending upload."""
        return not self.posted_to_yt and self.video_link


class SheetsManager:
    """Manages Google Sheets operations for video queues."""

    def __init__(self, credentials: Credentials, sheet_id: str):
        """Initialize Sheets manager."""
        self.credentials = credentials
        self.sheet_id = sheet_id
        self._service = None
        self._sheet_name = None

    @property
    def service(self):
        """Get or create Sheets API service."""
        if self._service is None:
            self._service = build('sheets', 'v4', credentials=self.credentials)
        return self._service

    def _get_sheet_name(self) -> str:
        """Get the name of the first sheet in the spreadsheet."""
        if self._sheet_name is None:
            response = self.service.spreadsheets().get(
                spreadsheetId=self.sheet_id,
                fields='sheets.properties.title'
            ).execute()

            if response.get('sheets'):
                self._sheet_name = response['sheets'][0]['properties']['title']
            else:
                self._sheet_name = 'Sheet1'

        return self._sheet_name

    def _ensure_columns_exist(self) -> None:
        """Ensure the additional columns (L, M, N) exist with headers."""
        sheet_name = self._get_sheet_name()

        # Get current headers
        range_name = f"'{sheet_name}'!A1:N1"
        response = self.service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=range_name
        ).execute()

        current_headers = response.get('values', [[]])[0]
        headers_needed = []

        # Check if we need to add our columns
        if len(current_headers) < 12:
            headers_needed.append('YT Video ID')
        if len(current_headers) < 13:
            headers_needed.append('YT Upload Date')
        if len(current_headers) < 14:
            headers_needed.append('Upload Status')

        if headers_needed:
            # Add missing headers
            start_col = len(current_headers)
            col_letter = chr(ord('A') + start_col)
            range_name = f"'{sheet_name}'!{col_letter}1"

            self.service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=range_name,
                valueInputOption='RAW',
                body={'values': [headers_needed]}
            ).execute()

            logger.info(f"Added columns: {headers_needed}")

    def _parse_row(self, row: List[Any], row_number: int) -> VideoEntry:
        """Parse a row of data into a VideoEntry."""
        def get_value(index: int, default: Any = '') -> Any:
            return row[index] if index < len(row) else default

        def parse_bool(value: str) -> bool:
            if isinstance(value, bool):
                return value
            return str(value).upper() in ('TRUE', 'YES', '1', 'DONE')

        def parse_int(value: str) -> int:
            try:
                return int(value) if value else 0
            except (ValueError, TypeError):
                return 0

        return VideoEntry(
            row_number=row_number,
            post_id=str(get_value(COLUMNS['post_id'])),
            video_link=str(get_value(COLUMNS['video_link'])),
            views=parse_int(get_value(COLUMNS['views'])),
            likes=parse_int(get_value(COLUMNS['likes'])),
            comments=parse_int(get_value(COLUMNS['comments'])),
            caption=str(get_value(COLUMNS['caption'])),
            post_date=str(get_value(COLUMNS['post_date'])),
            duration=parse_duration(str(get_value(COLUMNS['duration']))),
            thumbnail_url=str(get_value(COLUMNS['thumbnail'])),
            original_url=str(get_value(COLUMNS['original_url'])),
            posted_to_yt=parse_bool(get_value(COLUMNS['posted_to_yt'])),
            yt_video_id=get_value(COLUMNS['yt_video_id']) or None,
            yt_upload_date=get_value(COLUMNS['yt_upload_date']) or None,
            upload_status=get_value(COLUMNS['upload_status']) or None,
        )

    def get_all_videos(self) -> List[VideoEntry]:
        """Get all video entries from the sheet."""
        sheet_name = self._get_sheet_name()
        range_name = f"'{sheet_name}'!A2:N"  # Skip header row

        response = self.service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=range_name
        ).execute()

        rows = response.get('values', [])
        videos = []

        for i, row in enumerate(rows, start=2):  # Start at row 2 (after header)
            if row and row[0]:  # Skip empty rows
                videos.append(self._parse_row(row, i))

        return videos

    def get_pending_videos(self) -> List[VideoEntry]:
        """Get all videos that haven't been uploaded yet."""
        all_videos = self.get_all_videos()
        return [v for v in all_videos if v.is_pending]

    def get_pending_shorts(self) -> List[VideoEntry]:
        """Get pending videos that qualify as Shorts (< 60 seconds)."""
        pending = self.get_pending_videos()
        return [v for v in pending if v.is_short]

    def get_content_stats(self) -> Dict[str, int]:
        """
        Get content statistics from the sheet.

        Returns:
            Dict with total_pending, shorts_pending, skipped_too_long counts.
        """
        pending = self.get_pending_videos()

        shorts = [v for v in pending if v.is_short]
        too_long = [v for v in pending if not v.is_short]

        return {
            'total_pending': len(pending),
            'shorts_pending': len(shorts),
            'skipped_too_long': len(too_long)
        }

    def mark_as_uploaded(
        self,
        row_number: int,
        youtube_video_id: str,
        status: str = 'success'
    ) -> bool:
        """
        Mark a video as uploaded in the sheet.

        Updates columns:
        - K (Posted to YT): TRUE
        - L (YT Video ID): the YouTube video ID
        - M (YT Upload Date): current date
        - N (Upload Status): status
        """
        try:
            self._ensure_columns_exist()
            sheet_name = self._get_sheet_name()

            # Update columns K through N
            range_name = f"'{sheet_name}'!K{row_number}:N{row_number}"
            values = [[
                'TRUE',
                youtube_video_id,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                status
            ]]

            self.service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=range_name,
                valueInputOption='RAW',
                body={'values': values}
            ).execute()

            logger.info(f"Marked row {row_number} as uploaded (Video ID: {youtube_video_id})")
            return True

        except Exception as e:
            logger.error(f"Failed to update sheet row {row_number}: {e}")
            return False

    def mark_as_skipped(
        self,
        row_number: int,
        reason: str = 'skipped_too_long'
    ) -> bool:
        """Mark a video as skipped (e.g., too long for Shorts)."""
        try:
            self._ensure_columns_exist()
            sheet_name = self._get_sheet_name()

            # Update Upload Status column only
            range_name = f"'{sheet_name}'!N{row_number}"
            values = [[reason]]

            self.service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=range_name,
                valueInputOption='RAW',
                body={'values': values}
            ).execute()

            logger.info(f"Marked row {row_number} as skipped: {reason}")
            return True

        except Exception as e:
            logger.error(f"Failed to update sheet row {row_number}: {e}")
            return False

    def mark_as_failed(
        self,
        row_number: int,
        error_message: str
    ) -> bool:
        """Mark a video upload as failed."""
        try:
            self._ensure_columns_exist()
            sheet_name = self._get_sheet_name()

            # Update Upload Status column with error
            range_name = f"'{sheet_name}'!N{row_number}"
            values = [[f"failed: {error_message[:100]}"]]

            self.service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=range_name,
                valueInputOption='RAW',
                body={'values': values}
            ).execute()

            logger.info(f"Marked row {row_number} as failed: {error_message}")
            return True

        except Exception as e:
            logger.error(f"Failed to update sheet row {row_number}: {e}")
            return False

    def get_next_video_to_upload(self) -> Optional[VideoEntry]:
        """Get the next video that should be uploaded (first pending Short)."""
        shorts = self.get_pending_shorts()
        return shorts[0] if shorts else None

    def get_videos_to_upload(self, limit: int = 10) -> Tuple[List[VideoEntry], List[VideoEntry]]:
        """
        Get videos ready to upload and videos that will be skipped.

        Returns:
            Tuple of (shorts_to_upload, videos_to_skip)
        """
        pending = self.get_pending_videos()

        shorts = []
        too_long = []

        for video in pending[:limit * 2]:  # Get extra to account for skips
            if video.is_short:
                if len(shorts) < limit:
                    shorts.append(video)
            else:
                too_long.append(video)

        return shorts, too_long
