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


def _add_missing_columns(table_name: str, additions: dict[str, str]):
    # create_all() only creates tables that don't exist yet — it never adds
    # columns to a table that's already there. Existing installs' app.db
    # predates columns added to a model after its table first shipped, so
    # bring them up to date by hand, one ALTER TABLE per missing column.
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl_type in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl_type}"))


def init_db():
    Base.metadata.create_all(bind=engine)
    _add_missing_columns("job_history", {
        "reference": "VARCHAR(255)",
        "source_filename": "VARCHAR(500)",
        "row_count": "INTEGER",
        "reference_count": "INTEGER",
    })
    _add_missing_columns("order_tracking", {
        "screenshot_status": "VARCHAR(20)",
        "screenshot_error": "VARCHAR(500)",
    })
