"""
The single background worker that drains the ScreenshotJob queue
(database/models.py) and drives helpers/itos_automation.py.

Why a hand-rolled thread instead of Celery/Redis: the actual bottleneck
here isn't queue-broker throughput, it's that the automation takes over one
physical on-screen desktop (see itos_automation.py's docstring) — so there
can only ever be ONE of these running at a time on this machine no matter
what job-queue technology sits in front of it. A SQLite table plus one
daemon thread gives a durable, restart-proof queue at the right size for
that constraint, consistent with how this app already backgrounds work
(APScheduler's BackgroundScheduler for the hourly DB backup in main.py).

start_worker() must be called exactly once per running app process — see
its call site in main.py, guarded the same way _start_backup_scheduler()
already guards against Werkzeug's reloader parent process double-starting
things. Because only one thread ever calls _claim_next_job(), that function
is safe as a plain read-then-write despite not using any DB-level row lock:
there is never a second claimer to race against. (The double-REQUEST lock —
stopping two users from queueing the same row twice — is a separate,
genuinely concurrent-safe mechanism; see helpers/screenshot_queue.py.)
"""

import datetime
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from database.db import SessionLocal
from database.models import OrderTracking, ScreenshotJob
from helpers.itos_automation import ScreenshotAutomationError, capture_order_screenshots
from helpers.itos_number_extractor import extract_itos_number
from helpers.screenshot_queue import reference_dirname, screenshot_dir_for

logger = logging.getLogger("screenshot_worker")

POLL_SECONDS = int(os.getenv("SCREENSHOT_POLL_SECONDS", "3"))
# 2 automatic retries (3 attempts total) with backoff, as agreed — indexed
# by (attempts so far - 1).
RETRY_DELAYS_SECONDS = [30, 120]

# Runs the (Gemini) ITOS-number extraction off shot1 on a background thread,
# kicked off the instant shot1 is captured (see the on_shot1_captured hook
# below) so its network latency overlaps with the ~10s+ of on-screen
# automation still needed for shots 2/3 + the post-order navigate-back,
# instead of adding on top of it. One worker is enough: only one screenshot
# job is ever in flight at a time (see this module's docstring).
_itos_extract_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="itos-extract")
ITOS_EXTRACT_TIMEOUT_SECONDS = 20

_worker_started = False
_worker_lock = threading.Lock()


def start_worker():
    """Idempotent: a second call is a no-op, so accidental double-calls
    (e.g. from a future second registration site) can't spawn a second
    thread that would fight the first for the same desktop."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    _recover_stranded_jobs()
    thread = threading.Thread(target=_worker_loop, name="screenshot-worker", daemon=True)
    thread.start()
    logger.info("Screenshot worker started (poll interval: %ss)", POLL_SECONDS)


def _recover_stranded_jobs():
    """A previous run of this process may have crashed with a job stuck in
    "processing" — sweep those back to "queued" on startup so they're
    retried instead of hanging forever. This is the crash-recovery
    guarantee: a restart resumes work, it never loses it."""
    session = SessionLocal()
    try:
        stranded = session.query(ScreenshotJob).filter_by(status="processing").all()
        for job in stranded:
            job.status = "queued"
            job.next_retry_at = None
            row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
            if row and row.screenshot_status == "processing":
                row.screenshot_status = "queued"
        if stranded:
            session.commit()
            logger.warning("Recovered %d stranded screenshot job(s) after restart", len(stranded))
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
            logger.exception("Unexpected error in screenshot worker loop")
            time.sleep(POLL_SECONDS)
        finally:
            session.close()


def _claim_next_job(session) -> ScreenshotJob | None:
    now = datetime.datetime.utcnow()
    job = (
        session.query(ScreenshotJob)
        .filter(ScreenshotJob.status == "queued")
        .filter((ScreenshotJob.next_retry_at.is_(None)) | (ScreenshotJob.next_retry_at <= now))
        .order_by(ScreenshotJob.requested_at.asc())
        .first()
    )
    if job is None:
        return None
    job.status = "processing"
    job.started_at = now
    row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
    if row:
        row.screenshot_status = "processing"
    session.commit()
    return job


def _process_job(session, job: ScreenshotJob):
    row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
    client_slug = job.client.slug
    output_dir = screenshot_dir_for(client_slug, job.reference)

    # Fired the instant shot1 lands (before shots 2/3 are even taken) so the
    # Gemini call's network latency overlaps with the rest of the on-screen
    # automation instead of stacking on top of it after capture returns.
    itos_future = None

    def _on_shot1_captured(shot1_path):
        nonlocal itos_future
        itos_future = _itos_extract_executor.submit(extract_itos_number, shot1_path)

    try:
        # order_number only names the PNG files, so it must be filesystem-safe
        # (Vinmar references contain "/"); the raw reference is what gets
        # typed into iTOS's External ID box.
        capture_order_screenshots(
            reference_dirname(job.reference), output_dir,
            search_value=job.reference, on_shot1_captured=_on_shot1_captured,
            client_slug=client_slug,
        )
    except ScreenshotAutomationError as e:
        _handle_failure(session, job, row, str(e))
        return
    except Exception as e:
        logger.exception("Unexpected exception processing screenshot job %s", job.id)
        _handle_failure(session, job, row, f"Unexpected error: {e}")
        return

    job.status = "done"
    job.finished_at = datetime.datetime.utcnow()
    job.screenshot_dir = str(output_dir)
    if row:
        # This is the "flip the instant THIS row finishes" behavior —
        # committed here, per-job, never batched up for the whole request.
        row.status = "done"
        row.screenshot_status = "done"
        row.screenshot_error = None
        # Best-effort: never blocks or fails the job (see
        # helpers/itos_number_extractor.py). By now the background call
        # kicked off in _on_shot1_captured has usually already finished —
        # this just collects the result. Leaves itos_number untouched (not
        # reset to None) on a miss/timeout, so a previously-entered manual
        # value is never clobbered by a failed re-detection.
        itos_number = None
        if itos_future is not None:
            try:
                itos_number = itos_future.result(timeout=ITOS_EXTRACT_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning("ITOS number extraction did not complete for job %s: %s", job.id, e)
        if itos_number:
            row.itos_number = itos_number
    session.commit()
    logger.info("Screenshot job %s (reference %s) completed", job.id, job.reference)


def _handle_failure(session, job: ScreenshotJob, row: OrderTracking | None, error: str):
    job.attempts += 1
    job.last_error = error[:500]

    if job.attempts < job.max_attempts:
        delay = RETRY_DELAYS_SECONDS[min(job.attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        job.status = "queued"
        job.next_retry_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)
        if row:
            row.screenshot_status = "queued"
        logger.warning("Screenshot job %s attempt %d/%d failed, retrying in %ss: %s",
                        job.id, job.attempts, job.max_attempts, delay, error)
    else:
        job.status = "failed"
        job.finished_at = datetime.datetime.utcnow()
        if row:
            row.screenshot_status = "failed"
            row.screenshot_error = error[:500]
        logger.error("Screenshot job %s exhausted %d attempts, giving up: %s",
                     job.id, job.max_attempts, error)

    session.commit()
