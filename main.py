"""
Vanmoer Platform — Flask app factory.
This file stays generic: adding a new client/task never requires touching it,
only clients/__init__.py's TASK_REGISTRY (see routes/__init__.py).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, send_from_directory

from database.backup import backup_now
from database.db import init_db
from routes import register_all

BASE_DIR = Path(__file__).parent
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "1"))


def _start_backup_scheduler():
    # Flask's debug reloader forks a second process; only the actual worker
    # process (not the reloader's watcher parent) should run the scheduler,
    # or backups would fire twice as often as configured.
    if os.getenv("FLASK_DEBUG", "1") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(backup_now, "interval", hours=BACKUP_INTERVAL_HOURS, id="db_backup")
    scheduler.start()


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max upload

    init_db()
    register_all(app)
    _start_backup_scheduler()

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
        # Dev mode: Werkzeug's reloader + debugger, single-threaded — fine for local iteration.
        app.run(debug=True, port=port)
    else:
        # Everything else: waitress, a real multi-threaded WSGI server. The
        # Flask dev server used here before this was never meant to hold up
        # under concurrent users — this is the fix for that.
        from waitress import serve
        threads = int(os.getenv("WAITRESS_THREADS", "16"))
        print(f"Starting waitress on 0.0.0.0:{port} with {threads} threads")
        serve(app, host="0.0.0.0", port=port, threads=threads)
