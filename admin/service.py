"""
Admin business logic — user/client/task management + reporting. Kept separate
from routes/admin_routes.py (which stays thin HTTP glue) so it can also be
called from scripts/tests without going through Flask.
"""

import datetime

from sqlalchemy import case, func, or_

from database.backup import backup_now
from database.db import SessionLocal
from database.models import Client, JobHistory, Task, User, UserTaskAccess
from helpers.jwt_utils import hash_password


def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _utc_iso(dt: datetime.datetime) -> str:
    """
    JobHistory.timestamp is stored as a naive UTC datetime (datetime.utcnow,
    see database/models.py). Plain dt.isoformat() has no "Z"/offset suffix,
    and a timezone-less ISO string is parsed as LOCAL time by JS's Date
    constructor — silently displaying the raw UTC clock value as if it were
    already the viewer's local time. Appending "Z" here is what makes the
    frontend convert it to the viewer's actual local time correctly.
    """
    return dt.isoformat() + "Z"


def period_range(period: str, since: str | None = None,
                  until: str | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    """Resolves a period keyword into a half-open [since, until) UTC datetime
    range, server-side, so "Today"/"This week"/"This month" mean the same
    thing everywhere instead of being recomputed against the viewer's local
    clock in JS. `since`/`until` are plain "YYYY-MM-DD" date strings, only
    used (and required) when period == "custom"."""
    today = datetime.datetime.utcnow().date()
    tomorrow = today + datetime.timedelta(days=1)

    if period == "custom":
        if not since or not until:
            raise ValueError("since and until are required for a custom period")
        since_date = datetime.date.fromisoformat(since)
        until_date = datetime.date.fromisoformat(until) + datetime.timedelta(days=1)
        if since_date >= until_date:
            raise ValueError("since must be before until")
    elif period == "week":
        # Calendar week, Sunday through Saturday (not a trailing 7-day
        # window) — date.weekday() is Mon=0..Sun=6, so this steps back to
        # the most recent Sunday (today itself, if today is a Sunday).
        since_date = today - datetime.timedelta(days=(today.weekday() + 1) % 7)
        until_date = since_date + datetime.timedelta(days=7)
    elif period == "month":
        since_date, until_date = today.replace(day=1), tomorrow
    elif period == "today":
        since_date, until_date = today, tomorrow
    else:
        raise ValueError(f"Unknown period: {period}")

    return (
        datetime.datetime.combine(since_date, datetime.time.min),
        datetime.datetime.combine(until_date, datetime.time.min),
    )


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


def list_users(client_slug: str | None = None, task_slug: str | None = None) -> list[dict]:
    """`client_slug`/`task_slug` filter to users holding a grant for that
    client and/or task (a real DB join, not a client-side scan) — used by the
    Users tab's client/task filters."""
    session = SessionLocal()
    try:
        q = session.query(User).order_by(User.username)
        if client_slug or task_slug:
            q = q.join(UserTaskAccess, UserTaskAccess.user_id == User.id)
            if client_slug:
                q = q.join(Client, UserTaskAccess.client_id == Client.id).filter(Client.slug == client_slug)
            if task_slug:
                q = q.join(Task, UserTaskAccess.task_id == Task.id).filter(Task.slug == task_slug)
            q = q.distinct()
        users = q.all()
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


def jobs_summary(since: "datetime.datetime | None" = None,
                  until: "datetime.datetime | None" = None,
                  user_id: int | None = None, client_slug: str | None = None,
                  task_slug: str | None = None, search: str | None = None) -> list[dict]:
    """One row per (user, client, task) combo that has produced a job in the
    given window — the grouped table the admin dashboard drills down from.
    `since`/`until` are an optional half-open range: [since, until).
    Omit both for all-time. `user_id`/`client_slug`/`task_slug`/`search` are
    all DB-level filters (the admin UI no longer scans the result in JS)."""
    session = SessionLocal()
    try:
        success_count = func.sum(case((JobHistory.status == "success", 1), else_=0))
        failed_count = func.sum(case((JobHistory.status == "failed", 1), else_=0))
        file_count = func.sum(func.coalesce(
            JobHistory.reference_count,
            case((JobHistory.status == "success", 1), else_=0),
        ))
        q = (
            session.query(
                User.id, User.name, User.username,
                Client.name, Client.slug,
                Task.name, Task.slug,
                file_count, success_count, failed_count,
                func.max(JobHistory.timestamp),
            )
            .join(User, JobHistory.user_id == User.id)
            .join(Client, JobHistory.client_id == Client.id)
            .join(Task, JobHistory.task_id == Task.id)
        )
        if since is not None:
            q = q.filter(JobHistory.timestamp >= since)
        if until is not None:
            q = q.filter(JobHistory.timestamp < until)
        if user_id is not None:
            q = q.filter(User.id == user_id)
        if client_slug:
            q = q.filter(Client.slug == client_slug)
        if task_slug:
            q = q.filter(Task.slug == task_slug)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(or_(
                User.name.ilike(like), User.username.ilike(like),
                Client.name.ilike(like), Task.name.ilike(like),
            ))
        rows = (
            q.group_by(User.id, Client.id, Task.id)
            .order_by(func.max(JobHistory.timestamp).desc())
            .all()
        )
        return [{
            "user_id": uid, "user_name": uname, "username": uusername,
            "client_name": cname, "client_slug": cslug,
            "task_name": tname, "task_slug": tslug,
            "count": count, "success_count": succ or 0, "failed_count": fail or 0,
            "last_run": _utc_iso(last) if last else None,
        } for uid, uname, uusername, cname, cslug, tname, tslug, count, succ, fail, last in rows]
    finally:
        session.close()


def list_jobs(user_id: int | None = None, client_slug: str | None = None,
              task_slug: str | None = None, status: str | None = None,
              limit: int = 200) -> list[dict]:
    """Individual job rows for the drill-down modal / detail views. Filters
    are all optional and AND together."""
    session = SessionLocal()
    try:
        q = (
            session.query(JobHistory, User, Client, Task)
            .join(User, JobHistory.user_id == User.id)
            .join(Client, JobHistory.client_id == Client.id)
            .join(Task, JobHistory.task_id == Task.id)
        )
        if user_id is not None:
            q = q.filter(JobHistory.user_id == user_id)
        if client_slug:
            q = q.filter(Client.slug == client_slug)
        if task_slug:
            q = q.filter(Task.slug == task_slug)
        if status:
            q = q.filter(JobHistory.status == status)
        rows = q.order_by(JobHistory.timestamp.desc()).limit(limit).all()
        return [{
            "id": j.id,
            "timestamp": _utc_iso(j.timestamp),
            "user_name": u.name,
            "username": u.username,
            "client_name": c.name,
            "client_slug": c.slug,
            "task_name": t.name,
            "task_slug": t.slug,
            "reference": j.reference,
            "reference_count": j.reference_count,
            "source_filename": j.source_filename,
            "row_count": j.row_count,
            "status": j.status,
            "download_url": (
                f"/app/{c.slug}/{t.slug}/download/{j.output_filename.split('/')[0]}"
                if j.output_filename else None
            ),
        } for j, u, c, t in rows]
    finally:
        session.close()


def dashboard_stats(days: int = 14, client_slug: str | None = None,
                     task_slug: str | None = None) -> dict:
    """Stat tiles + a files-per-day series for the dashboard chart, optionally
    scoped to one client and/or task.

    "Files" is distinct-reference count (a batch bundling 5 shipments counts
    as 5), not a raw job/run count — see build_reference() in helpers/jobs.py.
    Success/failure rate stays run-based (a run either produced output or it
    didn't), a separate concept from how many files that run represented.
    """
    session = SessionLocal()
    try:
        file_count_expr = func.coalesce(
            JobHistory.reference_count,
            case((JobHistory.status == "success", 1), else_=0),
        )

        def base_query(*entities):
            q = session.query(*entities).select_from(JobHistory)
            if client_slug:
                q = q.join(Client, JobHistory.client_id == Client.id).filter(Client.slug == client_slug)
            if task_slug:
                q = q.join(Task, JobHistory.task_id == Task.id).filter(Task.slug == task_slug)
            return q

        total_runs = base_query(func.count(JobHistory.id)).scalar() or 0
        success = base_query(func.count(JobHistory.id)).filter(JobHistory.status == "success").scalar() or 0
        failed = total_runs - success
        total_files = base_query(func.sum(file_count_expr)).scalar() or 0

        today = datetime.datetime.utcnow().date()
        since = datetime.datetime.combine(today - datetime.timedelta(days=days - 1), datetime.time.min)
        day_col = func.date(JobHistory.timestamp)
        rows = (
            base_query(day_col, func.sum(file_count_expr))
            .filter(JobHistory.timestamp >= since)
            .group_by(day_col)
            .all()
        )
        counts_by_day = {d: c or 0 for d, c in rows}
        series = []
        for i in range(days):
            d = today - datetime.timedelta(days=days - 1 - i)
            series.append({"date": d.isoformat(), "count": counts_by_day.get(d.isoformat(), 0)})

        files_today = counts_by_day.get(today.isoformat(), 0)
        files_this_week = sum(p["count"] for p in series[-7:])

        return {
            "total_files": total_files,
            "success_count": success,
            "failed_count": failed,
            "success_rate": round(success / total_runs * 100, 1) if total_runs else 0,
            "files_today": files_today,
            "files_this_week": files_this_week,
            "series": series,
        }
    finally:
        session.close()
