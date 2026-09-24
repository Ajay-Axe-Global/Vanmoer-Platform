"""Uploads app.db and app_backup.db to Google Drive, overwriting the same
Drive file in place each run instead of creating a new file every time.

Requires database/drive_auth.py to have been run once already (see that
file's docstring) so token.json exists.
"""
import json
import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from database.db import BACKUP_PATH, DB_PATH

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
TOKEN_PATH = BASE_DIR / "token.json"
FILE_IDS_PATH = Path(__file__).parent / "drive_file_ids.json"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _load_file_ids() -> dict:
    if FILE_IDS_PATH.exists():
        return json.loads(FILE_IDS_PATH.read_text())
    return {}


def _save_file_ids(file_ids: dict):
    FILE_IDS_PATH.write_text(json.dumps(file_ids, indent=2))


def _get_drive_service():
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"{TOKEN_PATH} not found — run `python -m database.drive_auth` once to authorize."
        )

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_one(service, file_ids: dict, local_path: str, drive_name: str):
    media = MediaFileUpload(local_path, mimetype="application/x-sqlite3", resumable=True)
    file_id = file_ids.get(drive_name)

    if file_id:
        try:
            service.files().update(fileId=file_id, media_body=media).execute()
            return
        except HttpError as e:
            if e.resp.status != 404:
                raise
            # File was deleted/moved on the Drive side out from under us —
            # fall through and recreate it below.
            logger.warning("Drive file %s (id=%s) missing, recreating", drive_name, file_id)

    created = service.files().create(
        body={"name": drive_name}, media_body=media, fields="id"
    ).execute()
    file_ids[drive_name] = created["id"]


def sync_to_drive():
    """Overwrite app.db and app_backup.db on Drive with the current local
    copies. Never raises — a Drive outage must not take down the app or the
    local backup schedule, so failures are logged and swallowed."""
    try:
        service = _get_drive_service()
        file_ids = _load_file_ids()
        _upload_one(service, file_ids, DB_PATH, "app.db")
        _upload_one(service, file_ids, BACKUP_PATH, "app_backup.db")
        _save_file_ids(file_ids)
    except RefreshError:
        logger.error(
            "Google Drive token refresh failed (likely revoked) — "
            "re-run `python -m database.drive_auth` to re-authorize."
        )
    except Exception:
        logger.exception("Google Drive sync failed")
