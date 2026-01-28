"""YouTube OAuth flow handler for authentication and token management."""

import os
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from .utils import get_config, setup_logging

logger = setup_logging('oauth')

# OAuth scopes required for the application
SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly'
]


class OAuthManager:
    """Manages OAuth authentication for Google APIs."""

    def __init__(self):
        """Initialize OAuth manager with config."""
        self.config = get_config()
        self.client_id = self.config['google']['client_id']
        self.client_secret = self.config['google']['client_secret']

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Google OAuth credentials not configured. "
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env file."
            )

    def get_client_config(self) -> Dict[str, Any]:
        """Get OAuth client configuration."""
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost", "urn:ietf:wg:oauth:2.0:oob"]
            }
        }

    def run_oauth_flow(self, port: int = 8080) -> Tuple[str, str, datetime]:
        """
        Run the OAuth authorization flow.

        Opens a browser for the user to authorize, then captures the tokens.

        Returns:
            Tuple of (access_token, refresh_token, token_expiry)
        """
        logger.info("Starting OAuth authorization flow...")

        flow = InstalledAppFlow.from_client_config(
            self.get_client_config(),
            scopes=SCOPES
        )

        # Run local server for OAuth callback
        credentials = flow.run_local_server(
            port=port,
            prompt='consent',
            access_type='offline'
        )

        # Calculate token expiry
        token_expiry = datetime.utcnow() + timedelta(seconds=3600)
        if credentials.expiry:
            token_expiry = credentials.expiry.replace(tzinfo=None)

        logger.info("OAuth authorization successful")

        return (
            credentials.token,
            credentials.refresh_token,
            token_expiry
        )

    def create_credentials(
        self,
        access_token: str,
        refresh_token: str,
        token_expiry: Optional[datetime] = None
    ) -> Credentials:
        """Create Credentials object from stored tokens."""
        return Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.client_id,
            client_secret=self.client_secret,
            expiry=token_expiry
        )

    def refresh_credentials(self, credentials: Credentials) -> Tuple[str, datetime]:
        """
        Refresh expired credentials.

        Returns:
            Tuple of (new_access_token, new_expiry)
        """
        if not credentials.refresh_token:
            raise ValueError("No refresh token available. Re-authorization required.")

        credentials.refresh(Request())

        token_expiry = datetime.utcnow() + timedelta(seconds=3600)
        if credentials.expiry:
            token_expiry = credentials.expiry.replace(tzinfo=None)

        logger.info("Credentials refreshed successfully")

        return credentials.token, token_expiry

    def get_valid_credentials(
        self,
        access_token: str,
        refresh_token: str,
        token_expiry: Optional[datetime] = None
    ) -> Tuple[Credentials, bool]:
        """
        Get valid credentials, refreshing if necessary.

        Returns:
            Tuple of (credentials, was_refreshed)
        """
        credentials = self.create_credentials(
            access_token,
            refresh_token,
            token_expiry
        )

        # Check if credentials need refresh
        if credentials.expired or not credentials.valid:
            try:
                new_token, new_expiry = self.refresh_credentials(credentials)
                credentials = self.create_credentials(
                    new_token,
                    refresh_token,
                    new_expiry
                )
                return credentials, True
            except Exception as e:
                logger.error(f"Failed to refresh credentials: {e}")
                raise

        return credentials, False


class YouTubeClient:
    """YouTube API client wrapper."""

    def __init__(self, credentials: Credentials):
        """Initialize YouTube client with credentials."""
        self.credentials = credentials
        self._service = None

    @property
    def service(self):
        """Get or create YouTube API service."""
        if self._service is None:
            self._service = build('youtube', 'v3', credentials=self.credentials)
        return self._service

    def get_channel_info(self) -> Optional[Dict[str, str]]:
        """
        Get the authenticated user's channel info.

        Returns:
            Dict with 'id' and 'title' of the channel, or None if no channel.
        """
        try:
            response = self.service.channels().list(
                part='snippet',
                mine=True
            ).execute()

            if 'items' in response and len(response['items']) > 0:
                channel = response['items'][0]
                return {
                    'id': channel['id'],
                    'title': channel['snippet']['title']
                }

            logger.warning("No YouTube channel found for this account")
            return None

        except Exception as e:
            logger.error(f"Failed to get channel info: {e}")
            raise

    def verify_upload_permission(self) -> bool:
        """Verify that we have permission to upload videos."""
        try:
            # Try to get channel info - if successful, we have access
            channel = self.get_channel_info()
            return channel is not None
        except Exception as e:
            logger.error(f"Upload permission verification failed: {e}")
            return False


class SheetsClient:
    """Google Sheets API client wrapper."""

    def __init__(self, credentials: Credentials):
        """Initialize Sheets client with credentials."""
        self.credentials = credentials
        self._service = None

    @property
    def service(self):
        """Get or create Sheets API service."""
        if self._service is None:
            self._service = build('sheets', 'v4', credentials=self.credentials)
        return self._service

    def verify_sheet_access(self, sheet_id: str) -> bool:
        """Verify we can access the specified sheet."""
        try:
            self.service.spreadsheets().get(
                spreadsheetId=sheet_id
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Cannot access sheet {sheet_id}: {e}")
            return False

    def get_sheet_title(self, sheet_id: str) -> Optional[str]:
        """Get the title of a spreadsheet."""
        try:
            response = self.service.spreadsheets().get(
                spreadsheetId=sheet_id,
                fields='properties.title'
            ).execute()
            return response['properties']['title']
        except Exception as e:
            logger.error(f"Failed to get sheet title: {e}")
            return None


class DriveClient:
    """Google Drive API client wrapper."""

    def __init__(self, credentials: Credentials):
        """Initialize Drive client with credentials."""
        self.credentials = credentials
        self._service = None

    @property
    def service(self):
        """Get or create Drive API service."""
        if self._service is None:
            self._service = build('drive', 'v3', credentials=self.credentials)
        return self._service

    def get_file_metadata(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a file."""
        try:
            response = self.service.files().get(
                fileId=file_id,
                fields='id,name,mimeType,size'
            ).execute()
            return response
        except Exception as e:
            logger.error(f"Failed to get file metadata: {e}")
            return None


def verify_all_connections(
    access_token: str,
    refresh_token: str,
    token_expiry: Optional[datetime],
    sheet_id: str
) -> Dict[str, Any]:
    """
    Verify all API connections work with the given credentials.

    Returns:
        Dict with verification results for each service.
    """
    oauth_manager = OAuthManager()

    results = {
        'credentials_valid': False,
        'youtube_access': False,
        'channel_info': None,
        'sheets_access': False,
        'sheet_title': None,
        'errors': []
    }

    try:
        # Get valid credentials
        credentials, _ = oauth_manager.get_valid_credentials(
            access_token, refresh_token, token_expiry
        )
        results['credentials_valid'] = True

        # Verify YouTube access
        youtube = YouTubeClient(credentials)
        channel_info = youtube.get_channel_info()
        if channel_info:
            results['youtube_access'] = True
            results['channel_info'] = channel_info

        # Verify Sheets access
        sheets = SheetsClient(credentials)
        if sheets.verify_sheet_access(sheet_id):
            results['sheets_access'] = True
            results['sheet_title'] = sheets.get_sheet_title(sheet_id)

    except Exception as e:
        results['errors'].append(str(e))

    return results
