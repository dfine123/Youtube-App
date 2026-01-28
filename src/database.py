"""SQLite database operations for creator management and upload tracking."""

import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from .utils import (
    get_config,
    encrypt_token,
    decrypt_token,
    PROJECT_ROOT,
    setup_logging
)

logger = setup_logging('database')


@dataclass
class Creator:
    """Represents a creator profile."""
    id: Optional[int]
    name: str
    channel_id: str
    sheet_id: str
    access_token: str  # Decrypted
    refresh_token: str  # Decrypted
    token_expiry: Optional[datetime]
    posts_per_day: int
    default_privacy: str
    last_upload_at: Optional[datetime]
    uploads_today: int
    uploads_today_date: Optional[date]
    created_at: datetime
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'Creator':
        """Create a Creator instance from a database row."""
        return cls(
            id=row['id'],
            name=row['name'],
            channel_id=row['channel_id'],
            sheet_id=row['sheet_id'],
            access_token=decrypt_token(row['access_token_encrypted']) if row['access_token_encrypted'] else '',
            refresh_token=decrypt_token(row['refresh_token_encrypted']) if row['refresh_token_encrypted'] else '',
            token_expiry=datetime.fromisoformat(row['token_expiry']) if row['token_expiry'] else None,
            posts_per_day=row['posts_per_day'],
            default_privacy=row['default_privacy'],
            last_upload_at=datetime.fromisoformat(row['last_upload_at']) if row['last_upload_at'] else None,
            uploads_today=row['uploads_today'],
            uploads_today_date=date.fromisoformat(row['uploads_today_date']) if row['uploads_today_date'] else None,
            created_at=datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now(),
            active=bool(row['active'])
        )


@dataclass
class UploadLogEntry:
    """Represents an upload log entry."""
    id: Optional[int]
    creator_id: int
    post_id: str
    youtube_video_id: Optional[str]
    status: str  # success, failed, skipped_too_long
    error_message: Optional[str]
    video_duration: Optional[float]
    uploaded_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'UploadLogEntry':
        """Create an UploadLogEntry instance from a database row."""
        return cls(
            id=row['id'],
            creator_id=row['creator_id'],
            post_id=row['post_id'],
            youtube_video_id=row['youtube_video_id'],
            status=row['status'],
            error_message=row['error_message'],
            video_duration=row['video_duration'],
            uploaded_at=datetime.fromisoformat(row['uploaded_at']) if row['uploaded_at'] else datetime.now()
        )


@dataclass
class ContentStats:
    """Represents content statistics for a creator."""
    id: Optional[int]
    creator_id: int
    total_pending: int
    shorts_pending: int
    skipped_too_long: int
    days_runway: float
    last_calculated: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'ContentStats':
        """Create a ContentStats instance from a database row."""
        return cls(
            id=row['id'],
            creator_id=row['creator_id'],
            total_pending=row['total_pending'],
            shorts_pending=row['shorts_pending'],
            skipped_too_long=row['skipped_too_long'],
            days_runway=row['days_runway'],
            last_calculated=datetime.fromisoformat(row['last_calculated']) if row['last_calculated'] else datetime.now()
        )


class Database:
    """SQLite database manager for YouTube Shorts uploader."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection."""
        if db_path is None:
            config = get_config()
            db_path = PROJECT_ROOT / config['paths']['database']
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize the database schema."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Create creators table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS creators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                channel_id TEXT NOT NULL,
                sheet_id TEXT NOT NULL,
                access_token_encrypted BLOB,
                refresh_token_encrypted BLOB,
                token_expiry TEXT,
                posts_per_day INTEGER DEFAULT 3,
                default_privacy TEXT DEFAULT 'public',
                last_upload_at TEXT,
                uploads_today INTEGER DEFAULT 0,
                uploads_today_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1
            )
        ''')

        # Create upload_log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                post_id TEXT,
                youtube_video_id TEXT,
                status TEXT,
                error_message TEXT,
                video_duration REAL,
                uploaded_at TEXT,
                FOREIGN KEY (creator_id) REFERENCES creators(id)
            )
        ''')

        # Create content_stats table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER UNIQUE,
                total_pending INTEGER,
                shorts_pending INTEGER,
                skipped_too_long INTEGER,
                days_runway REAL,
                last_calculated TEXT,
                FOREIGN KEY (creator_id) REFERENCES creators(id)
            )
        ''')

        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_creator ON upload_log(creator_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_post ON upload_log(post_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_status ON upload_log(status)')

        conn.commit()
        conn.close()

    # ========== Creator CRUD Operations ==========

    def add_creator(
        self,
        name: str,
        channel_id: str,
        sheet_id: str,
        access_token: str,
        refresh_token: str,
        token_expiry: Optional[datetime] = None,
        posts_per_day: int = 3,
        default_privacy: str = 'public'
    ) -> int:
        """Add a new creator to the database."""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT INTO creators (
                    name, channel_id, sheet_id,
                    access_token_encrypted, refresh_token_encrypted, token_expiry,
                    posts_per_day, default_privacy,
                    uploads_today, uploads_today_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ''', (
                name,
                channel_id,
                sheet_id,
                encrypt_token(access_token),
                encrypt_token(refresh_token),
                token_expiry.isoformat() if token_expiry else None,
                posts_per_day,
                default_privacy,
                date.today().isoformat()
            ))

            conn.commit()
            creator_id = cursor.lastrowid
            logger.info(f"Added creator: {name} (ID: {creator_id})")
            return creator_id

        except sqlite3.IntegrityError:
            raise ValueError(f"Creator with name '{name}' already exists")
        finally:
            conn.close()

    def get_creator(self, name: str) -> Optional[Creator]:
        """Get a creator by name."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM creators WHERE name = ?', (name,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return Creator.from_row(row)
        return None

    def get_creator_by_id(self, creator_id: int) -> Optional[Creator]:
        """Get a creator by ID."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM creators WHERE id = ?', (creator_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return Creator.from_row(row)
        return None

    def get_all_creators(self, active_only: bool = True) -> List[Creator]:
        """Get all creators."""
        conn = self._get_connection()
        cursor = conn.cursor()

        if active_only:
            cursor.execute('SELECT * FROM creators WHERE active = 1 ORDER BY name')
        else:
            cursor.execute('SELECT * FROM creators ORDER BY name')

        rows = cursor.fetchall()
        conn.close()

        return [Creator.from_row(row) for row in rows]

    def update_creator(self, name: str, **kwargs) -> bool:
        """Update creator fields. Supports: posts_per_day, default_privacy, active, sheet_id."""
        allowed_fields = {'posts_per_day', 'default_privacy', 'active', 'sheet_id'}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

        if not updates:
            return False

        conn = self._get_connection()
        cursor = conn.cursor()

        set_clause = ', '.join(f'{k} = ?' for k in updates.keys())
        values = list(updates.values()) + [name]

        cursor.execute(
            f'UPDATE creators SET {set_clause} WHERE name = ?',
            values
        )

        conn.commit()
        affected = cursor.rowcount
        conn.close()

        return affected > 0

    def update_tokens(
        self,
        name: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        token_expiry: Optional[datetime] = None
    ) -> bool:
        """Update OAuth tokens for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        if refresh_token:
            cursor.execute('''
                UPDATE creators
                SET access_token_encrypted = ?, refresh_token_encrypted = ?, token_expiry = ?
                WHERE name = ?
            ''', (
                encrypt_token(access_token),
                encrypt_token(refresh_token),
                token_expiry.isoformat() if token_expiry else None,
                name
            ))
        else:
            cursor.execute('''
                UPDATE creators
                SET access_token_encrypted = ?, token_expiry = ?
                WHERE name = ?
            ''', (
                encrypt_token(access_token),
                token_expiry.isoformat() if token_expiry else None,
                name
            ))

        conn.commit()
        affected = cursor.rowcount
        conn.close()

        return affected > 0

    def remove_creator(self, name: str) -> bool:
        """Remove a creator from the database."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get creator ID first
        cursor.execute('SELECT id FROM creators WHERE name = ?', (name,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False

        creator_id = row['id']

        # Delete related records
        cursor.execute('DELETE FROM upload_log WHERE creator_id = ?', (creator_id,))
        cursor.execute('DELETE FROM content_stats WHERE creator_id = ?', (creator_id,))
        cursor.execute('DELETE FROM creators WHERE id = ?', (creator_id,))

        conn.commit()
        conn.close()

        logger.info(f"Removed creator: {name}")
        return True

    # ========== Upload Tracking ==========

    def record_upload(
        self,
        creator_id: int,
        post_id: str,
        youtube_video_id: Optional[str],
        status: str,
        error_message: Optional[str] = None,
        video_duration: Optional[float] = None
    ) -> int:
        """Record an upload attempt in the log."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO upload_log (
                creator_id, post_id, youtube_video_id, status, error_message, video_duration, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            creator_id,
            post_id,
            youtube_video_id,
            status,
            error_message,
            video_duration,
            datetime.utcnow().isoformat()
        ))

        conn.commit()
        log_id = cursor.lastrowid
        conn.close()

        return log_id

    def update_upload_count(self, creator_id: int) -> None:
        """Increment upload count for today."""
        conn = self._get_connection()
        cursor = conn.cursor()

        today = date.today().isoformat()

        # Check if we need to reset the counter (new day)
        cursor.execute(
            'SELECT uploads_today_date FROM creators WHERE id = ?',
            (creator_id,)
        )
        row = cursor.fetchone()

        if row and row['uploads_today_date'] != today:
            # New day, reset counter
            cursor.execute('''
                UPDATE creators
                SET uploads_today = 1, uploads_today_date = ?, last_upload_at = ?
                WHERE id = ?
            ''', (today, datetime.utcnow().isoformat(), creator_id))
        else:
            # Same day, increment counter
            cursor.execute('''
                UPDATE creators
                SET uploads_today = uploads_today + 1, last_upload_at = ?
                WHERE id = ?
            ''', (datetime.utcnow().isoformat(), creator_id))

        conn.commit()
        conn.close()

    def reset_daily_counts(self) -> int:
        """Reset daily upload counts for all creators (new day)."""
        conn = self._get_connection()
        cursor = conn.cursor()

        today = date.today().isoformat()

        cursor.execute('''
            UPDATE creators
            SET uploads_today = 0, uploads_today_date = ?
            WHERE uploads_today_date != ? OR uploads_today_date IS NULL
        ''', (today, today))

        conn.commit()
        affected = cursor.rowcount
        conn.close()

        return affected

    def get_upload_logs(
        self,
        creator_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[UploadLogEntry]:
        """Get upload log entries with optional filters."""
        conn = self._get_connection()
        cursor = conn.cursor()

        query = 'SELECT * FROM upload_log WHERE 1=1'
        params = []

        if creator_id:
            query += ' AND creator_id = ?'
            params.append(creator_id)

        if status:
            query += ' AND status = ?'
            params.append(status)

        query += ' ORDER BY uploaded_at DESC LIMIT ?'
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [UploadLogEntry.from_row(row) for row in rows]

    def is_post_uploaded(self, creator_id: int, post_id: str) -> bool:
        """Check if a post has already been successfully uploaded."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT 1 FROM upload_log
            WHERE creator_id = ? AND post_id = ? AND status = 'success'
            LIMIT 1
        ''', (creator_id, post_id))

        result = cursor.fetchone() is not None
        conn.close()

        return result

    # ========== Content Stats ==========

    def update_content_stats(
        self,
        creator_id: int,
        total_pending: int,
        shorts_pending: int,
        skipped_too_long: int
    ) -> None:
        """Update content statistics for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get posts_per_day for runway calculation
        cursor.execute('SELECT posts_per_day FROM creators WHERE id = ?', (creator_id,))
        row = cursor.fetchone()
        posts_per_day = row['posts_per_day'] if row else 3

        days_runway = shorts_pending / posts_per_day if posts_per_day > 0 else 0

        cursor.execute('''
            INSERT OR REPLACE INTO content_stats (
                creator_id, total_pending, shorts_pending, skipped_too_long, days_runway, last_calculated
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            creator_id,
            total_pending,
            shorts_pending,
            skipped_too_long,
            days_runway,
            datetime.utcnow().isoformat()
        ))

        conn.commit()
        conn.close()

    def get_content_stats(self, creator_id: int) -> Optional[ContentStats]:
        """Get content statistics for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM content_stats WHERE creator_id = ?', (creator_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return ContentStats.from_row(row)
        return None

    # ========== Scheduling Helpers ==========

    def can_upload(self, creator: Creator, force: bool = False) -> tuple[bool, str]:
        """
        Check if a creator can upload right now.

        Returns: (can_upload, reason)
        """
        if force:
            return True, "Force mode enabled"

        today = date.today()

        # Reset counter if it's a new day
        if creator.uploads_today_date != today:
            return True, "New day, counter reset"

        # Check daily quota
        if creator.uploads_today >= creator.posts_per_day:
            return False, f"Daily quota reached ({creator.uploads_today}/{creator.posts_per_day})"

        # Check 2-hour spacing
        if creator.last_upload_at and creator.uploads_today > 0:
            config = get_config()
            spacing_hours = config['defaults']['upload_spacing_hours']
            hours_since = (datetime.utcnow() - creator.last_upload_at).total_seconds() / 3600

            if hours_since < spacing_hours:
                minutes_remaining = int((spacing_hours - hours_since) * 60)
                return False, f"Too soon (wait {minutes_remaining} more minutes)"

        return True, "Ready to upload"

    def get_uploads_remaining_today(self, creator: Creator) -> int:
        """Get how many uploads are remaining for today."""
        today = date.today()

        if creator.uploads_today_date != today:
            return creator.posts_per_day

        return max(0, creator.posts_per_day - creator.uploads_today)
