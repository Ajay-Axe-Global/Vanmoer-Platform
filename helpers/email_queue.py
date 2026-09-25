"""
Enqueue/retry service layer for the Outlook forward-with-screenshots
automation. Mirrors helpers/screenshot_queue.py's request/retry shape and
double-request-lock pattern exactly, applied to `email_status` instead of
`screenshot_status` — with one extra eligibility rule: a row must already
be `status == "done"` (screenshots actually captured), since there's
nothing to attach otherwise.

The actual automation lives in helpers/outlook_automation.py; the
background worker that drains this queue lives in helpers/email_worker.py.
This module only ever inserts/updates rows — it never sends anything
itself, so calling it from a Flask request handler returns in milliseconds.
"""

import datetime
import uuid

from sqlalchemy import or_

from database.models import Client, EmailJob, OrderTracking, Task


def _get_client_and_task(session, client_slug: str, task_slug: str) -> tuple[Client, Task]:
    client = session.query(Client).filter_by(slug=client_slug).first()
    task = session.query(Task).filter_by(slug=task_slug).first()
    if not client or not task:
        raise RuntimeError(f"Client/Task not seeded in DB: {client_slug}/{task_slug}")
    return client, task


def _claim_rows_for_enqueue(session, client, task, order_tracking_ids: list[int],
                             allow_from_status) -> dict[int, str]:
    """Atomically claims each row for a fresh email send: flips
    email_status to "queued" only if it's currently one of
    `allow_from_status` (None, or "failed" for a retry) AND status is
    already "done" — this single conditional UPDATE per row IS the
    double-request lock, same reasoning as screenshot_queue.py's version.

    Returns {order_tracking_id: reference} for every row that WAS
    successfully claimed (i.e. should get a new EmailJob row).
    """
    status_conditions = [OrderTracking.email_status.in_(
        [s for s in allow_from_status if s is not None]
    )]
    if None in allow_from_status:
        status_conditions.append(OrderTracking.email_status.is_(None))

    claimed = {}
    for otid in order_tracking_ids:
        result = session.execute(
            OrderTracking.__table__.update()
            .where(
                OrderTracking.id == otid,
                OrderTracking.client_id == client.id,
                OrderTracking.task_id == task.id,
                OrderTracking.status == "done",
                or_(*status_conditions),
            )
            .values(email_status="queued", email_error=None)
        )
        if result.rowcount == 1:
            row = session.query(OrderTracking).filter_by(id=otid).first()
            claimed[otid] = row.reference
    session.commit()
    return claimed


def request_emails(session, client_slug: str, task_slug: str,
                    order_tracking_ids: list[int], user_id: int) -> dict:
    """Queues an Outlook forward for each given OrderTracking row. Rows
    whose screenshots aren't captured yet, or that are already
    queued/processing/sent, are skipped (with a reason) rather than
    erroring the whole call — a batch Approve should make progress on
    whatever it validly can."""
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
        elif row.status != "done":
            skipped.append({"id": otid, "reason": "Screenshots not captured yet."})
        elif row.email_status in ("queued", "processing"):
            skipped.append({"id": otid, "reason": "Already in progress."})
        elif row.email_status == "sent":
            skipped.append({"id": otid, "reason": "Already sent."})
        else:
            eligible_ids.append(otid)

    claimed = _claim_rows_for_enqueue(session, client, task, eligible_ids, allow_from_status=[None, "failed"])
    for otid in eligible_ids:
        if otid not in claimed:
            skipped.append({"id": otid, "reason": "Already in progress."})

    batch_id = str(uuid.uuid4())
    queued = []
    for otid, reference in claimed.items():
        session.add(EmailJob(
            order_tracking_id=otid, client_id=client.id, task_id=task.id,
            reference=reference, batch_id=batch_id, status="queued",
            requested_by=user_id, requested_at=datetime.datetime.utcnow(),
        ))
        queued.append(otid)
    session.commit()

    return {"queued": queued, "skipped": skipped, "batch_id": batch_id if queued else None}


def retry_failed_emails(session, client_slug: str, task_slug: str,
                         order_tracking_ids: list[int], user_id: int) -> dict:
    """Same shape as request_emails(), but only rows currently
    email_status == "failed" are eligible — a fresh EmailJob row is
    created, same as an initial request (there is no attempts counter to
    reset: emailing never auto-retries, so every attempt, first or manual
    retry, is a fresh row)."""
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
        elif row.email_status != "failed":
            skipped.append({"id": otid, "reason": "Not in a failed state."})
        else:
            eligible_ids.append(otid)

    claimed = _claim_rows_for_enqueue(session, client, task, eligible_ids, allow_from_status=["failed"])
    for otid in eligible_ids:
        if otid not in claimed:
            skipped.append({"id": otid, "reason": "Already in progress."})

    batch_id = str(uuid.uuid4())
    queued = []
    for otid, reference in claimed.items():
        session.add(EmailJob(
            order_tracking_id=otid, client_id=client.id, task_id=task.id,
            reference=reference, batch_id=batch_id, status="queued",
            requested_by=user_id, requested_at=datetime.datetime.utcnow(),
        ))
        queued.append(otid)
    session.commit()

    return {"queued": queued, "skipped": skipped, "batch_id": batch_id if queued else None}
