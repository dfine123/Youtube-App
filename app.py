#!/usr/bin/env python3
"""
YouTube Shorts Uploader - Web Dashboard
A Flask-based web interface for managing YouTube Shorts uploads.
"""

import os
import json
import threading
import queue
from datetime import datetime, date
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, flash, session, Response, stream_with_context
)
import yaml

# Import existing modules
from src.database import Database, Creator, UploadLogEntry, ContentStats, VideoStats
from src.oauth import OAuthManager, YouTubeClient, SheetsClient, verify_all_connections, SCOPES
from src.sheets import SheetsManager
from src.uploader import YouTubeUploader, estimate_quota_usage
from src.downloader import DriveDownloader
from src.metadata import generate_shorts_metadata
from src.utils import (
    get_config, get_runway_emoji, format_duration,
    time_since, setup_logging, PROJECT_ROOT, get_session_secret,
    reload_credentials
)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = get_session_secret()

# Configure for HTTPS (required for OAuth on Replit/production)
app.config['PREFERRED_URL_SCHEME'] = 'https'


def get_oauth_redirect_uri(endpoint: str = 'oauth_callback') -> str:
    """Get the OAuth redirect URI, forcing HTTPS for production environments."""
    # Map endpoint names to URL paths
    endpoint_paths = {
        'oauth_callback': '/oauth/callback',
        'oauth_callback_reauth': '/oauth/callback/reauth'
    }

    # Check if we're on Replit or other cloud platform
    replit_domain = os.environ.get('REPLIT_DEV_DOMAIN')
    if replit_domain:
        path = endpoint_paths.get(endpoint, '/oauth/callback')
        return f"https://{replit_domain}{path}"

    # Build URL and force HTTPS if not localhost
    url = url_for(endpoint, _external=True)
    if not url.startswith('http://localhost') and not url.startswith('http://127.0.0.1'):
        url = url.replace('http://', 'https://')
    return url

# Initialize database
db = Database()

# Logger
logger = setup_logging('webapp')

# Upload progress tracking
upload_progress = {}
upload_logs = {}


# ========== Helper Functions ==========

def get_next_post_info(creator: Creator, shorts_ready: int) -> dict:
    """
    Calculate when the next post can/will be uploaded.

    Returns dict with:
        - next_post_str: Human readable string
        - next_post_countdown: Minutes until next post (or -1 if not applicable)
        - next_post_status: 'ready', 'waiting', 'quota_met', 'no_content'
    """
    from datetime import timedelta

    now = datetime.utcnow()

    # Check if no content remaining
    if shorts_ready <= 0:
        return {
            'next_post_str': 'No content remaining',
            'next_post_countdown': -1,
            'next_post_status': 'no_content'
        }

    # Check if daily quota is met
    if creator.uploads_today >= creator.posts_per_day:
        # Calculate tomorrow's start time (assume 8 AM UTC for simplicity)
        tomorrow = now.date() + timedelta(days=1)
        next_time = datetime.combine(tomorrow, datetime.min.time().replace(hour=8))
        hours_until = (next_time - now).total_seconds() / 3600

        return {
            'next_post_str': f'Tomorrow ~{int(hours_until)}h',
            'next_post_countdown': int(hours_until * 60),
            'next_post_status': 'quota_met'
        }

    # Check 2-hour spacing rule
    if creator.last_upload_at:
        time_since_last = (now - creator.last_upload_at).total_seconds()
        two_hours = 2 * 60 * 60  # 2 hours in seconds

        if time_since_last < two_hours:
            remaining_seconds = two_hours - time_since_last
            remaining_minutes = int(remaining_seconds / 60)

            if remaining_minutes >= 60:
                hours = remaining_minutes // 60
                mins = remaining_minutes % 60
                countdown_str = f'{hours}h {mins}m' if mins > 0 else f'{hours}h'
            else:
                countdown_str = f'{remaining_minutes}m'

            return {
                'next_post_str': f'In {countdown_str}',
                'next_post_countdown': remaining_minutes,
                'next_post_status': 'waiting'
            }

    # Ready to post now
    return {
        'next_post_str': 'Ready now',
        'next_post_countdown': 0,
        'next_post_status': 'ready'
    }


def get_creator_status(creator: Creator) -> dict:
    """Get comprehensive status for a creator."""
    stats = db.get_content_stats(creator.id)
    can_upload, reason = db.can_upload(creator)
    uploads_remaining = db.get_uploads_remaining_today(creator)

    # Determine status
    if not creator.active:
        status = 'inactive'
        status_color = 'secondary'
    elif not creator.access_token or not creator.refresh_token:
        status = 'needs_auth'
        status_color = 'warning'
    elif can_upload:
        status = 'active'
        status_color = 'success'
    else:
        status = 'waiting'
        status_color = 'info'

    # Calculate runway
    days_runway = stats.days_runway if stats else 0
    shorts_ready = stats.shorts_pending if stats else 0

    # Runway color coding
    if days_runway >= 14:
        runway_color = 'success'
        runway_emoji = '🟢'
    elif days_runway >= 7:
        runway_color = 'warning'
        runway_emoji = '🟡'
    elif days_runway > 0:
        runway_color = 'danger'
        runway_emoji = '🔴'
    else:
        runway_color = 'dark'
        runway_emoji = '⚫'

    # Calculate next post time
    next_post = get_next_post_info(creator, shorts_ready)

    return {
        'id': creator.id,
        'name': creator.name,
        'channel_id': creator.channel_id,
        'sheet_id': creator.sheet_id,
        'posts_per_day': creator.posts_per_day,
        'default_privacy': creator.default_privacy,
        'active': creator.active,
        'uploads_today': creator.uploads_today,
        'uploads_remaining': uploads_remaining,
        'last_upload_at': creator.last_upload_at,
        'last_upload_str': time_since(creator.last_upload_at) if creator.last_upload_at else 'Never',
        'shorts_ready': shorts_ready,
        'days_runway': round(days_runway, 1),
        'runway_color': runway_color,
        'runway_emoji': runway_emoji,
        'status': status,
        'status_color': status_color,
        'can_upload': can_upload,
        'upload_reason': reason,
        'created_at': creator.created_at,
        'next_post_str': next_post['next_post_str'],
        'next_post_countdown': next_post['next_post_countdown'],
        'next_post_status': next_post['next_post_status']
    }


def refresh_creator_stats(creator: Creator) -> dict:
    """Refresh content stats from Google Sheets."""
    try:
        oauth = OAuthManager()
        credentials, was_refreshed = oauth.get_valid_credentials(
            creator.access_token,
            creator.refresh_token,
            creator.token_expiry
        )

        if was_refreshed:
            expiry = credentials.expiry.replace(tzinfo=None) if credentials.expiry else None
            db.update_tokens(creator.name, credentials.token, token_expiry=expiry)

        sheets = SheetsManager(credentials, creator.sheet_id)
        stats = sheets.get_content_stats()

        db.update_content_stats(
            creator.id,
            stats['total_pending'],
            stats['shorts_pending'],
            stats['skipped_too_long']
        )

        return {'success': True, 'stats': stats}
    except Exception as e:
        logger.error(f"Failed to refresh stats for {creator.name}: {e}")
        return {'success': False, 'error': str(e)}


def refresh_video_stats(creator: Creator, force: bool = False) -> dict:
    """
    Refresh YouTube video statistics for a creator.
    Caches results and only refreshes if needed (every 6 hours) unless forced.
    """
    try:
        # Check if refresh is needed
        if not force and not db.should_refresh_stats(creator.id):
            return {
                'success': True,
                'message': 'Stats are still fresh',
                'refreshed': False
            }

        oauth = OAuthManager()
        credentials, was_refreshed = oauth.get_valid_credentials(
            creator.access_token,
            creator.refresh_token,
            creator.token_expiry
        )

        if was_refreshed:
            expiry = credentials.expiry.replace(tzinfo=None) if credentials.expiry else None
            db.update_tokens(creator.name, credentials.token, token_expiry=expiry)

        youtube = YouTubeClient(credentials)

        # Get video IDs from upload logs (videos we've uploaded)
        video_ids = db.get_video_ids_for_creator(creator.id)

        if not video_ids:
            # Try to get videos from channel directly
            video_ids = youtube.get_channel_videos(creator.channel_id, max_results=100)

        if not video_ids:
            return {
                'success': True,
                'message': 'No videos found',
                'refreshed': False,
                'videos_updated': 0
            }

        # Fetch statistics from YouTube API
        video_stats = youtube.get_video_statistics(video_ids)

        # Store in database
        total_views = 0
        for stats in video_stats:
            db.upsert_video_stats(
                creator_id=creator.id,
                youtube_video_id=stats['video_id'],
                title=stats['title'],
                thumbnail_url=stats['thumbnail_url'],
                views=stats['views'],
                likes=stats['likes'],
                comments=stats['comments'],
                duration_seconds=stats['duration_seconds'],
                uploaded_at=stats['published_at']
            )
            total_views += stats['views']

        # Record daily views for trend tracking
        from datetime import date
        db.record_daily_views(creator.id, total_views, date.today())

        return {
            'success': True,
            'message': f'Updated {len(video_stats)} videos',
            'refreshed': True,
            'videos_updated': len(video_stats),
            'total_views': total_views
        }

    except Exception as e:
        logger.error(f"Failed to refresh video stats for {creator.name}: {e}")
        return {'success': False, 'error': str(e), 'refreshed': False}


def get_creator_video_summary(creator_id: int) -> dict:
    """Get a summary of video stats for a creator."""
    total_views = db.get_creator_total_views(creator_id)
    video_count = db.get_creator_video_count(creator_id)
    top_video = db.get_top_video(creator_id)
    last_updated = db.get_stats_last_updated(creator_id)
    views_7d = db.get_views_change(creator_id, days=7)

    return {
        'total_views': total_views,
        'video_count': video_count,
        'avg_views': round(total_views / video_count, 1) if video_count > 0 else 0,
        'views_7d': views_7d,
        'top_video': {
            'id': top_video.youtube_video_id,
            'title': top_video.title,
            'thumbnail_url': top_video.thumbnail_url,
            'views': top_video.views
        } if top_video else None,
        'last_updated': last_updated.isoformat() if last_updated else None,
        'last_updated_str': time_since(last_updated) if last_updated else 'Never'
    }


# ========== Page Routes ==========

@app.route('/')
def dashboard():
    """Dashboard home page - shows all creators with stats."""
    db.reset_daily_counts()  # Reset counts if new day

    creators = db.get_all_creators(active_only=False)
    creator_data = []

    for c in creators:
        status = get_creator_status(c)
        # Add video stats summary
        video_summary = get_creator_video_summary(c.id)
        status['total_views'] = video_summary['total_views']
        status['video_count'] = video_summary['video_count']
        status['views_7d'] = video_summary['views_7d']
        status['stats_last_updated'] = video_summary['last_updated_str']
        creator_data.append(status)

    # Calculate totals
    total_shorts = sum(c['shorts_ready'] for c in creator_data)
    total_uploads_today = sum(c['uploads_today'] for c in creator_data)
    total_quota_target = sum(c['posts_per_day'] for c in creator_data)

    # Get global video stats
    global_stats = db.get_global_stats()
    top_video = db.get_top_video()

    # Estimate quota usage
    quota_info = estimate_quota_usage(total_uploads_today)

    return render_template('dashboard.html',
        creators=creator_data,
        total_shorts=total_shorts,
        total_uploads_today=total_uploads_today,
        total_quota_target=total_quota_target,
        quota_info=quota_info,
        global_stats=global_stats,
        top_video=top_video,
        now=datetime.utcnow()
    )


@app.route('/creator/<int:creator_id>')
def creator_detail(creator_id: int):
    """Creator detail page."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        flash('Creator not found', 'error')
        return redirect(url_for('dashboard'))

    creator_status = get_creator_status(creator)

    # Get video statistics summary
    video_summary = get_creator_video_summary(creator_id)
    top_video = db.get_top_video(creator_id)
    bottom_video = db.get_bottom_video(creator_id)
    views_trend = db.get_views_trend(creator_id, days=30)

    # Get uploaded videos grid (sorted by newest by default)
    sort_by = request.args.get('sort', 'uploaded_at')
    sort_dir = request.args.get('dir', 'DESC')
    video_grid = db.get_creator_video_stats(creator_id, order_by=sort_by, order_dir=sort_dir, limit=100)

    # Get upload history
    upload_logs = db.get_upload_logs(creator_id=creator_id, limit=50)

    # Get pending videos from sheet (if possible)
    pending_videos = []
    try:
        oauth = OAuthManager()
        credentials, _ = oauth.get_valid_credentials(
            creator.access_token,
            creator.refresh_token,
            creator.token_expiry
        )
        sheets = SheetsManager(credentials, creator.sheet_id)
        pending = sheets.get_pending_shorts()[:10]  # Get first 10
        pending_videos = [{
            'post_id': v.post_id,
            'caption': v.caption[:100] + '...' if len(v.caption) > 100 else v.caption,
            'duration': format_duration(v.duration),
            'is_short': v.is_short,
            'thumbnail_url': v.thumbnail_url
        } for v in pending]
    except Exception as e:
        logger.warning(f"Could not fetch pending videos: {e}")

    return render_template('creator_detail.html',
        creator=creator_status,
        video_summary=video_summary,
        top_video=top_video,
        bottom_video=bottom_video,
        views_trend=views_trend,
        video_grid=video_grid,
        upload_logs=upload_logs,
        pending_videos=pending_videos,
        sort_by=sort_by,
        sort_dir=sort_dir,
        format_duration=format_duration,
        time_since=time_since
    )


@app.route('/add-creator')
def add_creator_page():
    """Add creator page."""
    config = get_config()
    return render_template('add_creator.html',
        default_posts_per_day=config['defaults']['posts_per_day'],
        default_privacy=config['defaults']['privacy_status']
    )


@app.route('/settings')
def settings_page():
    """Settings page."""
    config = get_config()

    # Check Google Cloud connection
    google_configured = bool(config['google']['client_id'] and config['google']['client_secret'])

    # Get current credentials for display (masked)
    current_client_id = config['google'].get('client_id', '')
    current_client_secret = config['google'].get('client_secret', '')

    # Mask the client secret for display
    if current_client_secret and len(current_client_secret) > 8:
        current_client_secret_masked = current_client_secret[:4] + '*' * (len(current_client_secret) - 8) + current_client_secret[-4:]
    else:
        current_client_secret_masked = ''

    return render_template('settings.html',
        config=config,
        google_configured=google_configured,
        current_client_id=current_client_id,
        current_client_secret_masked=current_client_secret_masked,
        config_path=str(PROJECT_ROOT / 'config.yaml')
    )


@app.route('/history')
def history_page():
    """Upload history page."""
    # Get filter parameters
    creator_id = request.args.get('creator_id', type=int)
    status = request.args.get('status')
    limit = request.args.get('limit', 100, type=int)

    # Get all creators for filter dropdown
    creators = db.get_all_creators(active_only=False)

    # Get upload logs
    logs = db.get_upload_logs(creator_id=creator_id, status=status, limit=limit)

    # Add creator names to logs
    creator_map = {c.id: c.name for c in creators}
    logs_with_names = []
    for log in logs:
        log_dict = {
            'id': log.id,
            'creator_id': log.creator_id,
            'creator_name': creator_map.get(log.creator_id, 'Unknown'),
            'post_id': log.post_id,
            'youtube_video_id': log.youtube_video_id,
            'status': log.status,
            'error_message': log.error_message,
            'video_duration': log.video_duration,
            'uploaded_at': log.uploaded_at,
            'uploaded_at_str': time_since(log.uploaded_at)
        }
        logs_with_names.append(log_dict)

    return render_template('history.html',
        logs=logs_with_names,
        creators=creators,
        selected_creator_id=creator_id,
        selected_status=status,
        format_duration=format_duration
    )


# ========== API Routes ==========

@app.route('/api/creators', methods=['GET'])
def api_get_creators():
    """Get all creators."""
    creators = db.get_all_creators(active_only=False)
    return jsonify([get_creator_status(c) for c in creators])


@app.route('/api/creators/<int:creator_id>', methods=['GET'])
def api_get_creator(creator_id: int):
    """Get a single creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404
    return jsonify(get_creator_status(creator))


@app.route('/api/creators/<int:creator_id>', methods=['PUT'])
def api_update_creator(creator_id: int):
    """Update creator settings."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    data = request.get_json()
    updates = {}

    if 'posts_per_day' in data:
        updates['posts_per_day'] = int(data['posts_per_day'])
    if 'default_privacy' in data:
        updates['default_privacy'] = data['default_privacy']
    if 'active' in data:
        updates['active'] = bool(data['active'])
    if 'sheet_id' in data:
        updates['sheet_id'] = data['sheet_id']

    if updates:
        db.update_creator(creator.name, **updates)
        # Refresh stats if sheet changed
        if 'sheet_id' in updates:
            creator = db.get_creator_by_id(creator_id)
            refresh_creator_stats(creator)

    return jsonify({'success': True, 'creator': get_creator_status(db.get_creator_by_id(creator_id))})


@app.route('/api/creators/<int:creator_id>', methods=['DELETE'])
def api_delete_creator(creator_id: int):
    """Delete a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    db.remove_creator(creator.name)
    return jsonify({'success': True})


@app.route('/api/creators/<int:creator_id>/refresh-stats', methods=['POST'])
def api_refresh_stats(creator_id: int):
    """Refresh content stats for a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    result = refresh_creator_stats(creator)
    if result['success']:
        return jsonify({'success': True, 'creator': get_creator_status(db.get_creator_by_id(creator_id))})
    return jsonify({'error': result['error']}), 500


@app.route('/api/refresh-all-stats', methods=['POST'])
def api_refresh_all_stats():
    """Refresh stats for all creators."""
    creators = db.get_all_creators()
    results = []

    for creator in creators:
        result = refresh_creator_stats(creator)
        results.append({
            'creator': creator.name,
            'success': result['success'],
            'error': result.get('error')
        })

    return jsonify({'results': results})


@app.route('/api/creators/<int:creator_id>/refresh-video-stats', methods=['POST'])
def api_refresh_video_stats(creator_id: int):
    """Refresh YouTube video statistics for a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    data = request.get_json() or {}
    force = data.get('force', False)

    result = refresh_video_stats(creator, force=force)
    if result['success']:
        # Get updated summary
        summary = get_creator_video_summary(creator_id)
        return jsonify({
            'success': True,
            'refreshed': result.get('refreshed', False),
            'message': result.get('message', ''),
            'videos_updated': result.get('videos_updated', 0),
            'summary': summary
        })
    return jsonify({'error': result.get('error', 'Unknown error')}), 500


@app.route('/api/refresh-all-video-stats', methods=['POST'])
def api_refresh_all_video_stats():
    """Refresh video stats for all active creators."""
    data = request.get_json() or {}
    force = data.get('force', False)

    creators = db.get_all_creators()
    results = []

    for creator in creators:
        result = refresh_video_stats(creator, force=force)
        results.append({
            'creator': creator.name,
            'success': result['success'],
            'refreshed': result.get('refreshed', False),
            'videos_updated': result.get('videos_updated', 0),
            'error': result.get('error')
        })

    return jsonify({'results': results})


@app.route('/api/creators/<int:creator_id>/videos', methods=['GET'])
def api_get_creator_videos(creator_id: int):
    """Get video grid data for a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    sort_by = request.args.get('sort', 'uploaded_at')
    sort_dir = request.args.get('dir', 'DESC')
    limit = request.args.get('limit', 100, type=int)

    videos = db.get_creator_video_stats(creator_id, order_by=sort_by, order_dir=sort_dir, limit=limit)

    return jsonify({
        'videos': [{
            'id': v.youtube_video_id,
            'title': v.title,
            'thumbnail_url': v.thumbnail_url,
            'views': v.views,
            'likes': v.likes,
            'comments': v.comments,
            'duration_seconds': v.duration_seconds,
            'uploaded_at': v.uploaded_at.isoformat() if v.uploaded_at else None,
            'youtube_url': f'https://youtube.com/shorts/{v.youtube_video_id}'
        } for v in videos],
        'total': len(videos)
    })


@app.route('/api/creators/<int:creator_id>/views-trend', methods=['GET'])
def api_get_views_trend(creator_id: int):
    """Get views trend data for Chart.js."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    days = request.args.get('days', 30, type=int)
    trend = db.get_views_trend(creator_id, days=days)

    return jsonify({
        'labels': [d['date'] for d in trend],
        'data': [d['views'] for d in trend]
    })


@app.route('/api/global-stats', methods=['GET'])
def api_get_global_stats():
    """Get global statistics across all creators."""
    global_stats = db.get_global_stats()
    top_video = db.get_top_video()

    return jsonify({
        'total_views': global_stats['total_views'],
        'total_videos': global_stats['total_videos'],
        'avg_views': global_stats['avg_views'],
        'top_video': {
            'id': top_video.youtube_video_id,
            'title': top_video.title,
            'thumbnail_url': top_video.thumbnail_url,
            'views': top_video.views,
            'youtube_url': f'https://youtube.com/shorts/{top_video.youtube_video_id}'
        } if top_video else None
    })


# ========== OAuth Flow ==========

@app.route('/api/oauth/start', methods=['POST'])
def api_oauth_start():
    """Start OAuth flow - returns authorization URL."""
    data = request.get_json()

    creator_name = data.get('name')
    sheet_id = data.get('sheet_id')
    posts_per_day = data.get('posts_per_day', 3)

    if not creator_name or not sheet_id:
        return jsonify({'error': 'Name and Sheet ID are required'}), 400

    # Check if creator already exists
    if db.get_creator(creator_name):
        return jsonify({'error': f'Creator "{creator_name}" already exists'}), 400

    # Store pending creator info in session
    session['pending_creator'] = {
        'name': creator_name,
        'sheet_id': sheet_id,
        'posts_per_day': posts_per_day
    }

    try:
        config = get_config()
        client_id = config['google']['client_id']
        client_secret = config['google']['client_secret']

        if not client_id or not client_secret:
            return jsonify({'error': 'Google OAuth not configured. Add your credentials to credentials.py'}), 400

        # Build authorization URL
        from google_auth_oauthlib.flow import Flow

        redirect_uri = get_oauth_redirect_uri('oauth_callback')
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=SCOPES
        )
        flow.redirect_uri = redirect_uri

        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )

        session['oauth_state'] = state

        return jsonify({'authorization_url': authorization_url})

    except Exception as e:
        logger.error(f"OAuth start error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/oauth/callback')
def oauth_callback():
    """OAuth callback handler."""
    error = request.args.get('error')
    if error:
        flash(f'OAuth error: {error}', 'error')
        return redirect(url_for('add_creator_page'))

    code = request.args.get('code')
    if not code:
        flash('No authorization code received', 'error')
        return redirect(url_for('add_creator_page'))

    pending = session.get('pending_creator')
    if not pending:
        flash('Session expired. Please try again.', 'error')
        return redirect(url_for('add_creator_page'))

    try:
        config = get_config()
        from google_auth_oauthlib.flow import Flow
        from datetime import timedelta

        redirect_uri = get_oauth_redirect_uri('oauth_callback')
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": config['google']['client_id'],
                    "client_secret": config['google']['client_secret'],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=SCOPES,
            state=session.get('oauth_state')
        )
        flow.redirect_uri = redirect_uri

        # Exchange code for tokens
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Get channel info
        youtube = YouTubeClient(credentials)
        channel_info = youtube.get_channel_info()

        if not channel_info:
            flash('Could not find YouTube channel for this account', 'error')
            return redirect(url_for('add_creator_page'))

        # Verify sheet access
        sheets_client = SheetsClient(credentials)
        if not sheets_client.verify_sheet_access(pending['sheet_id']):
            flash('Cannot access the specified Google Sheet. Make sure it is shared with this account.', 'error')
            return redirect(url_for('add_creator_page'))

        # Calculate token expiry
        token_expiry = datetime.utcnow() + timedelta(seconds=3600)
        if credentials.expiry:
            token_expiry = credentials.expiry.replace(tzinfo=None)

        # Add creator to database
        creator_id = db.add_creator(
            name=pending['name'],
            channel_id=channel_info['id'],
            sheet_id=pending['sheet_id'],
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_expiry=token_expiry,
            posts_per_day=pending['posts_per_day'],
            default_privacy=config['defaults']['privacy_status']
        )

        # Refresh stats
        creator = db.get_creator_by_id(creator_id)
        refresh_creator_stats(creator)

        # Clean up session
        session.pop('pending_creator', None)
        session.pop('oauth_state', None)

        flash(f'Successfully added creator "{pending["name"]}" (Channel: {channel_info["title"]})', 'success')
        return redirect(url_for('creator_detail', creator_id=creator_id))

    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        flash(f'Error completing authorization: {e}', 'error')
        return redirect(url_for('add_creator_page'))


@app.route('/api/creators/<int:creator_id>/reauth', methods=['POST'])
def api_reauth(creator_id: int):
    """Re-authenticate a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    # Store creator info for re-auth
    session['reauth_creator_id'] = creator_id
    session['pending_creator'] = {
        'name': creator.name,
        'sheet_id': creator.sheet_id,
        'posts_per_day': creator.posts_per_day,
        'is_reauth': True
    }

    try:
        config = get_config()
        from google_auth_oauthlib.flow import Flow

        redirect_uri = get_oauth_redirect_uri('oauth_callback_reauth')
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": config['google']['client_id'],
                    "client_secret": config['google']['client_secret'],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=SCOPES
        )
        flow.redirect_uri = redirect_uri

        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )

        session['oauth_state'] = state

        return jsonify({'authorization_url': authorization_url})

    except Exception as e:
        logger.error(f"Reauth error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/oauth/callback/reauth')
def oauth_callback_reauth():
    """OAuth callback for re-authentication."""
    error = request.args.get('error')
    if error:
        flash(f'OAuth error: {error}', 'error')
        return redirect(url_for('dashboard'))

    code = request.args.get('code')
    creator_id = session.get('reauth_creator_id')

    if not code or not creator_id:
        flash('Session expired. Please try again.', 'error')
        return redirect(url_for('dashboard'))

    creator = db.get_creator_by_id(creator_id)
    if not creator:
        flash('Creator not found', 'error')
        return redirect(url_for('dashboard'))

    try:
        config = get_config()
        from google_auth_oauthlib.flow import Flow
        from datetime import timedelta

        redirect_uri = get_oauth_redirect_uri('oauth_callback_reauth')
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": config['google']['client_id'],
                    "client_secret": config['google']['client_secret'],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=SCOPES,
            state=session.get('oauth_state')
        )
        flow.redirect_uri = redirect_uri

        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Calculate token expiry
        token_expiry = datetime.utcnow() + timedelta(seconds=3600)
        if credentials.expiry:
            token_expiry = credentials.expiry.replace(tzinfo=None)

        # Update tokens
        db.update_tokens(
            creator.name,
            credentials.token,
            credentials.refresh_token,
            token_expiry
        )

        # Clean up session
        session.pop('reauth_creator_id', None)
        session.pop('oauth_state', None)

        flash(f'Successfully re-authenticated "{creator.name}"', 'success')
        return redirect(url_for('creator_detail', creator_id=creator_id))

    except Exception as e:
        logger.error(f"Reauth callback error: {e}")
        flash(f'Error completing re-authentication: {e}', 'error')
        return redirect(url_for('creator_detail', creator_id=creator_id))


# ========== Upload Routes ==========

@app.route('/api/upload/<int:creator_id>', methods=['POST'])
def api_upload_creator(creator_id: int):
    """Trigger upload for a single creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    data = request.get_json() or {}
    dry_run = data.get('dry_run', False)
    force = data.get('force', False)
    limit = data.get('limit', 1)

    # Check if can upload
    can_upload, reason = db.can_upload(creator, force)
    if not can_upload and not force:
        return jsonify({'error': reason, 'can_upload': False}), 400

    # Start upload in background thread
    upload_id = f"{creator_id}_{datetime.now().timestamp()}"
    upload_progress[upload_id] = {'status': 'starting', 'progress': 0, 'logs': []}

    def run_upload():
        try:
            upload_progress[upload_id]['status'] = 'running'
            upload_progress[upload_id]['logs'].append(f"Starting upload for {creator.name}...")

            oauth = OAuthManager()
            credentials, was_refreshed = oauth.get_valid_credentials(
                creator.access_token,
                creator.refresh_token,
                creator.token_expiry
            )

            if was_refreshed:
                expiry = credentials.expiry.replace(tzinfo=None) if credentials.expiry else None
                db.update_tokens(creator.name, credentials.token, token_expiry=expiry)

            sheets = SheetsManager(credentials, creator.sheet_id)
            youtube = YouTubeUploader(credentials)
            downloader = DriveDownloader(credentials)

            # Get videos to upload
            shorts, too_long = sheets.get_videos_to_upload(limit)

            if not shorts:
                upload_progress[upload_id]['status'] = 'completed'
                upload_progress[upload_id]['logs'].append("No pending Shorts found")
                return

            uploaded = 0
            failed = 0

            for i, video in enumerate(shorts):
                upload_progress[upload_id]['logs'].append(f"[{i+1}/{len(shorts)}] Processing: {video.post_id}")
                upload_progress[upload_id]['progress'] = int((i / len(shorts)) * 100)

                if dry_run:
                    upload_progress[upload_id]['logs'].append(f"  [DRY RUN] Would upload: {video.post_id}")
                    uploaded += 1
                    continue

                try:
                    # Download
                    upload_progress[upload_id]['logs'].append("  Downloading from Drive...")
                    video_path = downloader.download_from_url(video.video_link)

                    # Generate metadata
                    metadata = generate_shorts_metadata(
                        caption=video.caption,
                        post_id=video.post_id,
                        original_url=video.original_url,
                        privacy_status=creator.default_privacy
                    )

                    # Upload
                    upload_progress[upload_id]['logs'].append("  Uploading to YouTube...")
                    result = youtube.upload_video(video_path, metadata)

                    # Cleanup
                    downloader.cleanup_temp_file(video_path)

                    if result.success:
                        uploaded += 1
                        db.record_upload(creator.id, video.post_id, result.video_id, 'success', video_duration=video.duration)
                        db.update_upload_count(creator.id)
                        sheets.mark_as_uploaded(video.row_number, result.video_id)
                        upload_progress[upload_id]['logs'].append(f"  SUCCESS: {result.shorts_url}")
                    else:
                        failed += 1
                        db.record_upload(creator.id, video.post_id, None, 'failed', result.error_message, video.duration)
                        sheets.mark_as_failed(video.row_number, result.error_message)
                        upload_progress[upload_id]['logs'].append(f"  FAILED: {result.error_message}")

                except Exception as e:
                    failed += 1
                    upload_progress[upload_id]['logs'].append(f"  ERROR: {str(e)}")
                    logger.exception(f"Upload error for {video.post_id}")

            upload_progress[upload_id]['status'] = 'completed'
            upload_progress[upload_id]['progress'] = 100
            upload_progress[upload_id]['logs'].append(f"Completed: {uploaded} uploaded, {failed} failed")

            # Refresh stats
            refresh_creator_stats(db.get_creator_by_id(creator_id))

        except Exception as e:
            upload_progress[upload_id]['status'] = 'error'
            upload_progress[upload_id]['logs'].append(f"ERROR: {str(e)}")
            logger.exception(f"Upload error for creator {creator_id}")

    thread = threading.Thread(target=run_upload)
    thread.start()

    return jsonify({'upload_id': upload_id, 'status': 'started'})


@app.route('/api/upload/all', methods=['POST'])
def api_upload_all():
    """Trigger upload for all active creators."""
    data = request.get_json() or {}
    dry_run = data.get('dry_run', False)
    force = data.get('force', False)

    creators = db.get_all_creators()
    upload_ids = []

    for creator in creators:
        can_upload, _ = db.can_upload(creator, force)
        if can_upload or force:
            # Trigger upload for each creator
            response = api_upload_creator(creator.id)
            if response.status_code == 200:
                result = response.get_json()
                upload_ids.append({'creator': creator.name, 'upload_id': result['upload_id']})

    return jsonify({'uploads': upload_ids})


@app.route('/api/upload/<upload_id>/status', methods=['GET'])
def api_upload_status(upload_id: str):
    """Get upload progress status."""
    if upload_id not in upload_progress:
        return jsonify({'error': 'Upload not found'}), 404

    return jsonify(upload_progress[upload_id])


@app.route('/api/upload/<upload_id>/stream')
def api_upload_stream(upload_id: str):
    """Stream upload logs via Server-Sent Events."""
    def generate():
        last_log_count = 0
        while True:
            if upload_id not in upload_progress:
                yield f"data: {json.dumps({'error': 'Upload not found'})}\n\n"
                break

            progress = upload_progress[upload_id]
            current_logs = progress.get('logs', [])

            # Send new logs
            if len(current_logs) > last_log_count:
                for log in current_logs[last_log_count:]:
                    yield f"data: {json.dumps({'log': log, 'status': progress['status'], 'progress': progress.get('progress', 0)})}\n\n"
                last_log_count = len(current_logs)

            if progress['status'] in ('completed', 'error'):
                yield f"data: {json.dumps({'status': progress['status'], 'done': True})}\n\n"
                break

            import time
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ========== Settings API ==========

@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get current settings."""
    config = get_config()
    return jsonify({
        'defaults': config['defaults'],
        'paths': config['paths'],
        'google_configured': bool(config['google']['client_id'] and config['google']['client_secret'])
    })


@app.route('/api/settings', methods=['PUT'])
def api_update_settings():
    """Update settings in config.yaml."""
    data = request.get_json()

    config_path = PROJECT_ROOT / 'config.yaml'

    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Update allowed settings
        if 'defaults' in data:
            for key in ['privacy_status', 'category_id', 'posts_per_day', 'max_video_duration']:
                if key in data['defaults']:
                    config['defaults'][key] = data['defaults'][key]

        if 'paths' in data:
            for key in ['temp_downloads', 'logs', 'database']:
                if key in data['paths']:
                    config['paths'][key] = data['paths'][key]

        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)

        return jsonify({'success': True, 'config': config})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/credentials', methods=['POST'])
def api_save_credentials():
    """Save Google API credentials to credentials.py file."""
    data = request.get_json()

    client_id = data.get('client_id', '').strip()
    client_secret = data.get('client_secret', '').strip()

    if not client_id or not client_secret:
        return jsonify({'error': 'Both Client ID and Client Secret are required'}), 400

    # Don't save if the secret is masked (unchanged)
    if '*' in client_secret:
        # User didn't change the secret, only update client_id
        config = get_config()
        client_secret = config['google'].get('client_secret', '')

    credentials_path = PROJECT_ROOT / 'credentials.py'

    try:
        # Read existing content or use template
        if credentials_path.exists():
            content = credentials_path.read_text()
        else:
            content = '''# credentials.py - Google API Credentials
# This file is auto-generated and should not be committed to git

GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""

# Encryption key for storing OAuth tokens securely
# Leave empty to auto-generate one (recommended for first-time setup)
ENCRYPTION_KEY = ""

# Flask session secret (leave empty to auto-generate)
SESSION_SECRET = ""
'''

        # Update GOOGLE_CLIENT_ID
        import re
        content = re.sub(
            r'GOOGLE_CLIENT_ID\s*=\s*["\'].*?["\']',
            f'GOOGLE_CLIENT_ID = "{client_id}"',
            content
        )

        # Update GOOGLE_CLIENT_SECRET
        content = re.sub(
            r'GOOGLE_CLIENT_SECRET\s*=\s*["\'].*?["\']',
            f'GOOGLE_CLIENT_SECRET = "{client_secret}"',
            content
        )

        # Write back
        credentials_path.write_text(content)

        # Reload credentials module to pick up changes
        reload_credentials()

        logger.info("Google API credentials saved successfully")
        return jsonify({'success': True, 'message': 'Credentials saved successfully'})

    except Exception as e:
        logger.error(f"Failed to save credentials: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/credentials', methods=['DELETE'])
def api_clear_credentials():
    """Clear Google API credentials from credentials.py file."""
    credentials_path = PROJECT_ROOT / 'credentials.py'

    try:
        if credentials_path.exists():
            content = credentials_path.read_text()

            import re
            # Clear GOOGLE_CLIENT_ID
            content = re.sub(
                r'GOOGLE_CLIENT_ID\s*=\s*["\'].*?["\']',
                'GOOGLE_CLIENT_ID = ""',
                content
            )

            # Clear GOOGLE_CLIENT_SECRET
            content = re.sub(
                r'GOOGLE_CLIENT_SECRET\s*=\s*["\'].*?["\']',
                'GOOGLE_CLIENT_SECRET = ""',
                content
            )

            credentials_path.write_text(content)

            # Reload credentials module
            reload_credentials()

        logger.info("Google API credentials cleared")
        return jsonify({'success': True, 'message': 'Credentials cleared successfully'})

    except Exception as e:
        logger.error(f"Failed to clear credentials: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/test-connection', methods=['POST'])
def api_test_connection():
    """Test Google API connection for a creator."""
    data = request.get_json()
    creator_id = data.get('creator_id')

    if not creator_id:
        return jsonify({'error': 'Creator ID required'}), 400

    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    results = verify_all_connections(
        creator.access_token,
        creator.refresh_token,
        creator.token_expiry,
        creator.sheet_id
    )

    return jsonify(results)


# ========== Queue Preview ==========

@app.route('/api/creators/<int:creator_id>/queue', methods=['GET'])
def api_get_queue(creator_id: int):
    """Get pending videos queue for a creator."""
    creator = db.get_creator_by_id(creator_id)
    if not creator:
        return jsonify({'error': 'Creator not found'}), 404

    limit = request.args.get('limit', 20, type=int)

    try:
        oauth = OAuthManager()
        credentials, _ = oauth.get_valid_credentials(
            creator.access_token,
            creator.refresh_token,
            creator.token_expiry
        )

        sheets = SheetsManager(credentials, creator.sheet_id)
        shorts, too_long = sheets.get_videos_to_upload(limit)

        return jsonify({
            'shorts': [{
                'post_id': v.post_id,
                'caption': v.caption,
                'duration': v.duration,
                'duration_str': format_duration(v.duration),
                'thumbnail_url': v.thumbnail_url,
                'original_url': v.original_url
            } for v in shorts],
            'too_long': [{
                'post_id': v.post_id,
                'caption': v.caption,
                'duration': v.duration,
                'duration_str': format_duration(v.duration)
            } for v in too_long]
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ========== Error Handlers ==========

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('500.html'), 500


# ========== Main ==========

if __name__ == '__main__':
    # Ensure required directories exist
    (PROJECT_ROOT / 'templates').mkdir(exist_ok=True)
    (PROJECT_ROOT / 'static').mkdir(exist_ok=True)

    print("\n" + "="*60)
    print("  YouTube Shorts Uploader - Web Dashboard")
    print("="*60)
    print(f"\n  Open in browser: http://localhost:5000")
    print("\n  Press Ctrl+C to stop\n")

    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
