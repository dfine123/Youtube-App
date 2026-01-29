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


@dataclass
class VideoStats:
    """Represents YouTube video statistics."""
    id: Optional[int]
    creator_id: int
    youtube_video_id: str
    title: str
    thumbnail_url: str
    views: int
    likes: int
    comments: int
    duration_seconds: int
    uploaded_at: datetime
    last_updated: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'VideoStats':
        """Create a VideoStats instance from a database row."""
        return cls(
            id=row['id'],
            creator_id=row['creator_id'],
            youtube_video_id=row['youtube_video_id'],
            title=row['title'],
            thumbnail_url=row['thumbnail_url'] or '',
            views=row['views'] or 0,
            likes=row['likes'] or 0,
            comments=row['comments'] or 0,
            duration_seconds=row['duration_seconds'] or 0,
            uploaded_at=datetime.fromisoformat(row['uploaded_at']) if row['uploaded_at'] else datetime.now(),
            last_updated=datetime.fromisoformat(row['last_updated']) if row['last_updated'] else datetime.now()
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

        # Create video_stats table for caching YouTube statistics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS video_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                youtube_video_id TEXT NOT NULL UNIQUE,
                title TEXT,
                thumbnail_url TEXT,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                duration_seconds INTEGER DEFAULT 0,
                uploaded_at TEXT,
                last_updated TEXT,
                FOREIGN KEY (creator_id) REFERENCES creators(id)
            )
        ''')

        # Create daily_views table for tracking view trends
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                UNIQUE(creator_id, date),
                FOREIGN KEY (creator_id) REFERENCES creators(id)
            )
        ''')

        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_creator ON upload_log(creator_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_post ON upload_log(post_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_upload_log_status ON upload_log(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_stats_creator ON video_stats(creator_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_stats_views ON video_stats(views DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_views_creator ON daily_views(creator_id)')

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

    # ========== Video Stats ==========

    def upsert_video_stats(
        self,
        creator_id: int,
        youtube_video_id: str,
        title: str,
        thumbnail_url: str,
        views: int,
        likes: int,
        comments: int,
        duration_seconds: int = 0,
        uploaded_at: Optional[datetime] = None
    ) -> int:
        """Insert or update video statistics."""
        conn = self._get_connection()
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()
        uploaded_at_str = uploaded_at.isoformat() if uploaded_at else now

        cursor.execute('''
            INSERT INTO video_stats (
                creator_id, youtube_video_id, title, thumbnail_url,
                views, likes, comments, duration_seconds, uploaded_at, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(youtube_video_id) DO UPDATE SET
                title = excluded.title,
                thumbnail_url = excluded.thumbnail_url,
                views = excluded.views,
                likes = excluded.likes,
                comments = excluded.comments,
                last_updated = excluded.last_updated
        ''', (
            creator_id, youtube_video_id, title, thumbnail_url,
            views, likes, comments, duration_seconds, uploaded_at_str, now
        ))

        conn.commit()
        video_id = cursor.lastrowid
        conn.close()

        return video_id

    def get_video_stats(self, youtube_video_id: str) -> Optional[VideoStats]:
        """Get stats for a specific video."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM video_stats WHERE youtube_video_id = ?', (youtube_video_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return VideoStats.from_row(row)
        return None

    def get_creator_video_stats(
        self,
        creator_id: int,
        order_by: str = 'uploaded_at',
        order_dir: str = 'DESC',
        limit: int = 50
    ) -> List[VideoStats]:
        """Get all video stats for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Validate order_by to prevent SQL injection
        valid_order_columns = {'uploaded_at', 'views', 'likes', 'comments', 'title'}
        if order_by not in valid_order_columns:
            order_by = 'uploaded_at'

        order_dir = 'DESC' if order_dir.upper() == 'DESC' else 'ASC'

        cursor.execute(f'''
            SELECT * FROM video_stats
            WHERE creator_id = ?
            ORDER BY {order_by} {order_dir}
            LIMIT ?
        ''', (creator_id, limit))

        rows = cursor.fetchall()
        conn.close()

        return [VideoStats.from_row(row) for row in rows]

    def get_creator_total_views(self, creator_id: int) -> int:
        """Get total views across all videos for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT COALESCE(SUM(views), 0) as total FROM video_stats WHERE creator_id = ?',
            (creator_id,)
        )
        row = cursor.fetchone()
        conn.close()

        return row['total'] if row else 0

    def get_creator_video_count(self, creator_id: int) -> int:
        """Get total number of uploaded videos for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT COUNT(*) as count FROM video_stats WHERE creator_id = ?',
            (creator_id,)
        )
        row = cursor.fetchone()
        conn.close()

        return row['count'] if row else 0

    def get_top_video(self, creator_id: Optional[int] = None) -> Optional[VideoStats]:
        """Get the top performing video by views, optionally for a specific creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        if creator_id:
            cursor.execute(
                'SELECT * FROM video_stats WHERE creator_id = ? ORDER BY views DESC LIMIT 1',
                (creator_id,)
            )
        else:
            cursor.execute('SELECT * FROM video_stats ORDER BY views DESC LIMIT 1')

        row = cursor.fetchone()
        conn.close()

        if row:
            return VideoStats.from_row(row)
        return None

    def get_bottom_video(self, creator_id: int) -> Optional[VideoStats]:
        """Get the worst performing video by views for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT * FROM video_stats WHERE creator_id = ? ORDER BY views ASC LIMIT 1',
            (creator_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return VideoStats.from_row(row)
        return None

    def get_global_stats(self) -> Dict[str, Any]:
        """Get aggregate stats across all creators."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                COALESCE(SUM(views), 0) as total_views,
                COUNT(*) as total_videos,
                COALESCE(AVG(views), 0) as avg_views
            FROM video_stats
        ''')
        row = cursor.fetchone()
        conn.close()

        return {
            'total_views': row['total_views'] if row else 0,
            'total_videos': row['total_videos'] if row else 0,
            'avg_views': round(row['avg_views'], 1) if row else 0
        }

    def get_video_ids_for_creator(self, creator_id: int) -> List[str]:
        """Get all YouTube video IDs for a creator from upload logs."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT youtube_video_id FROM upload_log
            WHERE creator_id = ? AND youtube_video_id IS NOT NULL AND status = 'success'
        ''', (creator_id,))

        rows = cursor.fetchall()
        conn.close()

        return [row['youtube_video_id'] for row in rows]

    def get_stats_last_updated(self, creator_id: int) -> Optional[datetime]:
        """Get when stats were last updated for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT MAX(last_updated) as last_updated FROM video_stats WHERE creator_id = ?
        ''', (creator_id,))
        row = cursor.fetchone()
        conn.close()

        if row and row['last_updated']:
            return datetime.fromisoformat(row['last_updated'])
        return None

    def should_refresh_stats(self, creator_id: int, hours_threshold: int = 6) -> bool:
        """Check if stats should be refreshed (older than threshold)."""
        last_updated = self.get_stats_last_updated(creator_id)
        if not last_updated:
            return True

        hours_since = (datetime.utcnow() - last_updated).total_seconds() / 3600
        return hours_since >= hours_threshold

    # ========== Daily Views Tracking ==========

    def record_daily_views(self, creator_id: int, views: int, for_date: Optional[date] = None) -> None:
        """Record total views for a creator on a specific date."""
        conn = self._get_connection()
        cursor = conn.cursor()

        target_date = (for_date or date.today()).isoformat()

        cursor.execute('''
            INSERT INTO daily_views (creator_id, date, views)
            VALUES (?, ?, ?)
            ON CONFLICT(creator_id, date) DO UPDATE SET views = excluded.views
        ''', (creator_id, target_date, views))

        conn.commit()
        conn.close()

    def get_views_trend(self, creator_id: int, days: int = 30) -> List[Dict[str, Any]]:
        """Get daily views trend for a creator."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT date, views FROM daily_views
            WHERE creator_id = ?
            ORDER BY date DESC
            LIMIT ?
        ''', (creator_id, days))

        rows = cursor.fetchall()
        conn.close()

        # Return in chronological order
        return [{'date': row['date'], 'views': row['views']} for row in reversed(rows)]

    def get_views_change(self, creator_id: int, days: int = 7) -> int:
        """Get views gained in the last N days."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get earliest and latest views in the period
        cursor.execute('''
            SELECT views FROM daily_views
            WHERE creator_id = ?
            ORDER BY date DESC
            LIMIT 1
        ''', (creator_id,))
        latest = cursor.fetchone()

        cursor.execute('''
            SELECT views FROM daily_views
            WHERE creator_id = ?
            ORDER BY date ASC
            LIMIT 1 OFFSET ?
        ''', (creator_id, max(0, days - 1)))
        earliest = cursor.fetchone()

        conn.close()

        if latest and earliest:
            return latest['views'] - earliest['views']
        return 0
