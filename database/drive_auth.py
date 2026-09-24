"""One-time interactive Google Drive authorization.

Run manually, once, on a machine with a browser:

    python -m database.drive_auth

Opens a local browser window for you to approve access, then writes
token.json next to credentials.json. From then on, drive_sync.py reuses
and silently refreshes that token — no browser needed again unless you
revoke access or delete token.json.
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).parent.parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

# drive.file: the app can only see/modify files it creates itself, not the
# whole Drive — narrowest scope that still lets us create and overwrite
# app.db / app_backup.db.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(f"Missing {CREDENTIALS_PATH} — place your OAuth client secret there first.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Authorized. Token saved to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
