"""
Run locally once to get GOOGLE_REFRESH_TOKEN.

1) Put GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET into environment variables
   or paste them below when prompted.
2) Run: python get_google_refresh_token.py
3) Log in to Google in the opened browser.
4) Copy the printed refresh token into Render env var GOOGLE_REFRESH_TOKEN.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

client_id = os.getenv("GOOGLE_CLIENT_ID") or input("GOOGLE_CLIENT_ID: ").strip()
client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or input("GOOGLE_CLIENT_SECRET: ").strip()

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080/"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
print("\nGOOGLE_REFRESH_TOKEN=", creds.refresh_token, sep="")
