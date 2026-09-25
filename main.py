"""
Vanmoer Platform — Flask app factory.
This file stays generic: adding a new client/task never requires touching it,
only clients/__init__.py's TASK_REGISTRY (see routes/__init__.py).
"""
import os
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Windows' console defaults sys.stdout/stderr to the system codepage (often
# cp1252), which has no mapping for many characters client extractors' own
# debug print()s use freely (→, ✓, etc. — e.g. Sabic Inbound's cross-check
# fix logging). A print() hitting one of those crashes with
# UnicodeEncodeError mid-request, which the route's generic exception
# handler then surfaces to the user as a raw Python error instead of any
# real processing failure. Reconfiguring both streams to UTF-8 at startup
# fixes this for every client's prints at once, not just one call site.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, send_from_directory 

from database.scheduled_backup import backup_and_sync
from database.seed import seed
from helpers.email_worker import start_worker as start_email_worker
from helpers.screenshot_worker import start_worker as start_screenshot_worker
from routes import register_all

BASE_DIR = Path(__file__).parent
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "1"))


def _lan_ip() -> str:
    # Doesn't actually send anything (UDP), just asks the OS which local
    # interface it would use to reach an external address.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _running_in_reloader_watcher() -> bool:
    # Flask's debug reloader forks a second process; only the actual worker
    # process (not the reloader's watcher parent) should run singleton
    # background work, or it fires/starts twice.
    return os.getenv("FLASK_DEBUG", "1") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true"


def _start_backup_scheduler():
    if _running_in_reloader_watcher():
        return
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(backup_and_sync, "interval", hours=BACKUP_INTERVAL_HOURS, id="db_backup")
    scheduler.start()


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

    # Idempotent: creates clients/tasks/the default admin only if missing, so
    # it's safe to run on every boot instead of requiring a manual
    # `python -m database.seed` before first login.
    seed()
    register_all(app)
    _start_backup_scheduler()
    if not _running_in_reloader_watcher():
        # Two independent single-concurrency workers — see
        # helpers/email_worker.py's module docstring for why sending
        # emails doesn't share the screenshot worker's physical-desktop
        # constraint and gets its own dedicated thread instead.
        start_screenshot_worker()
        start_email_worker()

    @app.route("/")
    @app.route("/login")
    def login_page():
        return send_from_directory(BASE_DIR / "auth", "login.html")

    @app.route("/dashboard")
    def dashboard_page():
        return send_from_directory(BASE_DIR / "auth", "dashboard.html")

    @app.route("/auth/<path:filename>")
    def auth_assets(filename):
        return send_from_directory(BASE_DIR / "auth", filename)

    @app.route("/admin")
    def admin_page():
        return send_from_directory(BASE_DIR / "admin" / "templates", "dashboard.html")

    @app.route("/admin/<path:filename>")
    def admin_assets(filename):
        return send_from_directory(BASE_DIR / "admin", filename)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    if os.getenv("FLASK_DEBUG", "1") == "1":
        # Dev mode: Werkzeug's reloader + debugger.
        # host=0.0.0.0 so other machines on the LAN can reach it, not just localhost.
        # threaded=True: the admin dashboard fires ~8 API calls on a single
        # page load (stats, summary, users, clients, tasks, the two by-client
        # charts, productivity) — without this, Werkzeug serves them one at a
        # time even when the browser sends them concurrently, so the page
        # visibly waits on the sum of every query's DB time instead of the
        # slowest one. All of these are read-only, and the DB is in WAL mode
        # (see database/db.py), so concurrent reads from multiple threads are
        # safe here.
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            # Reloader's forked worker process — the one that actually serves requests.
            print(f"Running on http://{_lan_ip()}:{port} (LAN)")
        app.run(debug=True, host="0.0.0.0", port=port, threaded=True)
    else:
        # Everything else: waitress, a real multi-threaded WSGI server. The
        # Flask dev server used here before this was never meant to hold up
        # under concurrent users — this is the fix for that.
        from waitress import serve
        threads = int(os.getenv("WAITRESS_THREADS", "16"))
        print(f"Starting waitress on http://{_lan_ip()}:{port} (LAN) with {threads} threads")
        serve(app, host="0.0.0.0", port=port, threads=threads)

