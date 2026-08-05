import sqlite3

from database.db import BACKUP_PATH, DB_PATH


def backup_now():
    """Hot-copy the live SQLite DB to the backup path using SQLite's own backup
    API — safe to run while the DB is in use, unlike a raw file copy. Called
    after every admin write (client added, user created) so app_backup.db is
    always a known-good, up-to-date working copy."""
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(BACKUP_PATH)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
