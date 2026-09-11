import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
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
    # Real-world business key extracted from the source document(s) — e.g. a
    # SABIC shipment number or a Carpenter TCS reference — so an admin can
    # trace a generated Excel back to the order it came from. Comma-joined
    # when a single run covers several (e.g. a multi-item dispatch advice).
    # Rows logged before this column existed are simply NULL.
    reference = Column(String(255), nullable=True)
    source_filename = Column(String(500), nullable=True)
    row_count = Column(Integer, nullable=True)
    # Distinct reference count — e.g. 5 dispatch-advice shipments bundled into
    # one Sabic Outbound batch, or 3 distinct order refs merged into one
    # Carpenter Inbound Excel. This, not row_count and not "one row per job",
    # is what the admin dashboard's "Files" totals are counted by. NULL on
    # rows logged before this column existed, or on a failed run.
    reference_count = Column(Integer, nullable=True)

    user = relationship("User", back_populates="jobs")
    client = relationship("Client", back_populates="jobs")
    task = relationship("Task", back_populates="jobs")


class ModelPricing(Base):
    """Admin-entered $/1M-token price for a Gemini model, effective from a
    given moment onward. Adding a new row (when Google changes prices) never
    edits or deletes the old one — cost calculation always picks whichever
    row's effective_from is the latest one <= the Gemini call's own
    timestamp (see helpers/billing.get_active_pricing()), so a bill already
    computed and stored on GeminiUsageLog never retroactively changes just
    because a newer price was entered later. Multiple models can be priced
    independently (e.g. if a client ever moves onto gemini-2.5-pro)."""
    __tablename__ = "model_pricing"

    id = Column(Integer, primary_key=True)
    model_name = Column(String(120), nullable=False)
    input_price_usd_per_million = Column(Numeric(12, 6), nullable=False)
    output_price_usd_per_million = Column(Numeric(12, 6), nullable=False)
    effective_from = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    # A clearly-labeled placeholder seeded at first boot (see database/seed.py)
    # so the Billing page isn't empty before a real admin-entered price
    # exists — surfaced in the UI so nobody mistakes it for a real number.
    is_placeholder = Column(Boolean, nullable=False, default=False)


class ExchangeRate(Base):
    """USD->INR rate, same 'effective_from, never edited in place' pattern
    as ModelPricing — every GeminiUsageLog row snapshots the rate it used at
    write time, so a rate update never silently changes a past cost figure
    already shown/exported to someone."""
    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True)
    usd_to_inr = Column(Numeric(12, 6), nullable=False)
    effective_from = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    is_placeholder = Column(Boolean, nullable=False, default=False)


class GeminiUsageLog(Base):
    """One row per Gemini API call (not per job — a single job/run typically
    makes 2-4+ calls, e.g. carrier-id + MBL + packing-list) — see
    helpers/gemini_client.call_gemini() for how usage is captured and
    helpers/billing.record_usage_for_job() for how these rows get written
    (from helpers/jobs.log_job(), the one chokepoint every client task
    already runs through). user_id/client_id/task_id are denormalized onto
    the row (same philosophy as JobHistory) so per-user/per-client/per-task
    billing aggregation is a plain indexed GROUP BY, no join required.

    Cost columns are computed and stored AT WRITE TIME using whichever
    ModelPricing/ExchangeRate rows were active then — immutable historical
    fact, never recomputed later from current prices."""
    __tablename__ = "gemini_usage_log"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("job_history.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    model_name = Column(String(120), nullable=False)
    # Short label for which extraction step this call was (e.g.
    # "carrier_id", "mbl", "packing_list") — optional, purely diagnostic
    # (lets a later "cost per step" breakdown exist without a schema
    # change); blank when a call site doesn't pass one.
    call_label = Column(String(80), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    input_cost_usd = Column(Numeric(14, 8), nullable=False, default=0)
    output_cost_usd = Column(Numeric(14, 8), nullable=False, default=0)
    total_cost_usd = Column(Numeric(14, 8), nullable=False, default=0)
    exchange_rate_used = Column(Numeric(12, 6), nullable=False, default=0)
    total_cost_inr = Column(Numeric(14, 6), nullable=False, default=0)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    job = relationship("JobHistory")
    user = relationship("User")
    client = relationship("Client")
    task = relationship("Task")
