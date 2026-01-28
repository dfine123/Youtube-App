# credentials_example.py - Copy this to credentials.py and fill in your values
#
# Get these from Google Cloud Console:
# 1. Go to https://console.cloud.google.com/
# 2. Create a project and enable YouTube Data API v3, Google Sheets API, Google Drive API
# 3. Create OAuth 2.0 credentials (Web application type)
# 4. Add redirect URI: http://localhost:5000/oauth/callback
# 5. Copy your Client ID and Client Secret below

GOOGLE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "your-client-secret"

# Encryption key for storing OAuth tokens securely
# Leave empty to auto-generate one (recommended for first-time setup)
ENCRYPTION_KEY = ""

# Flask session secret (leave empty to auto-generate)
SESSION_SECRET = ""
