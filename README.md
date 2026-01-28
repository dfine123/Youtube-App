# YouTube Shorts Uploader

Automate YouTube **Shorts** uploads for multiple creator accounts. This system reads video queues from Google Sheets, uploads ONLY videos under 60 seconds as Shorts to their respective YouTube channels, and tracks upload status.

## Key Features

- **Shorts-Only System**: Videos 60 seconds or longer are automatically skipped
- **Multi-Creator Support**: Manage multiple YouTube channels from one system
- **Google Sheets Integration**: Read from existing content library spreadsheets
- **Smart Scheduling**: 2-hour spacing between uploads, configurable posts per day
- **Content Runway Tracking**: Know how many days of content you have remaining
- **Encrypted Token Storage**: OAuth tokens stored securely with Fernet encryption
- **Resumable Uploads**: Handles interruptions gracefully

## Quick Start

### 1. Google Cloud Console Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable the following APIs:
   - YouTube Data API v3
   - Google Sheets API
   - Google Drive API
4. Go to **APIs & Services > Credentials**
5. Click **Create Credentials > OAuth 2.0 Client ID**
6. Select **Desktop app** as application type
7. Download the credentials (you'll need the Client ID and Client Secret)

### 2. Installation

```bash
# Clone or download the repository
cd youtube-uploader

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
```

### 3. Configuration

Edit `.env` with your Google OAuth credentials:

```bash
# Get these from Google Cloud Console
GOOGLE_CLIENT_ID=your_client_id_here
GOOGLE_CLIENT_SECRET=your_client_secret_here

# Generate encryption key (run this command and paste the output)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=paste_generated_key_here
```

### 4. Add Your First Creator

```bash
python manage.py add-creator
```

This will:
1. Prompt for creator name and Google Sheet ID
2. Open browser for YouTube OAuth authorization
3. Verify channel access and sheet access
4. Display content runway statistics

### 5. Run Uploads

```bash
# Test with dry run first
python upload.py --creator "CreatorName" --dry-run

# Upload single video
python upload.py --creator "CreatorName" --limit 1

# Upload all available (respects daily limit & 2hr spacing)
python upload.py --creator "CreatorName"
```

## CLI Commands

### manage.py - Creator Management

```bash
# Add new creator (interactive OAuth flow)
python manage.py add-creator

# List all creators with content runway
python manage.py list

# Detailed status dashboard
python manage.py status

# Show specific creator details
python manage.py show "CreatorName"

# Update creator settings
python manage.py update "CreatorName" --posts-per-day 5
python manage.py update "CreatorName" --privacy public

# Remove creator
python manage.py remove "CreatorName"

# Re-authenticate creator (refresh OAuth)
python manage.py reauth "CreatorName"

# Test all connections
python manage.py test "CreatorName"

# Refresh content stats from sheets
python manage.py refresh-stats
```

### upload.py - Upload Runner

```bash
# Process uploads for one creator
python upload.py --creator "CreatorName"

# Upload single next video
python upload.py --creator "CreatorName" --limit 1

# Process all creators
python upload.py --all

# Dry run - show what would happen
python upload.py --creator "CreatorName" --dry-run

# Force upload ignoring timing restrictions
python upload.py --creator "CreatorName" --force

# Show queue status without uploading
python upload.py --status
```

## Google Sheet Structure

Your content library spreadsheet should have this structure:

| Column | Field | Description |
|--------|-------|-------------|
| A | Post ID | Unique identifier |
| B | Video Drive Link | Google Drive download URL |
| C | Views | Original views count |
| D | Likes | Original likes count |
| E | Comments | Original comments count |
| F | Caption | Video caption/description |
| G | Post Date | Original post date |
| H | Video Duration | Duration in seconds (CRITICAL) |
| I | Thumbnail URL | Thumbnail image URL |
| J | Original Post URL | Link to original content |
| K | Posted to YT | TRUE/FALSE (system updates this) |

The system will automatically add columns L, M, N for:
- YT Video ID
- YT Upload Date
- Upload Status

## Scheduling with Cron

The system is designed to run via cron. It handles spacing logic internally.

```bash
# Run every hour - system handles 2-hour spacing
0 * * * * cd /path/to/youtube-uploader && python upload.py --all >> logs/cron.log 2>&1
```

**How scheduling works:**
- Each creator has a `posts_per_day` setting (1-10)
- Uploads are spaced exactly 2 hours apart
- Daily counter resets at midnight UTC
- Running hourly lets the system pick optimal upload times

## Content Runway

The system tracks how many days of content each creator has:

```
days_remaining = pending_shorts / posts_per_day
```

**Runway indicators:**
- 🟢 Green: 14+ days remaining
- 🟡 Yellow: 7-13 days remaining
- 🔴 Red: < 7 days remaining (needs content refresh)
- ⚫ Black: Empty queue

## YouTube API Quota

YouTube API has a daily quota of 10,000 units:
- Each upload costs ~1,600 units
- **Maximum ~6 uploads per day** across all creators

The system displays quota usage estimates after each batch.

## Important Notes

1. **SHORTS ONLY**: Videos >= 60 seconds are automatically skipped
2. **PUBLIC BY DEFAULT**: Videos post as public for Shorts algorithm discovery
3. **2-HOUR SPACING**: Fixed spacing between uploads for optimal algorithm performance
4. **NO CUSTOM THUMBNAILS**: YouTube auto-generates Shorts thumbnails
5. **#SHORTS PREFIX**: Titles automatically include "#Shorts " for discovery

## Troubleshooting

### OAuth Token Expired
```bash
python manage.py reauth "CreatorName"
```

### Cannot Access Sheet
- Ensure the Google Sheet is shared with your Google account
- Check the Sheet ID is correct (from the URL: `docs.google.com/spreadsheets/d/SHEET_ID/edit`)

### Quota Exceeded
- Wait until tomorrow (quota resets daily)
- Or use a different Google Cloud project

### Upload Failed
- Check the logs in `./logs/` directory
- Verify video file is accessible on Google Drive
- Ensure video is < 60 seconds

## Project Structure

```
youtube-uploader/
├── config.yaml           # Configuration settings
├── .env                  # Credentials (not in git)
├── requirements.txt      # Python dependencies
├── manage.py             # Creator management CLI
├── upload.py             # Main upload runner
├── src/
│   ├── __init__.py
│   ├── database.py       # SQLite operations
│   ├── oauth.py          # YouTube OAuth flow
│   ├── sheets.py         # Google Sheets integration
│   ├── downloader.py     # Google Drive downloads
│   ├── uploader.py       # YouTube upload engine
│   ├── metadata.py       # Title/description generator
│   └── utils.py          # Helpers and utilities
├── logs/                 # Log files
└── temp/                 # Temporary downloads
```

## License

MIT License - Feel free to use and modify for your needs.
