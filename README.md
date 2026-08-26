# Vanmoer Platform

Internal document-automation platform for Axe Global's freight-forwarding
clients. Users upload source documents (MBLs, packing lists, invoices,
arrival notices, scanned dispatch docs, ...) for a given client + task, the
platform runs LLM-assisted extraction and validation over them, and returns
a formatted Excel outcome file. All activity is logged for admin reporting.

## What it does

- **Multi-client, multi-task**: each client (e.g. Sabic, Carpenter) has one
  or more tasks (e.g. Inbound, Outbound), each with its own document
  requirements, extraction logic, and Excel output format.
- **LLM extraction**: PDF documents are parsed and key fields extracted via
  Google Gemini (`helpers/gemini_client.py`), then cross-validated against
  each other (e.g. MBL vs packing list vs invoice).
- **Auth + RBAC**: JWT-based login. Admins have access to everything; regular
  users are granted specific client+task combinations
  (`database/models.py: UserTaskAccess`).
- **Admin dashboard**: manage users, clients, tasks, and access grants; view
  job history and stats by user/client; trigger manual DB backups.
- **Job history**: every processed file is logged (`database/models.py:
  JobHistory`) with the user, client, task, source filename, extracted
  business reference, row count, and success/failure status.
- **Scheduled backups**: the running app periodically snapshots the SQLite
  database (`database/backup.py`, interval set by `BACKUP_INTERVAL_HOURS`).

## Tech stack

- **Backend**: Flask 3 (app factory in `main.py`), SQLAlchemy 2 + SQLite
  (WAL mode), PyJWT for auth, APScheduler for backups.
- **Document processing**: pymupdf / pdfplumber / pypdf for PDF parsing,
  google-generativeai (Gemini) for extraction, openpyxl/pandas for Excel
  output.
- **Frontend**: plain HTML/JS served directly by Flask (`auth/`, `admin/`,
  and each task's `templates/` folder) — no build step, no SPA framework.
- **Dev server**: Werkzeug reloader. **Production**: waitress (multi-threaded
  WSGI server), switched on via `FLASK_DEBUG=0`.

## Project layout

```
main.py                    Flask app factory — generic, rarely needs editing
routes/
  __init__.py               register_all(app) — wires auth + admin + every client blueprint
  auth_routes.py             /auth/* — login, token issuance
  admin_routes.py            /admin/* — users, clients, tasks, jobs, stats, backup
auth/                       Login + post-login dashboard (static HTML/JS)
admin/                      Admin dashboard (static HTML/JS + admin/service.py)
database/
  models.py                  Client, Task, User, UserTaskAccess, JobHistory
  db.py                       SQLAlchemy engine/session setup (SQLite, WAL)
  seed.py                     Bootstraps clients/tasks/first admin on boot
  backup.py                   DB snapshot logic
helpers/
  base_task.py                BaseTask — abstract contract every task implements
  decorators.py                login_required / role_required / task_access_required
  gemini_client.py             Gemini API wrapper
  excel_writer.py              Generic Excel writer used by most tasks
  jobs.py                      Job dir/output path helpers, log_job()
  jwt_utils.py                 Token issuance/decoding, password hashing
clients/
  __init__.py                  TASK_REGISTRY — single source of truth for task blueprints
  <client_slug>/<task_slug>/   One folder per client task (task.py, extraction logic, templates/)
uploads/                     Per-job upload + output storage (gitignored)
```

## How a request flows

1. User logs in (`/auth/login`) → gets a JWT carrying their role and grants.
2. User opens a task page (e.g. `/app/sabic/inbound/`), uploads the required
   documents.
3. The task's Flask blueprint (`clients/<client>/<task>/task.py`) validates
   the upload, saves files to a per-job directory (`helpers/jobs.py`), and
   calls its `BaseTask.process(files)`.
4. `process()` extracts data (often via Gemini), cross-validates it, and
   returns `{"rows": [...], "summary": {...}}`.
5. The blueprint writes the Excel file (`helpers/excel_writer.write_excel`,
   or the task writes its own output if `writes_own_output = True`) and logs
   the job (`helpers/jobs.log_job`).
6. The user downloads the generated Excel; the run shows up in the admin
   dashboard's job history and stats.

## Setup

1. **Clone and install dependencies**
   ```
   pip install -r requirements.txt
   ```
2. **Configure environment**
   ```
   cp .env.example .env
   ```
   Fill in `GEMINI_API_KEY` and a real `JWT_SECRET` at minimum. See
   `.env.example` for what every variable does.
3. **Run the app**
   ```
   python main.py
   ```
   On first boot, `main.py` calls `database.seed.seed()` automatically,
   which creates `database/app.db`, seeds the `Carpenter`/`Sabic` clients and
   `Inbound`/`Outbound` tasks, and creates a default admin login:
   ```
   username: admin
   password: admin123   <- change this immediately after first login
   ```
4. **Log in** at `http://localhost:5000/` (or the LAN address printed on
   startup) and change the admin password from the dashboard.

Dev mode (`FLASK_DEBUG=1`, the default) runs Werkzeug's dev server on
`0.0.0.0` so it's reachable from other machines on the LAN. Set
`FLASK_DEBUG=0` for anything beyond local testing — the app then serves
through waitress instead.

## Adding a new client or task

**Important: the Admin Page's "Clients" tab is not where a new client gets
built.** Adding a row there only creates an RBAC/reporting label — it does
not create a template generator, an extraction pipeline, or an upload page.
The actual client is built **entirely inside the `clients/` folder**,
following the same architecture every existing client follows. The admin
entry is just the last, mechanical step so the client can be granted to
users and shown in reports.

The codebase is deliberately structured so building a new client never
requires touching `main.py` or `routes/__init__.py` — only:
`clients/<new_folder>/...` + one import line in `clients/__init__.py`.

### 1. Build the client entirely inside `clients/`

Create `clients/<client_slug>/<task_slug>/` and build it out like every
other client:

```
clients/<client_slug>/<task_slug>/
  task.py            <TaskName>Task(BaseTask) + the Flask Blueprint (bp)
  <extraction>.py     document parsing / field extraction logic
  templates/<client_slug>_<task_slug>/index.html   upload UI
```

This is the real work, and it must follow the existing architecture rather
than reinventing it per client:

- **Subclass `helpers.base_task.BaseTask`** and implement `process(files)` —
  same contract every task already follows (see `helpers/base_task.py`).
  Don't invent a parallel task shape.
- **Reuse the shared helpers instead of rewriting them per client**:
  - `helpers/gemini_client.py` for LLM extraction calls
  - `helpers/excel_writer.write_excel` for standard tabular output (only set
    `writes_own_output = True` and write the file yourself if the output
    format is genuinely bespoke, like `clients/carpenter/inbound/task.py`)
  - `helpers/jobs.py` (`new_job_dir`, `job_output_path`, `log_job`,
    `build_reference`) for job storage + history logging
  - `helpers/decorators.task_access_required` on every `/process` and
    `/download` route, so RBAC is enforced the same way everywhere
- **Copy the closest existing task as your starting template**:
  - `clients/sabic/inbound/task.py` — generic Excel output, the simplest
    reference pattern
  - `clients/carpenter/inbound/task.py` — bespoke/merged output,
    `writes_own_output=True`
  This keeps every client's `task.py` structured the same way (task class →
  blueprint → `index`/`process`/`download` routes), which is what makes the
  platform's "just register a blueprint" model work at all.

The skeleton of `task.py` looks like this — same shape in every client:

```python
from flask import Blueprint, jsonify, render_template, request, g
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required

CLIENT_SLUG = "newclient"
TASK_SLUG = "inbound"

class NewClientInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "NewClient Inbound"

    required_documents = [
        {"key": "invoice", "label": "Commercial Invoice", "accept": ".pdf", "multiple": False},
    ]
    column_config = [
        {"header": "Ref No", "field_key": "ref_no", "width": 22},
        # ...
    ]

    def process(self, files: dict, output_path: str | None = None) -> dict:
        # extract (helpers/gemini_client.py), validate, build rows
        return {"rows": rows, "summary": summary}

_task = NewClientInboundTask()
bp = Blueprint("newclient_inbound", __name__, template_folder="templates",
               url_prefix="/app/newclient/inbound")

@bp.route("/")
def index():
    return render_template("newclient_inbound/index.html")

@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    ...  # see clients/sabic/inbound/task.py for the full reference pattern
```

### 2. Register the blueprint

In `clients/__init__.py`:

```python
from clients.newclient.inbound.task import bp as newclient_inbound_bp

TASK_REGISTRY = [
    ...,
    newclient_inbound_bp,
]
```

That's it — `main.py` and `routes/__init__.py` never change; the new task is
picked up automatically via `TASK_REGISTRY`.

### 3. Add the RBAC/reporting entry (the mechanical last step)

Only now does the Admin Page come in. Open **Admin → Clients** and add the
new client by name (`POST /api/admin/clients` → `admin/service.py:
create_client`, which slugifies the name and inserts a `Client` row — see
`database/models.py: Client`). Tasks (`Inbound`/`Outbound`) are shared
across clients and already seeded, so you normally don't need to add a new
one — reuse an existing task slug if it fits.

This step exists purely so the platform can show the client in grants and
reports; **it has no effect on what code runs** — that's entirely
determined by `TASK_REGISTRY` and the `client_slug`/`task_slug` your
blueprint's routes use. If the slug the admin panel generates doesn't match
the `CLIENT_SLUG` used in your `task.py`, grants and reporting will silently
fail to line up — keep them identical.

### 4. Grant users access

In **Admin → Users**, grant the relevant users the new client+task
combination (edit user → check the new grant). Admin accounts already have
access to everything with no grant needed.

## Admin Page

The admin dashboard (`/admin`, served from `admin/templates/dashboard.html`
+ `admin/admin.js`) is a static single-page UI that talks to the JSON API
under `/api/admin/*` (`routes/admin_routes.py`, business logic in
`admin/service.py`). All of it is gated by `role_required("admin")` — only
`User.role == "admin"` accounts can reach it.

| Area | What it does | Endpoint(s) |
|---|---|---|
| **Clients** | List / add clients (RBAC + reporting label only — see above) | `GET/POST /api/admin/clients` |
| **Tasks** | List tasks (`Inbound`/`Outbound`, shared across clients) | `GET /api/admin/tasks` |
| **Users** | List/filter, create, edit, deactivate, reactivate users; assign client+task grants | `GET/POST /api/admin/users`, `PUT/DELETE /api/admin/users/<id>`, `POST /api/admin/users/<id>/reactivate` |
| **Jobs** | Drill-down job history: by user, by client, filtered/paginated list, grouped summary table | `GET /api/admin/jobs`, `/jobs/by-user`, `/jobs/by-client`, `/jobs/summary` |
| **Stats** | Dashboard tiles + files-per-day chart, files-by-client chart, productivity-by-user chart | `GET /api/admin/stats`, `/stats/by-client`, `/jobs/productivity` |
| **Backup** | Trigger an immediate DB snapshot (same routine the scheduler runs) | `POST /api/admin/backup` |

Notes:

- **Users are deactivated, not deleted** (`User.is_active`) — this keeps
  historical `JobHistory` rows intact instead of orphaning or cascading
  them; a deactivated user just can't log in and drops out of grant/user
  pickers until reactivated.
- **"Files" vs "runs"**: most stats/report endpoints count distinct
  business references (`JobHistory.reference_count`), not raw job rows — a
  single batch run covering 5 shipments counts as 5 files. Success/failure
  rate stays run-based. See the docstring on `admin/service.py:
  dashboard_stats` if you're adding a new report.
- Date-range filters (`today` / `week` / `month` / `custom`) are resolved
  server-side in UTC (`admin/service.py: period_range`) so "This week" means
  the same thing regardless of the viewer's timezone or local clock.

## Notes / gotchas

- `database/app.db` is a live SQLite database in WAL mode — you'll also see
  `app.db-shm` / `app.db-wal` sidecar files while the app is running. Don't
  delete those while the server is up.
- `uploads/` holds per-job source files and generated Excel output; it's
  gitignored and grows over time — there's currently no automatic cleanup.
- The default admin password (`admin123`) is only safe for local/dev use —
  always change it before exposing the app beyond localhost.
- `GEMINI_API_KEY` is required for any task that does LLM extraction
  (currently all of them) — the app will still boot without it, but
  document processing will fail at runtime.
