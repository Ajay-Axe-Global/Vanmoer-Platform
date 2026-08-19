"""
Shared job-folder + JobHistory logging helpers used by every client task's
/process and /download routes. Keeps that plumbing out of each task.py.
"""

import uuid
from pathlib import Path

from database.models import Client, JobHistory, Task

UPLOADS_DIR = Path(__file__).parent.parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)


def new_job_dir() -> tuple[str, Path]:
    job_id = uuid.uuid4().hex
    job_dir = UPLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir


def job_output_path(job_id: str) -> Path:
    return UPLOADS_DIR / job_id / "output.xlsx"


def get_client_and_task(session, client_slug: str, task_slug: str) -> tuple[Client, Task]:
    client = session.query(Client).filter_by(slug=client_slug).first()
    task = session.query(Task).filter_by(slug=task_slug).first()
    if not client or not task:
        raise RuntimeError(f"Client/Task not seeded in DB: {client_slug}/{task_slug}")
    return client, task


def build_reference(values, sep: str = ", ") -> tuple[str, int]:
    """The standard way every task turns its row-level business-reference
    values (e.g. one SABIC shipment ID per row, one Carpenter TCS ref per
    row) into what JobHistory.reference / reference_count store: de-duped,
    order-preserving, comma-joined for display, plus the distinct count that
    the admin dashboard's "Files" totals are summed by — so a batch bundling
    5 distinct shipments counts as 5 files, not 1.
    """
    distinct = list(dict.fromkeys(v.strip() for v in values if v and str(v).strip()))
    return sep.join(distinct), len(distinct)


def log_job(session, user_id: int, client_slug: str, task_slug: str, output_filename: str, status: str,
            reference: str | None = None, source_filename: str | None = None,
            row_count: int | None = None, reference_count: int | None = None) -> JobHistory:
    client, task = get_client_and_task(session, client_slug, task_slug)
    # A successful run always produced at least one real file even when
    # extraction couldn't find a distinct reference for it — never let the
    # "Files" tally silently undercount to zero for a run that did work.
    if status == "success" and not reference_count:
        reference_count = 1
    job = JobHistory(
        user_id=user_id,
        client_id=client.id,
        task_id=task.id,
        output_filename=output_filename,
        status=status,
        reference=reference,
        source_filename=source_filename,
        row_count=row_count,
        reference_count=reference_count,
    )
    session.add(job)
    session.commit()
    return job
