"""
Drains the EmailJob queue (database/models.py) and drives
helpers/outlook_automation.py, with up to EMAIL_WORKER_MAX_CONCURRENCY
(default 3) worker threads running at once instead of a single one.

Structurally similar to helpers/screenshot_worker.py (durable DB queue,
crash-recovery sweep on startup), but independent of it: Playwright here
drives its OWN isolated Chrome profile per user over the DOM/CDP, not the
physical desktop the ITOS automation takes over, so these workers never
contend with the screenshot worker and can run at the same time.

Concurrency model: EACH USER gets their own persistent Chrome profile
directory (profile_dir_for_user in outlook_automation.py), so two DIFFERENT
users' sends genuinely don't conflict — safe to run at once. The SAME user's
jobs must never run at once, though: Chrome's own SingletonLock physically
prevents two processes from opening the same profile directory
simultaneously — a second launch just fails, it doesn't queue politely. So
_claim_next_job enforces "at most one in-flight job per user_id" via
_busy_user_ids, guarded by _claim_lock, while still letting DIFFERENT users'
jobs be claimed by different worker threads concurrently. The claim itself
(a fast DB query + status flip) is serialized by _claim_lock; the slow part
(actually driving the browser) happens after the lock is released, so the
threads still get real concurrency where it matters.

Browser reuse: once a worker thread claims a user's first job, it opens ONE
OutlookSession (helpers/outlook_automation.py) via open_outlook_session and
keeps reusing it — via _claim_next_job_for_user — for every OTHER queued job
that same user has, instead of relaunching Chrome (and re-checking login)
per job. The session is closed once that user's queue is drained for this
pass, or immediately if opening/logging in fails. This is what "one browser
per approval, opened fresh every time" used to cost: relaunch + login-check
latency multiplied by every single job, even back-to-back ones from the same
person's own batch approval.

No queued/backed-off retry here, unlike the screenshot worker: sending an
email isn't idempotent. _process_job does make one immediate in-place retry
on a transient automation failure (see its comment), since that's safe —
everything that can raise happens before any real send occurs — but beyond
that one retry, or on a LoginRequiredError from opening the session (never
worth retrying), failure is terminal ("failed") — see
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
from database.models import EmailJob, OrderTracking, User
from helpers.crypto_utils import decrypt_secret
from helpers.outlook_automation import (
    EmailAutomationError,
    LoginRequiredError,
    close_outlook_session,
    open_outlook_session,
    send_forwarded_screenshots,
)
from helpers.screenshot_queue import list_screenshot_files, screenshot_dir_for

logger = logging.getLogger("email_worker")

POLL_SECONDS = int(os.getenv("EMAIL_POLL_SECONDS", "3"))
# How many real Chrome windows this process will ever have open at once —
# each slot is a separate worker thread, but every job it processes still
# belongs to a DIFFERENT user than whatever every other slot is currently
# on (see _claim_next_job/_busy_user_ids), so raising this doesn't risk
# same-user profile-lock conflicts, only more concurrent CPU/memory load on
# whatever machine hosts this.
MAX_CONCURRENCY = int(os.getenv("EMAIL_WORKER_MAX_CONCURRENCY", "3"))

_worker_started = False
_worker_lock = threading.Lock()

# Guards _busy_user_ids — held only for the fast claim-and-commit step in
# _claim_next_job, never across an actual send (see module docstring).
_claim_lock = threading.Lock()
_busy_user_ids: set[int] = set()


def start_worker():
    """Idempotent: a second call is a no-op, so accidental double-calls
    can't spawn extra threads beyond MAX_CONCURRENCY."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    _recover_stranded_jobs()
    for i in range(MAX_CONCURRENCY):
        thread = threading.Thread(target=_worker_loop, name=f"email-worker-{i + 1}", daemon=True)
        thread.start()
    logger.info(
        "Email worker started: %d concurrent slot(s) (poll interval: %ss)",
        MAX_CONCURRENCY, POLL_SECONDS,
    )


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
        claimed_user_id = None
        outlook_session = None
        try:
            job = _claim_next_job(session)
            if job is None:
                session.close()
                time.sleep(POLL_SECONDS)
                continue
            claimed_user_id = job.requested_by

            # Keep pulling more of THIS SAME user's queued jobs onto the
            # one OutlookSession opened for the first one, instead of
            # releasing the user-lock and re-claiming through the general
            # pool each time — that's what actually reuses the browser
            # across a batch (see module docstring).
            while job is not None:
                outlook_session = _process_job(session, job, outlook_session)
                if outlook_session is None:
                    # Opening/logging in failed unrecoverably for this
                    # user this pass — stop trying more of their queued
                    # jobs now rather than repeating the identical failure
                    # back-to-back. They'll be picked up again on a future
                    # poll once whatever's wrong (e.g. needs_reauth) is
                    # fixed.
                    break
                job = _claim_next_job_for_user(session, claimed_user_id)
        except Exception:
            logger.exception("Unexpected error in email worker loop")
            time.sleep(POLL_SECONDS)
        finally:
            if outlook_session is not None:
                try:
                    close_outlook_session(outlook_session)
                except Exception:
                    logger.exception("Error closing Outlook session for user %s", claimed_user_id)
            session.close()
            if claimed_user_id is not None:
                with _claim_lock:
                    _busy_user_ids.discard(claimed_user_id)


def _claim_next_job(session) -> EmailJob | None:
    with _claim_lock:
        candidates = (
            session.query(EmailJob)
            .filter(EmailJob.status == "queued")
            .order_by(EmailJob.requested_at.asc())
            .all()
        )
        # Skip any job whose user already has a job in flight on another
        # worker slot right now — that user's Chrome profile is in use, and
        # a second launch against it would just fail (see module docstring),
        # not queue up. Earlier-queued jobs for OTHER users still jump
        # ahead of a later-queued job for a busy user, which is the correct
        # behavior — it's still first-come-first-served per user, just not
        # a single global FIFO once multiple users are in flight.
        job = next((j for j in candidates if j.requested_by not in _busy_user_ids), None)
        if job is None:
            return None

        _busy_user_ids.add(job.requested_by)
        job.status = "processing"
        job.started_at = datetime.datetime.utcnow()
        row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
        if row:
            row.email_status = "processing"
        session.commit()
        return job


def _claim_next_job_for_user(session, user_id: int) -> EmailJob | None:
    """Claims the next queued job for a user this worker thread ALREADY
    holds the reservation for (see _busy_user_ids) — used to keep pulling
    more of that same user's work onto the already-open OutlookSession
    instead of releasing and re-claiming through the general pool. No
    _claim_lock needed here: since this user_id is already reserved, no
    OTHER thread will ever match it in _claim_next_job's filter, so there's
    no contention to guard against."""
    job = (
        session.query(EmailJob)
        .filter(EmailJob.status == "queued", EmailJob.requested_by == user_id)
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


def _process_job(session, job: EmailJob, outlook_session):
    """Processes ONE job, reusing `outlook_session` if given (opened for an
    earlier job in this same user's batch — see _worker_loop), or opening a
    fresh one if None (first job of this user's batch this pass). Returns
    the OutlookSession to keep reusing for this user's next job, or None if
    the caller should stop trying more of this user's jobs this pass (an
    unrecoverable open/login failure — see open_outlook_session)."""
    row = session.query(OrderTracking).filter_by(id=job.order_tracking_id).first()
    client_slug = job.client.slug

    if not row:
        _handle_failure(session, job, row, "Order tracking row not found — nothing to attach.")
        return outlook_session

    # Each job's requester is who owns the Outlook account the send goes
    # through — see User.outlook_username/outlook_password/outlook_status
    # in database/models.py. The forward always uses the account of
    # whoever clicked Approve on this row, never a shared/hardcoded one.
    user = session.query(User).filter_by(id=job.requested_by).first()
    if not user or not user.outlook_username:
        _handle_failure(
            session, job, row,
            "No Outlook account connected for this user — set one in Admin > Users, "
            "then retry.",
        )
        return outlook_session

    password = decrypt_secret(user.outlook_password) if user.outlook_password else None

    screenshot_dir = screenshot_dir_for(client_slug, row.reference)
    filenames = list_screenshot_files(client_slug, row.reference)
    if not filenames:
        _handle_failure(session, job, row, "No captured screenshot files found for this row.")
        return outlook_session
    screenshot_paths = [screenshot_dir / name for name in filenames]

    if outlook_session is None:
        try:
            outlook_session = open_outlook_session(user.id, user.outlook_username, password, client_slug)
        except LoginRequiredError as e:
            user.outlook_status = "needs_reauth"
            _handle_failure(session, job, row, str(e))
            return None
        except EmailAutomationError as e:
            _handle_failure(session, job, row, str(e))
            return None
        except Exception as e:
            logger.exception("Unexpected exception opening Outlook session for user %s", user.id)
            _handle_failure(session, job, row, f"Unexpected error opening Outlook: {e}")
            return None

    # One immediate retry on transient automation failures (e.g. a UI
    # element not rendering in time — see outlook_automation.py's
    # _click_forward for a real example) before giving up. Not a queued/
    # backed-off retry like the screenshot worker's — this module still
    # deliberately has no attempts/next_retry_at (see its docstring):
    # sending isn't idempotent, so this only retries the ONE failure mode
    # that's actually safe to redo — anything that raised before Send was
    # ever clicked, which is every EmailAutomationError/unexpected
    # exception in practice (send_forwarded_screenshots only returns
    # cleanly, letting the job reach "sent" below, after a real send/draft
    # succeeds with nothing left to fail afterward except closing the
    # browser).
    last_error = None
    sent = False
    for attempt in (1, 2):
        try:
            send_forwarded_screenshots(
                outlook_session, job.reference, screenshot_paths,
                itos_number=row.itos_number, client_slug=client_slug,
            )
            sent = True
            break
        except EmailAutomationError as e:
            last_error = str(e)
            logger.warning("Email job %s attempt %d/2 failed: %s", job.id, attempt, last_error)
        except Exception as e:
            logger.exception("Unexpected exception processing email job %s (attempt %d/2)", job.id, attempt)
            last_error = f"Unexpected error: {e}"

    if not sent:
        _handle_failure(session, job, row, last_error or "Unknown error")
        return outlook_session

    user.outlook_status = "connected"
    job.status = "sent"
    job.finished_at = datetime.datetime.utcnow()
    if row:
        # Per-row flip, committed immediately — matches the screenshot
        # worker's "each row updates independently" behavior for batches.
        row.email_status = "sent"
        row.email_error = None
    session.commit()
    logger.info("Email job %s (reference %s) sent via %s", job.id, job.reference, user.outlook_username)
    return outlook_session


def _handle_failure(session, job: EmailJob, row: OrderTracking | None, error: str):
    # Called only once both the initial attempt and its one immediate
    # retry (see _process_job) are exhausted, or on LoginRequiredError
    # (no retry at all — see _process_job). No queued/backed-off retry
    # here, deliberately — see this module's docstring.
    job.status = "failed"
    job.finished_at = datetime.datetime.utcnow()
    job.last_error = error[:500]
    if row:
        row.email_status = "failed"
        row.email_error = error[:500]
    logger.error("Email job %s failed (after retry): %s", job.id, error)
    session.commit()
