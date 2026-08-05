import os
from pathlib import Path

from sqlalchemy import create_engine, event
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


def init_db():
    Base.metadata.create_all(bind=engine)
