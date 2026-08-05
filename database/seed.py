"""
Bootstraps a fresh app.db: creates tables, seeds Client rows (Carpenter, Sabic),
seeds Task rows (Inbound, Outbound), and creates the first admin login if none
exists yet. Run once: `python -m database.seed`
"""

from database.backup import backup_now
from database.db import SessionLocal, init_db
from database.models import Client, Task, User
from helpers.jwt_utils import hash_password

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"  # CHANGE THIS after first login


def seed():
    init_db()
    session = SessionLocal()
    try:
        for name in ("Carpenter", "Sabic"):
            slug = name.lower()
            if not session.query(Client).filter_by(slug=slug).first():
                session.add(Client(name=name, slug=slug))

        for name in ("Inbound", "Outbound"):
            slug = name.lower()
            if not session.query(Task).filter_by(slug=slug).first():
                session.add(Task(name=name, slug=slug))

        session.commit()

        if not session.query(User).filter_by(role="admin").first():
            session.add(User(
                name="Administrator",
                username=DEFAULT_ADMIN_USERNAME,
                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                role="admin",
            ))
            session.commit()
            print(f"Seeded default admin -> username: {DEFAULT_ADMIN_USERNAME} / "
                  f"password: {DEFAULT_ADMIN_PASSWORD} (change this immediately)")
        else:
            print("Admin already exists, skipping.")
    finally:
        session.close()

    backup_now()
    print("Seed complete. database/app.db and database/app_backup.db are ready.")


if __name__ == "__main__":
    seed()
