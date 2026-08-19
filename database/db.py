import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker

from database.models import Base

DB_DIR = Path(__file__).parent
DB_PATH = os.getenv("DATABASE_PATH", str(DB_DIR / "app.db"))
BACKUP_PATH = os.getenv("DATABASE_BACKUP_PATH", str(DB_DIR / "app_backup.db"))

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    # WAL lets readers and the writer run concurrently instead of blocking each
    # other; busy_timeout makes a write that hits a momentary lock wait (up to
    # 5s) and retry instead of immediately raising "database is locked" — both
    # matter once more than a handful of users are hitting the app at once.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))


def _migrate_job_history_columns():
    # create_all() only creates tables that don't exist yet — it never adds
    # columns to a table that's already there. app.db predates the
    # reference/source_filename/row_count columns, so bring existing
    # installs up to date by hand.
    inspector = inspect(engine)
    if "job_history" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("job_history")}
    additions = {
        "reference": "VARCHAR(255)",
        "source_filename": "VARCHAR(500)",
        "row_count": "INTEGER",
        "reference_count": "INTEGER",
    }
    with engine.begin() as conn:
        for name, ddl_type in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE job_history ADD COLUMN {name} {ddl_type}"))


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_job_history_columns()
