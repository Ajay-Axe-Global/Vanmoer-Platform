"""
The second single background worker — drains the EmailJob queue
(database/models.py) and drives helpers/outlook_automation.py.

Structurally identical to helpers/screenshot_worker.py (durable DB queue +
one daemon thread, crash-recovery sweep on startup), but independent of it:
Playwright here drives its OWN isolated Chrome profile over the DOM/CDP, not
the physical desktop the ITOS automation takes over, so the two workers
never contend with each other and can run at the same time. This worker's
own single-concurrency constraint is different — only one process may hold
the persistent Outlook browser profile directory open at once (see
outlook_automation.py's docstring) — which is what serializing all email
jobs through this one thread guarantees.

No retry/backoff here, unlike the screenshot worker: sending an email isn't
idempotent, so any failure is terminal ("failed") — see
helpers/email_queue.py's retry_failed_emails() for how a human-initiated
Retry re-queues a fresh attempt instead.

start_worker() must be called exactly once per running app process — see
its call site in main.py, guarded the same way the screenshot worker's is.
"""

import datetime
import logging
import os
import threading
import time

from database.db import SessionLocal
from database.models import EmailJob, OrderTracking
from helpers.outlook_automation import EmailAutomationError, LoginRequiredError, send_forwarded_screenshots
from helpers.screenshot_queue import list_screenshot_files, screenshot_dir_for

logger = logging.getLogger("email_worker")

POLL_SECONDS = int(os.getenv("EMAIL_POLL_SECONDS", "3"))

_worker_started = False
_worker_lock = threading.Lock()


def start_worker():
    """Idempotent: a second call is a no-op, so accidental double-calls
    can't spawn a second thread that would fight the first for the one
    persistent Outlook browser profile."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    _recover_stranded_jobs()
    thread = threading.Thread(target=_worker_loop, name="email-worker", daemon=True)
    thread.start()
    logger.info("Email worker started (poll interval: %ss)", POLL_SECONDS)


def _recover_stranded_jobs():
    """A previous run of this process may have crashed with a job stuck in
    "processing" — sweep those back to "queued" on startup, same
    crash-recovery guarantee as the screenshot worker's."""
    session = SessionLocal()
    try:
        stranded = session.query(EmailJob).filter_by(status="processing").all()
        for job in stranded:
            job.status = "queued"
            row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
            if row and row.email_status == "processing":
                row.email_status = "queued"
        if stranded:
            session.commit()
            logger.warning("Recovered %d stranded email job(s) after restart", len(stranded))
    finally:
        session.close()


def _worker_loop():
    while True:
        session = SessionLocal()
        try:
            job = _claim_next_job(session)
            if job is None:
                session.close()
                time.sleep(POLL_SECONDS)
                continue
            _process_job(session, job)
        except Exception:
            logger.exception("Unexpected error in email worker loop")
            time.sleep(POLL_SECONDS)
        finally:
            session.close()


def _claim_next_job(session) -> EmailJob | None:
    job = (
        session.query(EmailJob)
        .filter(EmailJob.status == "queued")
        .order_by(EmailJob.requested_at.asc())
        .first()
    )
    if job is None:
        return None
    job.status = "processing"
    job.started_at = datetime.datetime.utcnow()
    row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
    if row:
        row.email_status = "processing"
    session.commit()
    return job


def _process_job(session, job: EmailJob):
    row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
    client_slug = job.client.slug

    if not row or not row.itos_number:
        _handle_failure(session, job, row, "No ITOS number on this row — nothing to attach.")
        return

    screenshot_dir = screenshot_dir_for(client_slug, row.itos_number)
    filenames = list_screenshot_files(client_slug, row.itos_number)
    if not filenames:
        _handle_failure(session, job, row, "No captured screenshot files found for this row.")
        return
    screenshot_paths = [screenshot_dir / name for name in filenames]

    try:
        send_forwarded_screenshots(job.reference, screenshot_paths)
    except LoginRequiredError as e:
        _handle_failure(session, job, row, str(e))
        return
    except EmailAutomationError as e:
        _handle_failure(session, job, row, str(e))
        return
    except Exception as e:
        logger.exception("Unexpected exception processing email job %s", job.id)
        _handle_failure(session, job, row, f"Unexpected error: {e}")
        return

    job.status = "sent"
    job.finished_at = datetime.datetime.utcnow()
    if row:
        # Per-row flip, committed immediately — matches the screenshot
        # worker's "each row updates independently" behavior for batches.
        row.email_status = "sent"
        row.email_error = None
    session.commit()
    logger.info("Email job %s (reference %s) sent", job.id, job.reference)


def _handle_failure(session, job: EmailJob, row: OrderTracking | None, error: str):
    # No retry scheduling here, deliberately — see this module's docstring.
    job.status = "failed"
    job.finished_at = datetime.datetime.utcnow()
    job.last_error = error[:500]
    if row:
        row.email_status = "failed"
        row.email_error = error[:500]
    logger.error("Email job %s failed (no auto-retry): %s", job.id, error)
    session.commit()
