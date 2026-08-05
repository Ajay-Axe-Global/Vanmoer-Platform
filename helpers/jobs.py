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


def log_job(session, user_id: int, client_slug: str, task_slug: str, output_filename: str, status: str) -> JobHistory:
    client, task = get_client_and_task(session, client_slug, task_slug)
    job = JobHistory(
        user_id=user_id,
        client_id=client.id,
        task_id=task.id,
        output_filename=output_filename,
        status=status,
    )
    session.add(job)
    session.commit()
    return job
