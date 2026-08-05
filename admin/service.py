"""
Admin business logic — user/client/task management + reporting. Kept separate
from routes/admin_routes.py (which stays thin HTTP glue) so it can also be
called from scripts/tests without going through Flask.
"""

from sqlalchemy import func

from database.backup import backup_now
from database.db import SessionLocal
from database.models import Client, JobHistory, Task, User, UserTaskAccess
from helpers.jwt_utils import hash_password


def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def list_clients() -> list[dict]:
    session = SessionLocal()
    try:
        return [{"id": c.id, "name": c.name, "slug": c.slug}
                for c in session.query(Client).order_by(Client.name).all()]
    finally:
        session.close()


def list_tasks() -> list[dict]:
    session = SessionLocal()
    try:
        return [{"id": t.id, "name": t.name, "slug": t.slug}
                for t in session.query(Task).order_by(Task.name).all()]
    finally:
        session.close()


def create_client(name: str) -> dict:
    session = SessionLocal()
    try:
        slug = _slugify(name)
        if session.query(Client).filter_by(slug=slug).first():
            raise ValueError(f"Client '{name}' already exists")
        client = Client(name=name.strip(), slug=slug)
        session.add(client)
        session.commit()
        result = {"id": client.id, "name": client.name, "slug": client.slug}
    finally:
        session.close()
    backup_now()
    return result


def list_users() -> list[dict]:
    session = SessionLocal()
    try:
        users = session.query(User).order_by(User.username).all()
        return [{
            "id": u.id,
            "name": u.name,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "grants": [
                {"client": g.client.name, "client_slug": g.client.slug,
                 "task": g.task.name, "task_slug": g.task.slug}
                for g in u.grants
            ],
        } for u in users]
    finally:
        session.close()


def _resolve_grants(session, role: str, grants: list[dict] | None) -> list[tuple]:
    """grants: [{"client_slug": ..., "task_slug": ...}, ...]. Returns [(Client, Task), ...]."""
    if role != "user":
        return []
    grants = grants or []
    if not grants:
        raise ValueError("at least one client/task grant is required for a non-admin user")

    resolved = []
    seen = set()
    for g in grants:
        client_slug, task_slug = g.get("client_slug"), g.get("task_slug")
        if not client_slug or not task_slug:
            raise ValueError("each grant needs a client and a task")
        key = (client_slug, task_slug)
        if key in seen:
            continue
        seen.add(key)
        client = session.query(Client).filter_by(slug=client_slug).first()
        task = session.query(Task).filter_by(slug=task_slug).first()
        if not client or not task:
            raise ValueError(f"Unknown client/task: {client_slug}/{task_slug}")
        resolved.append((client, task))
    return resolved


def create_user(name: str, username: str, password: str, role: str, grants: list[dict] | None) -> dict:
    name = (name or "").strip()
    username = (username or "").strip()
    if not name or not username or not password:
        raise ValueError("name, username and password are required")
    if role not in ("admin", "user"):
        raise ValueError("role must be 'admin' or 'user'")

    session = SessionLocal()
    try:
        if session.query(User).filter_by(username=username).first():
            raise ValueError(f"Username '{username}' is already taken")

        resolved_grants = _resolve_grants(session, role, grants)

        user = User(
            name=name,
            username=username,
            password_hash=hash_password(password),
            role=role,
        )
        session.add(user)
        session.flush()  # assign user.id before attaching grants
        for client, task in resolved_grants:
            session.add(UserTaskAccess(user_id=user.id, client_id=client.id, task_id=task.id))
        session.commit()
        result = {"id": user.id, "username": user.username, "role": user.role}
    finally:
        session.close()
    backup_now()
    return result


def update_user(user_id: int, name: str, username: str, password: str | None, role: str,
                 grants: list[dict] | None) -> dict:
    name = (name or "").strip()
    username = (username or "").strip()
    if not name or not username:
        raise ValueError("name and username are required")
    if role not in ("admin", "user"):
        raise ValueError("role must be 'admin' or 'user'")

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            raise ValueError("User not found")

        existing = session.query(User).filter_by(username=username).first()
        if existing and existing.id != user_id:
            raise ValueError(f"Username '{username}' is already taken")

        resolved_grants = _resolve_grants(session, role, grants)

        user.name = name
        user.username = username
        user.role = role
        if password:  # blank password on edit = keep the existing one
            user.password_hash = hash_password(password)

        # Replace the grant set wholesale — simpler and safer than diffing,
        # and the admin UI always submits the full intended list anyway.
        session.query(UserTaskAccess).filter_by(user_id=user.id).delete()
        for client, task in resolved_grants:
            session.add(UserTaskAccess(user_id=user.id, client_id=client.id, task_id=task.id))

        session.commit()
        result = {"id": user.id, "username": user.username, "role": user.role}
    finally:
        session.close()
    backup_now()
    return result


def set_user_active(user_id: int, active: bool, acting_user_id: int) -> dict:
    if user_id == acting_user_id and not active:
        raise ValueError("You cannot delete your own account while logged in")

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            raise ValueError("User not found")
        user.is_active = active
        session.commit()
        result = {"id": user.id, "username": user.username, "is_active": user.is_active}
    finally:
        session.close()
    backup_now()
    return result


def jobs_by_user() -> list[dict]:
    session = SessionLocal()
    try:
        rows = (
            session.query(User.username, User.name, func.count(JobHistory.id))
            .join(JobHistory, JobHistory.user_id == User.id)
            .group_by(User.id)
            .order_by(User.username)
            .all()
        )
        return [{"username": u, "name": n, "count": c} for u, n, c in rows]
    finally:
        session.close()


def jobs_by_client() -> list[dict]:
    session = SessionLocal()
    try:
        rows = (
            session.query(Client.name, func.count(JobHistory.id))
            .join(JobHistory, JobHistory.client_id == Client.id)
            .group_by(Client.id)
            .order_by(Client.name)
            .all()
        )
        return [{"client": c, "count": n} for c, n in rows]
    finally:
        session.close()
