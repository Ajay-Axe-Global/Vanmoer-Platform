import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Client(Base):
    """Admin-managed lookup table — NOT a hardcoded enum. Seeded with Carpenter/Sabic,
    admin can add more via the admin panel. Adding a row here is RBAC metadata only;
    the actual extraction code still has to be built under clients/<slug>/."""
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    slug = Column(String(120), unique=True, nullable=False)

    grants = relationship("UserTaskAccess", back_populates="client")
    jobs = relationship("JobHistory", back_populates="client")


class Task(Base):
    """Admin-managed lookup table, same pattern as Client. Seeded with Inbound/Outbound."""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    slug = Column(String(120), unique=True, nullable=False)

    grants = relationship("UserTaskAccess", back_populates="task")
    jobs = relationship("JobHistory", back_populates="task")


class User(Base):
    """A user's client/task access lives in UserTaskAccess (many-to-many), not on
    this row — one login can be granted several client+task combinations (e.g. a
    person who runs both Carpenter Inbound and Outbound). Admin accounts have no
    grants at all."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # "admin" | "user"
    # "Delete" in the admin UI sets this False rather than removing the row —
    # a hard delete would either orphan JobHistory rows (breaking the
    # files-by-user report) or cascade-delete them (silently shrinking a
    # client's historical file count). Deactivated users can't log in and
    # are hidden from the client/task assignment flow, but their past jobs
    # still count correctly in both reports.
    is_active = Column(Boolean, nullable=False, default=True)

    grants = relationship("UserTaskAccess", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("JobHistory", back_populates="user")


class UserTaskAccess(Base):
    """One row per (user, client, task) grant. A user with N grants sees N task
    cards on their post-login dashboard (or is dropped straight into the task
    page when N == 1)."""
    __tablename__ = "user_task_access"
    __table_args__ = (UniqueConstraint("user_id", "client_id", "task_id", name="uq_user_client_task"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)

    user = relationship("User", back_populates="grants")
    client = relationship("Client", back_populates="grants")
    task = relationship("Task", back_populates="grants")


class JobHistory(Base):
    """One row per processed job. client_id/task_id are denormalized onto the row
    (not just reachable via user_id) so 'files by user' and 'files by client' are
    both cheap direct-column queries — important since 2 users can work one client."""
    __tablename__ = "job_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    output_filename = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False)  # "success" | "failed"

    user = relationship("User", back_populates="jobs")
    client = relationship("Client", back_populates="jobs")
    task = relationship("Task", back_populates="jobs")
