"""
Enqueue/retry service layer for the ITOS screenshot automation. Shared by
every client task's routes (see clients/sabic/outbound/task.py for the first
caller) so wiring this into a second client later is a couple of route
lines, not copy-pasted queue logic.

The actual automation lives in helpers/itos_automation.py; the background
worker that drains this queue lives in helpers/screenshot_worker.py. This
module only ever inserts/updates rows — it never runs the automation itself,
so calling it from a Flask request handler returns in milliseconds.
"""

import datetime
import uuid
from pathlib import Path

from sqlalchemy import or_

from database.models import Client, OrderTracking, ScreenshotJob, Task

SCREENSHOTS_DIR = Path(__file__).parent.parent / "screenshots"


def screenshot_dir_for(client_slug: str, itos_number: str) -> Path:
    d = SCREENSHOTS_DIR / client_slug / itos_number
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_screenshot_files(client_slug: str, itos_number: str) -> list[str]:
    """Filenames only (not full paths) — the route layer resolves these back
    against screenshot_dir_for() itself so a caller can't smuggle a path via
    itos_number. Sorted so the 3-shot sequence (_01_order, _02_addresses,
    _03_transport) always displays in the order they were captured."""
    d = SCREENSHOTS_DIR / client_slug / itos_number
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".png")


def _get_client_and_task(session, client_slug: str, task_slug: str) -> tuple[Client, Task]:
    client = session.query(Client).filter_by(slug=client_slug).first()
    task = session.query(Task).filter_by(slug=task_slug).first()
    if not client or not task:
        raise RuntimeError(f"Client/Task not seeded in DB: {client_slug}/{task_slug}")
    return client, task


def _claim_rows_for_enqueue(session, client, task, order_tracking_ids: list[int],
                             allow_from_status) -> dict[int, str]:
    """Atomically claims each row for a fresh screenshot run: flips
    screenshot_status to "queued" only if it's currently one of
    `allow_from_status` (None, or "failed" for a retry) — this single
    conditional UPDATE per row IS the double-request lock: two
    near-simultaneous requests for the same row can't both succeed, because
    SQLite serializes writers and the second UPDATE's WHERE clause simply
    won't match anymore once the first has already flipped the row.

    Returns {order_tracking_id: itos_number} for every row that WAS
    successfully claimed (i.e. should get a new ScreenshotJob row).
    """
    status_conditions = [OrderTracking.screenshot_status.in_(
        [s for s in allow_from_status if s is not None]
    )]
    if None in allow_from_status:
        status_conditions.append(OrderTracking.screenshot_status.is_(None))

    claimed = {}
    for otid in order_tracking_ids:
        result = session.execute(
            OrderTracking.__table__.update()
            .where(
                OrderTracking.id == otid,
                OrderTracking.client_id == client.id,
                OrderTracking.task_id == task.id,
                OrderTracking.itos_number.isnot(None),
                OrderTracking.itos_number != "",
                or_(*status_conditions),
            )
            .values(screenshot_status="queued", screenshot_error=None)
        )
        if result.rowcount == 1:
            row = session.query(OrderTracking).filter_by(id=otid).first()
            claimed[otid] = row.itos_number
    session.commit()
    return claimed


def request_screenshots(session, client_slug: str, task_slug: str,
                         order_tracking_ids: list[int], user_id: int) -> dict:
    """Queues a screenshot run for each given OrderTracking row. Rows that
    have no saved ITOS number yet, or are already queued/processing/done,
    are skipped (with a reason) rather than erroring the whole call — a
    batch Request should make progress on whatever it validly can."""
    client, task = _get_client_and_task(session, client_slug, task_slug)

    rows_by_id = {
        r.id: r for r in
        session.query(OrderTracking).filter_by(client_id=client.id, task_id=task.id)
        .filter(OrderTracking.id.in_(order_tracking_ids)).all()
    }

    skipped = []
    eligible_ids = []
    for otid in order_tracking_ids:
        row = rows_by_id.get(otid)
        if not row:
            skipped.append({"id": otid, "reason": "Not found."})
        elif not row.itos_number:
            skipped.append({"id": otid, "reason": "No ITOS number saved yet."})
        elif row.screenshot_status in ("queued", "processing"):
            skipped.append({"id": otid, "reason": "Already in progress."})
        elif row.screenshot_status == "done":
            skipped.append({"id": otid, "reason": "Already done."})
        else:
            eligible_ids.append(otid)

    claimed = _claim_rows_for_enqueue(session, client, task, eligible_ids, allow_from_status=[None, "failed"])
    for otid in eligible_ids:
        if otid not in claimed:
            skipped.append({"id": otid, "reason": "Already in progress."})

    batch_id = str(uuid.uuid4())
    queued = []
    for otid, itos_number in claimed.items():
        session.add(ScreenshotJob(
            order_tracking_id=otid, client_id=client.id, task_id=task.id,
            itos_number=itos_number, batch_id=batch_id, status="queued",
            requested_by=user_id, requested_at=datetime.datetime.utcnow(),
        ))
        queued.append(otid)
    session.commit()

    return {"queued": queued, "skipped": skipped, "batch_id": batch_id if queued else None}


def retry_failed(session, client_slug: str, task_slug: str,
                  order_tracking_ids: list[int], user_id: int) -> dict:
    """Same shape as request_screenshots(), but only rows currently
    "failed" are eligible — a fresh ScreenshotJob row is created with
    attempts reset to 0, same as an initial request."""
    client, task = _get_client_and_task(session, client_slug, task_slug)

    rows_by_id = {
        r.id: r for r in
        session.query(OrderTracking).filter_by(client_id=client.id, task_id=task.id)
        .filter(OrderTracking.id.in_(order_tracking_ids)).all()
    }

    skipped = []
    eligible_ids = []
    for otid in order_tracking_ids:
        row = rows_by_id.get(otid)
        if not row:
            skipped.append({"id": otid, "reason": "Not found."})
        elif row.screenshot_status != "failed":
            skipped.append({"id": otid, "reason": "Not in a failed state."})
        else:
            eligible_ids.append(otid)

    claimed = _claim_rows_for_enqueue(session, client, task, eligible_ids, allow_from_status=["failed"])
    for otid in eligible_ids:
        if otid not in claimed:
            skipped.append({"id": otid, "reason": "Already in progress."})

    batch_id = str(uuid.uuid4())
    queued = []
    for otid, itos_number in claimed.items():
        session.add(ScreenshotJob(
            order_tracking_id=otid, client_id=client.id, task_id=task.id,
            itos_number=itos_number, batch_id=batch_id, status="queued",
            requested_by=user_id, requested_at=datetime.datetime.utcnow(),
        ))
        queued.append(otid)
    session.commit()

    return {"queued": queued, "skipped": skipped, "batch_id": batch_id if queued else None}
