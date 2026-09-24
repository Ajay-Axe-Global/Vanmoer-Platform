"""Shared local-backup + Drive-sync sequence, used by both the hourly
scheduler (main.py) and the admin panel's manual backup button
(routes/admin_routes.py) so the two stay in sync."""
from database.backup import backup_now
from database.drive_sync import sync_to_drive


def backup_and_sync():
    # Local snapshot first so the Drive copy of app_backup.db reflects this
    # run's data, not the previous hour's.
    backup_now()
    sync_to_drive()
