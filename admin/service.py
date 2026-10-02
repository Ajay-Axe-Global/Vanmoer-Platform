"""
Admin business logic — user/client/task management + reporting. Kept separate
from routes/admin_routes.py (which stays thin HTTP glue) so it can also be
called from scripts/tests without going through Flask.
"""

import datetime

from sqlalchemy import case, func, or_

from sqlalchemy.orm import aliased

from database.backup import backup_now
from database.db import SessionLocal
from database.models import (
    Client,
    EmailJob,
    GeminiUsageLog,
    JobHistory,
    OrderTracking,
    ScreenshotJob,
    Task,
    User,
    UserTaskAccess,
)
from helpers.crypto_utils import encrypt_secret
from helpers.dates import period_range, resolve_tz, utc_iso
from helpers.jwt_utils import hash_password

# Re-exported so existing call sites (this module used to define these
# itself) and any external imports of `admin.service.period_range` etc.
# keep working unchanged. Real implementation lives in helpers/dates.py so
# other pages (e.g. the SABIC Outbound order-tracking panel) share the exact
# same "Today"/"This week"/"This month" boundary semantics.
_resolve_tz = resolve_tz
_utc_iso = utc_iso


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
            "outlook_username": u.outlook_username,
            # Password itself is never returned to the client, connected
            # or not — the admin form only ever writes a new one, never
            # reads the old one back.
            "outlook_connected": bool(u.outlook_password),
            "outlook_status": u.outlook_status,
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


def create_user(name: str, username: str, password: str, role: str, grants: list[dict] | None,
                 outlook_username: str | None = None, outlook_password: str | None = None) -> dict:
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

        outlook_username = (outlook_username or "").strip() or None

        user = User(
            name=name,
            username=username,
            password_hash=hash_password(password),
            role=role,
            outlook_username=outlook_username,
            outlook_password=encrypt_secret(outlook_password) if outlook_password else None,
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
                 grants: list[dict] | None,
                 outlook_username: str | None = None, outlook_password: str | None = None) -> dict:
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

        # Outlook username can be cleared (blank field = disconnect this
        # user's Outlook account); blank Outlook password on edit = keep
        # the existing encrypted one, same "blank means unchanged"
        # convention as the login password above.
        user.outlook_username = (outlook_username or "").strip() or None
        if outlook_password:
            user.outlook_password = encrypt_secret(outlook_password)
        elif not user.outlook_username:
            user.outlook_password = None
            user.outlook_status = None

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
                     task_slug: str | None = None, tz_name: str | None = None) -> dict:
    """Stat tiles + a files-per-day series for the dashboard chart, optionally
    scoped to one client and/or task.

    "Files" is distinct-reference count (a batch bundling 5 shipments counts
    as 5), not a raw job/run count — see build_reference() in helpers/jobs.py.
    Success/failure rate stays run-based (a run either produced output or it
    didn't), a separate concept from how many files that run represented.

    Day buckets ("Today", the per-day series) are calendar days in the
    viewer's timezone (`tz_name`), not the server's UTC day — grouping by
    `func.date(timestamp)` in SQL would bucket by the stored UTC day instead,
    which silently shifts jobs made near local midnight onto the wrong day.
    JobHistory.timestamp is naive UTC, so buckets are computed in Python
    after converting each row's timestamp into the viewer's tz.
    """
    session = SessionLocal()
    try:
        tz = _resolve_tz(tz_name)
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

        today = datetime.datetime.now(tz).date()
        since_date = today - datetime.timedelta(days=days - 1)
        since_utc = datetime.datetime.combine(since_date, datetime.time.min, tzinfo=tz) \
            .astimezone(datetime.timezone.utc).replace(tzinfo=None)
        rows = (
            base_query(JobHistory.timestamp, file_count_expr)
            .filter(JobHistory.timestamp >= since_utc)
            .all()
        )
        counts_by_day: dict[str, int] = {}
        for ts, count in rows:
            local_date = ts.replace(tzinfo=datetime.timezone.utc).astimezone(tz).date().isoformat()
            counts_by_day[local_date] = counts_by_day.get(local_date, 0) + (count or 0)

        series = []
        for i in range(days):
            d = today - datetime.timedelta(days=days - 1 - i)
            series.append({"date": d.isoformat(), "count": counts_by_day.get(d.isoformat(), 0)})

        files_today = counts_by_day.get(today.isoformat(), 0)
        # Calendar week (Sunday-Saturday), same boundary as the "This week"
        # period filter (period_range) — not a trailing-7-day window, so the
        # two stay in agreement instead of drifting apart mid-week.
        week_since, week_until = period_range("week", tz_name=tz_name)
        files_this_week = (
            base_query(func.sum(file_count_expr))
            .filter(JobHistory.timestamp >= week_since, JobHistory.timestamp < week_until)
            .scalar() or 0
        )

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


def files_by_client(since: datetime.datetime, until: datetime.datetime,
                     task_slug: str | None = None) -> list[dict]:
    """One row per client, zero-filled, total files in [since, until) —
    optionally scoped to one task. Every client from the catalog is included
    even at 0, so a client/color's bar position in the chart doesn't shift
    between periods that happen to have no data for it (same reasoning as the
    "full catalog" filter-option lists elsewhere in this module)."""
    session = SessionLocal()
    try:
        file_count_expr = func.coalesce(
            JobHistory.reference_count,
            case((JobHistory.status == "success", 1), else_=0),
        )
        q = (
            session.query(Client.id, func.sum(file_count_expr))
            .join(JobHistory, JobHistory.client_id == Client.id)
            .filter(JobHistory.timestamp >= since, JobHistory.timestamp < until)
        )
        if task_slug:
            q = q.join(Task, JobHistory.task_id == Task.id).filter(Task.slug == task_slug)
        counts_by_client = dict(q.group_by(Client.id).all())

        clients = session.query(Client).order_by(Client.name).all()
        return [{
            "client_name": c.name, "client_slug": c.slug,
            "count": counts_by_client.get(c.id, 0) or 0,
        } for c in clients]
    finally:
        session.close()


def productivity_by_user(since: datetime.datetime, until: datetime.datetime,
                          client_slug: str | None = None, task_slug: str | None = None,
                          user_id: int | None = None, search: str | None = None) -> list[dict]:
    """Files per user in [since, until) — same filter semantics as
    jobs_summary() (client/task/user/search all narrow the WHERE clause),
    but grouped by user only. Powers the "Productivity" pie chart. Users with
    no matching jobs are simply absent (a 0-file pie slice is just clutter)."""
    session = SessionLocal()
    try:
        file_count_expr = func.coalesce(
            JobHistory.reference_count,
            case((JobHistory.status == "success", 1), else_=0),
        )
        q = (
            session.query(User.id, User.name, User.username, func.sum(file_count_expr))
            .join(JobHistory, JobHistory.user_id == User.id)
            .join(Client, JobHistory.client_id == Client.id)
            .join(Task, JobHistory.task_id == Task.id)
            .filter(JobHistory.timestamp >= since, JobHistory.timestamp < until)
        )
        if client_slug:
            q = q.filter(Client.slug == client_slug)
        if task_slug:
            q = q.filter(Task.slug == task_slug)
        if user_id is not None:
            q = q.filter(User.id == user_id)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(or_(
                User.name.ilike(like), User.username.ilike(like),
                Client.name.ilike(like), Task.name.ilike(like),
            ))
        rows = (
            q.group_by(User.id)
            .order_by(func.sum(file_count_expr).desc())
            .all()
        )
        return [{"user_id": uid, "user_name": uname, "username": uusername, "count": count or 0}
                for uid, uname, uusername, count in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
# ORDER TRACKING ACTIVITY (screenshots + emails) — same filter/query
# conventions as jobs_summary()/productivity_by_user() above, applied to
# ScreenshotJob/EmailJob instead of JobHistory. Generic across any
# client/task, same as those tables themselves (see their docstrings in
# database/models.py) — not scoped to Sabic Outbound specifically, even
# though that's the only task using this pipeline today.


def screenshot_summary_by_user(since: datetime.datetime, until: datetime.datetime,
                                user_id: int | None = None, client_slug: str | None = None,
                                task_slug: str | None = None, search: str | None = None) -> list[dict]:
    """One row per (user, client, task) combo that has REQUESTED a
    screenshot job in [since, until) — "success" is ScreenshotJob.status ==
    "done" (matches the status the tracking panel itself shows), "pending"
    covers queued/processing (still in flight, not yet a final outcome)."""
    session = SessionLocal()
    try:
        done_count = func.sum(case((ScreenshotJob.status == "done", 1), else_=0))
        failed_count = func.sum(case((ScreenshotJob.status == "failed", 1), else_=0))
        pending_count = func.sum(case((ScreenshotJob.status.in_(["queued", "processing"]), 1), else_=0))
        q = (
            session.query(
                User.id, User.name, User.username,
                Client.name, Client.slug,
                Task.name, Task.slug,
                func.count(ScreenshotJob.id), done_count, failed_count, pending_count,
                func.max(ScreenshotJob.requested_at),
            )
            .join(User, ScreenshotJob.requested_by == User.id)
            .join(Client, ScreenshotJob.client_id == Client.id)
            .join(Task, ScreenshotJob.task_id == Task.id)
            .filter(ScreenshotJob.requested_at >= since, ScreenshotJob.requested_at < until)
        )
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
            .order_by(func.max(ScreenshotJob.requested_at).desc())
            .all()
        )
        return [{
            "user_id": uid, "user_name": uname, "username": uusername,
            "client_name": cname, "client_slug": cslug,
            "task_name": tname, "task_slug": tslug,
            "count": count, "done_count": done or 0, "failed_count": fail or 0,
            "pending_count": pending or 0,
            "last_requested": _utc_iso(last) if last else None,
        } for uid, uname, uusername, cname, cslug, tname, tslug, count, done, fail, pending, last in rows]
    finally:
        session.close()


def email_summary_by_user(since: datetime.datetime, until: datetime.datetime,
                           user_id: int | None = None, client_slug: str | None = None,
                           task_slug: str | None = None, search: str | None = None) -> list[dict]:
    """Same shape as screenshot_summary_by_user() but over EmailJob —
    "success" is EmailJob.status == "sent"."""
    session = SessionLocal()
    try:
        sent_count = func.sum(case((EmailJob.status == "sent", 1), else_=0))
        failed_count = func.sum(case((EmailJob.status == "failed", 1), else_=0))
        pending_count = func.sum(case((EmailJob.status.in_(["queued", "processing"]), 1), else_=0))
        q = (
            session.query(
                User.id, User.name, User.username,
                Client.name, Client.slug,
                Task.name, Task.slug,
                func.count(EmailJob.id), sent_count, failed_count, pending_count,
                func.max(EmailJob.requested_at),
            )
            .join(User, EmailJob.requested_by == User.id)
            .join(Client, EmailJob.client_id == Client.id)
            .join(Task, EmailJob.task_id == Task.id)
            .filter(EmailJob.requested_at >= since, EmailJob.requested_at < until)
        )
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
            .order_by(func.max(EmailJob.requested_at).desc())
            .all()
        )
        return [{
            "user_id": uid, "user_name": uname, "username": uusername,
            "client_name": cname, "client_slug": cslug,
            "task_name": tname, "task_slug": tslug,
            "count": count, "sent_count": sent or 0, "failed_count": fail or 0,
            "pending_count": pending or 0,
            "last_requested": _utc_iso(last) if last else None,
        } for uid, uname, uusername, cname, cslug, tname, tslug, count, sent, fail, pending, last in rows]
    finally:
        session.close()


def order_tracking_activity(since: datetime.datetime | None = None, until: datetime.datetime | None = None,
                             client_slug: str | None = None, task_slug: str | None = None,
                             user_id: int | None = None, search: str | None = None,
                             limit: int = 200) -> list[dict]:
    """One row per OrderTracking reference, joined to its MOST RECENT
    ScreenshotJob and EmailJob (if any) — answers "who requested the
    screenshot / who sent the email for reference X". A reference can have
    several ScreenshotJob/EmailJob rows over time (each "Request"/"Send"
    click after a prior batch finished starts a new one — see their own
    docstrings), so this always shows the latest one per reference, found
    via a MAX(id) subquery per order_tracking_id (not MAX(requested_at):
    id ordering is unambiguous even if two rows land in the same second).

    `user_id` filters to references where that user requested EITHER the
    screenshot or the email — this is a person-centric report ("what has
    this user touched"), not scoped to one side of the pipeline."""
    session = SessionLocal()
    try:
        latest_shot_ids = (
            session.query(
                ScreenshotJob.order_tracking_id.label("otid"),
                func.max(ScreenshotJob.id).label("max_id"),
            )
            .group_by(ScreenshotJob.order_tracking_id)
            .subquery()
        )
        latest_email_ids = (
            session.query(
                EmailJob.order_tracking_id.label("otid"),
                func.max(EmailJob.id).label("max_id"),
            )
            .group_by(EmailJob.order_tracking_id)
            .subquery()
        )
        ShotJob = aliased(ScreenshotJob)
        EmailJobRow = aliased(EmailJob)
        ShotUser = aliased(User)
        EmailUser = aliased(User)

        q = (
            session.query(OrderTracking, Client, Task, ShotJob, ShotUser, EmailJobRow, EmailUser)
            .join(Client, OrderTracking.client_id == Client.id)
            .join(Task, OrderTracking.task_id == Task.id)
            .outerjoin(latest_shot_ids, latest_shot_ids.c.otid == OrderTracking.id)
            .outerjoin(ShotJob, ShotJob.id == latest_shot_ids.c.max_id)
            .outerjoin(ShotUser, ShotUser.id == ShotJob.requested_by)
            .outerjoin(latest_email_ids, latest_email_ids.c.otid == OrderTracking.id)
            .outerjoin(EmailJobRow, EmailJobRow.id == latest_email_ids.c.max_id)
            .outerjoin(EmailUser, EmailUser.id == EmailJobRow.requested_by)
        )
        if since is not None:
            q = q.filter(OrderTracking.updated_at >= since)
        if until is not None:
            q = q.filter(OrderTracking.updated_at < until)
        if client_slug:
            q = q.filter(Client.slug == client_slug)
        if task_slug:
            q = q.filter(Task.slug == task_slug)
        if user_id is not None:
            q = q.filter(or_(ShotUser.id == user_id, EmailUser.id == user_id))
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(or_(
                OrderTracking.reference.ilike(like),
                OrderTracking.itos_number.ilike(like),
            ))
        rows = q.order_by(OrderTracking.updated_at.desc()).limit(limit).all()

        return [{
            "reference": row.reference,
            "itos_number": row.itos_number,
            "client_name": client.name, "client_slug": client.slug,
            "task_name": task.name, "task_slug": task.slug,
            "status": row.status,
            "screenshot_status": shot.status if shot else row.screenshot_status,
            "screenshot_requested_by": shot_user.name if shot_user else None,
            "screenshot_requested_by_username": shot_user.username if shot_user else None,
            "screenshot_error": (shot.last_error if shot else row.screenshot_error),
            "email_status": email.status if email else row.email_status,
            "email_requested_by": email_user.name if email_user else None,
            "email_requested_by_username": email_user.username if email_user else None,
            "email_error": (email.last_error if email else row.email_error),
            "updated_at": _utc_iso(row.updated_at) if row.updated_at else None,
        } for row, client, task, shot, shot_user, email, email_user in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
# BILLING & USAGE (Gemini Token Cost) — same filter/query conventions as the
# jobs_*/dashboard_* functions above, applied to GeminiUsageLog instead of
# JobHistory. See database/models.GeminiUsageLog and helpers/billing.py for
# how rows get here (one per Gemini API call, cost snapshotted at write
# time) and database/seed.py for the placeholder pricing/rate seeded on a
# fresh install.
# ═══════════════════════════════════════════════════════════════════════════

def _billing_base_query(session, *entities, since=None, until=None,
                         client_slug=None, task_slug=None, user_id=None, model_name=None):
    q = session.query(*entities).select_from(GeminiUsageLog)
    if client_slug:
        q = q.join(Client, GeminiUsageLog.client_id == Client.id).filter(Client.slug == client_slug)
    if task_slug:
        q = q.join(Task, GeminiUsageLog.task_id == Task.id).filter(Task.slug == task_slug)
    if user_id is not None:
        q = q.filter(GeminiUsageLog.user_id == user_id)
    if model_name:
        q = q.filter(GeminiUsageLog.model_name == model_name)
    if since is not None:
        q = q.filter(GeminiUsageLog.timestamp >= since)
    if until is not None:
        q = q.filter(GeminiUsageLog.timestamp < until)
    return q


def billing_summary(since: datetime.datetime, until: datetime.datetime,
                     client_slug: str | None = None, task_slug: str | None = None,
                     user_id: int | None = None, model_name: str | None = None) -> dict:
    """Headline stat-tile numbers for the Billing page's selected filters."""
    session = SessionLocal()
    
    try:
        row = _billing_base_query(
            session,
            func.coalesce(func.sum(GeminiUsageLog.total_cost_inr), 0),
            func.coalesce(func.sum(GeminiUsageLog.total_cost_usd), 0),
            func.coalesce(func.sum(GeminiUsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(GeminiUsageLog.completion_tokens), 0),
            func.coalesce(func.sum(GeminiUsageLog.total_tokens), 0),
            func.count(GeminiUsageLog.id),
            func.count(func.distinct(GeminiUsageLog.job_id)),
            since=since, until=until, client_slug=client_slug, task_slug=task_slug,
            user_id=user_id, model_name=model_name,
        ).one()
        cost_inr, cost_usd, prompt_tok, completion_tok, total_tok, calls, jobs = row
        cost_inr, cost_usd = float(cost_inr), float(cost_usd)
        return {
            "total_cost_inr": round(cost_inr, 2),
            "total_cost_usd": round(cost_usd, 4),
            "total_prompt_tokens": int(prompt_tok),
            "total_completion_tokens": int(completion_tok),
            "total_tokens": int(total_tok),
            "total_calls": int(calls),
            "total_jobs": int(jobs),
            "avg_cost_per_job_inr": round(cost_inr / jobs, 2) if jobs else 0,
        }
    finally:
        session.close()


def usage_by_day(since: datetime.datetime, until: datetime.datetime,
                  client_slug: str | None = None, task_slug: str | None = None,
                  user_id: int | None = None, model_name: str | None = None,
                  tz_name: str | None = None) -> list[dict]:
    """Daily cost/token/call series for the line & bar charts — day buckets
    are calendar days in the viewer's timezone, same reasoning and same
    UTC-timestamp-bucketed-in-Python approach as dashboard_stats()'s
    files-per-day series (grouping by func.date() in SQL would bucket by
    the stored UTC day instead, shifting late-night jobs onto the wrong
    local day)."""
    session = SessionLocal()
    try:
        tz = _resolve_tz(tz_name)
        rows = _billing_base_query(
            session, GeminiUsageLog.timestamp, GeminiUsageLog.total_cost_inr,
            GeminiUsageLog.total_cost_usd, GeminiUsageLog.total_tokens,
            since=since, until=until, client_slug=client_slug, task_slug=task_slug,
            user_id=user_id, model_name=model_name,
        ).all()

        by_day: dict[str, dict] = {}
        for ts, cost_inr, cost_usd, tokens in rows:
            local_date = ts.replace(tzinfo=datetime.timezone.utc).astimezone(tz).date().isoformat()
            bucket = by_day.setdefault(local_date, {"cost_inr": 0.0, "cost_usd": 0.0, "tokens": 0, "calls": 0})
            bucket["cost_inr"] += float(cost_inr)
            bucket["cost_usd"] += float(cost_usd)
            bucket["tokens"] += int(tokens)
            bucket["calls"] += 1

        # Zero-filled across every day in [since, until) — a day with no
        # calls at all is still a real point on the line chart, not a gap.
        series = []
        d = since.date()
        end = until.date()
        while d < end:
            iso = d.isoformat()
            bucket = by_day.get(iso, {"cost_inr": 0.0, "cost_usd": 0.0, "tokens": 0, "calls": 0})
            series.append({
                "date": iso,
                "cost_inr": round(bucket["cost_inr"], 2),
                "cost_usd": round(bucket["cost_usd"], 4),
                "tokens": bucket["tokens"],
                "calls": bucket["calls"],
            })
            d += datetime.timedelta(days=1)
        return series
    finally:
        session.close()


def high_demand_days(since: datetime.datetime, until: datetime.datetime,
                      client_slug: str | None = None, task_slug: str | None = None,
                      user_id: int | None = None, model_name: str | None = None,
                      tz_name: str | None = None, top_n: int = 5) -> list[dict]:
    """Top-N days by cost in the selected window — the "high demand day"
    callout (Google Cloud Console-style peak-usage highlight). Days with
    zero calls are excluded (a 0-cost day is never "high demand")."""
    series = usage_by_day(since, until, client_slug, task_slug, user_id, model_name, tz_name)
    non_zero = [d for d in series if d["calls"] > 0]
    return sorted(non_zero, key=lambda d: d["cost_inr"], reverse=True)[:top_n]


def usage_by_model(since: datetime.datetime, until: datetime.datetime,
                    client_slug: str | None = None, task_slug: str | None = None,
                    user_id: int | None = None) -> list[dict]:
    """Cost/token share per Gemini model — pie/bar chart. Only models that
    actually have usage in the window appear (no catalog to zero-fill
    against, unlike clients/tasks)."""
    session = SessionLocal()
    try:
        rows = (
            _billing_base_query(
                session, GeminiUsageLog.model_name,
                func.sum(GeminiUsageLog.total_cost_inr), func.sum(GeminiUsageLog.total_cost_usd),
                func.sum(GeminiUsageLog.total_tokens), func.count(GeminiUsageLog.id),
                since=since, until=until, client_slug=client_slug, task_slug=task_slug, user_id=user_id,
            )
            .group_by(GeminiUsageLog.model_name)
            .order_by(func.sum(GeminiUsageLog.total_cost_inr).desc())
            .all()
        )
        return [{
            "model_name": model, "cost_inr": round(float(cost_inr), 2),
            "cost_usd": round(float(cost_usd), 4), "tokens": int(tokens), "calls": int(calls),
        } for model, cost_inr, cost_usd, tokens, calls in rows]
    finally:
        session.close()


def usage_by_client(since: datetime.datetime, until: datetime.datetime,
                     task_slug: str | None = None, user_id: int | None = None,
                     model_name: str | None = None) -> list[dict]:
    """Cost per client, zero-filled across the full client catalog — same
    "every client appears even at 0" reasoning as files_by_client(), so a
    client/color's position in the chart doesn't shift between periods."""
    session = SessionLocal()
    try:
        q = (
            session.query(Client.id, func.sum(GeminiUsageLog.total_cost_inr), func.count(GeminiUsageLog.id))
            .join(GeminiUsageLog, GeminiUsageLog.client_id == Client.id)
            .filter(GeminiUsageLog.timestamp >= since, GeminiUsageLog.timestamp < until)
        )
        if task_slug:
            q = q.join(Task, GeminiUsageLog.task_id == Task.id).filter(Task.slug == task_slug)
        if user_id is not None:
            q = q.filter(GeminiUsageLog.user_id == user_id)
        if model_name:
            q = q.filter(GeminiUsageLog.model_name == model_name)
        by_client = {cid: (cost, calls) for cid, cost, calls in q.group_by(Client.id).all()}

        clients = session.query(Client).order_by(Client.name).all()
        return [{
            "client_name": c.name, "client_slug": c.slug,
            "cost_inr": round(float(by_client.get(c.id, (0, 0))[0] or 0), 2),
            "calls": int(by_client.get(c.id, (0, 0))[1] or 0),
        } for c in clients]
    finally:
        session.close()


def usage_by_task(since: datetime.datetime, until: datetime.datetime,
                   client_slug: str | None = None, user_id: int | None = None,
                   model_name: str | None = None) -> list[dict]:
    """Cost per task, zero-filled across the full task catalog — same
    reasoning as usage_by_client()."""
    session = SessionLocal()
    try:
        q = (
            session.query(Task.id, func.sum(GeminiUsageLog.total_cost_inr), func.count(GeminiUsageLog.id))
            .join(GeminiUsageLog, GeminiUsageLog.task_id == Task.id)
            .filter(GeminiUsageLog.timestamp >= since, GeminiUsageLog.timestamp < until)
        )
        if client_slug:
            q = q.join(Client, GeminiUsageLog.client_id == Client.id).filter(Client.slug == client_slug)
        if user_id is not None:
            q = q.filter(GeminiUsageLog.user_id == user_id)
        if model_name:
            q = q.filter(GeminiUsageLog.model_name == model_name)
        by_task = {tid: (cost, calls) for tid, cost, calls in q.group_by(Task.id).all()}

        tasks = session.query(Task).order_by(Task.name).all()
        return [{
            "task_name": t.name, "task_slug": t.slug,
            "cost_inr": round(float(by_task.get(t.id, (0, 0))[0] or 0), 2),
            "calls": int(by_task.get(t.id, (0, 0))[1] or 0),
        } for t in tasks]
    finally:
        session.close()


def usage_by_user(since: datetime.datetime, until: datetime.datetime,
                   client_slug: str | None = None, task_slug: str | None = None,
                   model_name: str | None = None) -> list[dict]:
    """Cost per user in [since, until) — powers the Billing page's
    per-user pie/bar chart. Users with zero usage in the window are simply
    absent, same as productivity_by_user()."""
    session = SessionLocal()
    try:
        q = (
            session.query(User.id, User.name, User.username,
                          func.sum(GeminiUsageLog.total_cost_inr), func.count(GeminiUsageLog.id))
            .join(GeminiUsageLog, GeminiUsageLog.user_id == User.id)
            .filter(GeminiUsageLog.timestamp >= since, GeminiUsageLog.timestamp < until)
        )
        if client_slug:
            q = q.join(Client, GeminiUsageLog.client_id == Client.id).filter(Client.slug == client_slug)
        if task_slug:
            q = q.join(Task, GeminiUsageLog.task_id == Task.id).filter(Task.slug == task_slug)
        if model_name:
            q = q.filter(GeminiUsageLog.model_name == model_name)
        rows = (
            q.group_by(User.id)
            .order_by(func.sum(GeminiUsageLog.total_cost_inr).desc())
            .all()
        )

        return [{
            "user_id": uid, "user_name": uname, "username": uusername,
            "cost_inr": round(float(cost or 0), 2), "calls": int(calls or 0),
        } for uid, uname, uusername, cost, calls in rows]
    finally:
        session.close()
