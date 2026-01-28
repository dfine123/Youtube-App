"""Utility functions for logging, configuration, and helpers."""

import os
import logging
import yaml
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet
from rich.console import Console
from rich.logging import RichHandler

# Rich console for fancy output
console = Console()

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent

# Cache for auto-generated encryption key
_cached_encryption_key = None


def _get_credentials():
    """Import credentials from credentials.py file."""
    try:
        import credentials
        return credentials
    except ImportError:
        # Return empty module-like object if credentials.py doesn't exist
        class EmptyCredentials:
            GOOGLE_CLIENT_ID = ""
            GOOGLE_CLIENT_SECRET = ""
            ENCRYPTION_KEY = ""
            SESSION_SECRET = ""
        return EmptyCredentials()


def get_config() -> Dict[str, Any]:
    """Load configuration from config.yaml and credentials.py."""
    config_path = PROJECT_ROOT / "config.yaml"

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Load credentials
    creds = _get_credentials()

    # Override with credentials.py values if present
    if getattr(creds, 'GOOGLE_CLIENT_ID', ''):
        config['google']['client_id'] = creds.GOOGLE_CLIENT_ID
    if getattr(creds, 'GOOGLE_CLIENT_SECRET', ''):
        config['google']['client_secret'] = creds.GOOGLE_CLIENT_SECRET

    return config


def get_encryption_key() -> bytes:
    """
    Get the encryption key from credentials.py.
    Auto-generates and saves one if not set.
    """
    global _cached_encryption_key

    # Return cached key if available
    if _cached_encryption_key:
        return _cached_encryption_key

    creds = _get_credentials()
    key = getattr(creds, 'ENCRYPTION_KEY', '')

    if key:
        _cached_encryption_key = key.encode()
        return _cached_encryption_key

    # Auto-generate encryption key
    new_key = Fernet.generate_key().decode()
    _cached_encryption_key = new_key.encode()

    # Try to save it to credentials.py for persistence
    credentials_path = PROJECT_ROOT / "credentials.py"
    if credentials_path.exists():
        try:
            content = credentials_path.read_text()
            # Update the ENCRYPTION_KEY line
            import re
            new_content = re.sub(
                r'ENCRYPTION_KEY\s*=\s*["\'].*["\']',
                f'ENCRYPTION_KEY = "{new_key}"',
                content
            )
            if new_content != content:
                credentials_path.write_text(new_content)
                console.print(f"[green]Auto-generated ENCRYPTION_KEY and saved to credentials.py[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not save auto-generated encryption key: {e}[/yellow]")
            console.print(f"[yellow]Add this to credentials.py: ENCRYPTION_KEY = \"{new_key}\"[/yellow]")

    return _cached_encryption_key


def get_session_secret() -> str:
    """Get Flask session secret from credentials.py or generate one."""
    creds = _get_credentials()
    secret = getattr(creds, 'SESSION_SECRET', '')

    if secret:
        return secret

    # Generate a random session secret
    return os.urandom(24).hex()


def encrypt_token(token: str) -> bytes:
    """Encrypt a token using Fernet symmetric encryption."""
    if not token:
        return b''
    fernet = Fernet(get_encryption_key())
    return fernet.encrypt(token.encode())


def decrypt_token(encrypted_token: bytes) -> str:
    """Decrypt a token using Fernet symmetric encryption."""
    if not encrypted_token:
        return ''
    fernet = Fernet(get_encryption_key())
    return fernet.decrypt(encrypted_token).decode()


def setup_logging(name: str = 'youtube_uploader', level: int = logging.INFO) -> logging.Logger:
    """Set up logging with Rich handler and file output."""
    config = get_config()
    log_dir = PROJECT_ROOT / config['paths']['logs']
    log_dir.mkdir(exist_ok=True)

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Remove existing handlers
    logger.handlers = []

    # Rich console handler
    console_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True
    )
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # File handler
    log_file = log_dir / f"{name}_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(level)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger


def get_temp_dir() -> Path:
    """Get the temporary download directory, creating if needed."""
    config = get_config()
    temp_dir = PROJECT_ROOT / config['paths']['temp_downloads']
    temp_dir.mkdir(exist_ok=True)
    return temp_dir


def parse_duration(duration_str: str) -> float:
    """
    Parse a duration string into seconds.

    Supports formats:
    - "23" or "23s" -> 23 seconds
    - "1:23" or "01:23" -> 83 seconds
    - "1:23:45" -> 5025 seconds
    - "23.5" -> 23.5 seconds
    """
    if not duration_str:
        return 0.0

    duration_str = str(duration_str).strip().lower()

    # Remove 's' suffix if present
    if duration_str.endswith('s'):
        duration_str = duration_str[:-1]

    # Handle MM:SS or HH:MM:SS format
    if ':' in duration_str:
        parts = duration_str.split(':')
        if len(parts) == 2:
            minutes, seconds = parts
            return float(minutes) * 60 + float(seconds)
        elif len(parts) == 3:
            hours, minutes, seconds = parts
            return float(hours) * 3600 + float(minutes) * 60 + float(seconds)

    # Handle plain number (seconds)
    try:
        return float(duration_str)
    except ValueError:
        return 0.0


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"


def time_since(dt: datetime) -> str:
    """Return a human-readable string for time since a datetime."""
    if not dt:
        return "never"

    now = datetime.utcnow()
    diff = now - dt

    if diff.total_seconds() < 60:
        return "just now"
    elif diff.total_seconds() < 3600:
        minutes = int(diff.total_seconds() // 60)
        return f"{minutes}m ago"
    elif diff.total_seconds() < 86400:
        hours = int(diff.total_seconds() // 3600)
        minutes = int((diff.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m ago"
    else:
        days = int(diff.total_seconds() // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"


def time_until(dt: datetime) -> str:
    """Return a human-readable string for time until a datetime."""
    if not dt:
        return "now"

    now = datetime.utcnow()
    diff = dt - now

    if diff.total_seconds() <= 0:
        return "now"
    elif diff.total_seconds() < 60:
        return "in less than a minute"
    elif diff.total_seconds() < 3600:
        minutes = int(diff.total_seconds() // 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif diff.total_seconds() < 86400:
        hours = int(diff.total_seconds() // 3600)
        minutes = int((diff.total_seconds() % 3600) // 60)
        if minutes > 0:
            return f"in {hours}h {minutes}m"
        return f"in {hours} hour{'s' if hours != 1 else ''}"
    else:
        return "tomorrow"


def get_runway_emoji(days: float) -> str:
    """Get the appropriate emoji for content runway status."""
    if days <= 0:
        return "\u26ab"  # Black circle - empty
    elif days < 7:
        return "\U0001f534"  # Red circle - critical
    elif days < 14:
        return "\U0001f7e1"  # Yellow circle - warning
    else:
        return "\U0001f7e2"  # Green circle - healthy


def clean_filename(filename: str) -> str:
    """Clean a string to be safe for use as a filename."""
    # Remove or replace unsafe characters
    unsafe_chars = '<>:"/\\|?*'
    for char in unsafe_chars:
        filename = filename.replace(char, '_')
    return filename[:200]  # Limit length


def extract_file_id_from_drive_url(url: str) -> Optional[str]:
    """Extract the file ID from various Google Drive URL formats."""
    if not url:
        return None

    import re

    # Pattern for direct download links: drive.google.com/uc?id=XXXXX
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern for view links: drive.google.com/file/d/XXXXX/view
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern for open links: drive.google.com/open?id=XXXXX
    match = re.search(r'open\?id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    return None


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
