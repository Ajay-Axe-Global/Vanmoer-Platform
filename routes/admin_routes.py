import datetime

from flask import Blueprint, g, jsonify, request

from admin import service
from database.backup import backup_now
from helpers.decorators import role_required

bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


@bp.route("/clients", methods=["GET"])
@role_required("admin")
def get_clients():
    return jsonify(service.list_clients())


@bp.route("/clients", methods=["POST"])
@role_required("admin")
def post_client():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        return jsonify(service.create_client(name)), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/tasks", methods=["GET"])
@role_required("admin")
def get_tasks():
    return jsonify(service.list_tasks())


@bp.route("/users", methods=["GET"])
@role_required("admin")
def get_users():
    return jsonify(service.list_users())


@bp.route("/users", methods=["POST"])
@role_required("admin")
def post_user():
    data = request.get_json(silent=True) or {}
    try:
        result = service.create_user(
            name=data.get("name", ""),
            username=data.get("username", ""),
            password=data.get("password", ""),
            role=data.get("role", "user"),
            grants=data.get("grants"),  # [{"client_slug": ..., "task_slug": ...}, ...]
        )
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/users/<int:user_id>", methods=["PUT"])
@role_required("admin")
def put_user(user_id):
    data = request.get_json(silent=True) or {}
    try:
        result = service.update_user(
            user_id,
            name=data.get("name", ""),
            username=data.get("username", ""),
            password=data.get("password") or None,
            role=data.get("role", "user"),
            grants=data.get("grants"),
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/users/<int:user_id>", methods=["DELETE"])
@role_required("admin")
def delete_user(user_id):
    try:
        result = service.set_user_active(user_id, active=False, acting_user_id=g.user["user_id"])
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/users/<int:user_id>/reactivate", methods=["POST"])
@role_required("admin")
def reactivate_user(user_id):
    try:
        result = service.set_user_active(user_id, active=True, acting_user_id=g.user["user_id"])
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/jobs/by-user", methods=["GET"])
@role_required("admin")
def get_jobs_by_user():
    return jsonify(service.jobs_by_user())


@bp.route("/jobs/by-client", methods=["GET"])
@role_required("admin")
def get_jobs_by_client():
    return jsonify(service.jobs_by_client())


@bp.route("/jobs/summary", methods=["GET"])
@role_required("admin")
def get_jobs_summary():
    since = request.args.get("since")
    until = request.args.get("until")
    return jsonify(service.jobs_summary(
        since=datetime.datetime.fromisoformat(since) if since else None,
        until=datetime.datetime.fromisoformat(until) if until else None,
    ))


@bp.route("/jobs", methods=["GET"])
@role_required("admin")
def get_jobs():
    user_id = request.args.get("user_id", type=int)
    return jsonify(service.list_jobs(
        user_id=user_id,
        client_slug=request.args.get("client_slug") or None,
        task_slug=request.args.get("task_slug") or None,
        status=request.args.get("status") or None,
        limit=request.args.get("limit", default=200, type=int),
    ))


@bp.route("/stats", methods=["GET"])
@role_required("admin")
def get_stats():
    return jsonify(service.dashboard_stats(
        client_slug=request.args.get("client_slug") or None,
        task_slug=request.args.get("task_slug") or None,
    ))


@bp.route("/backup", methods=["POST"])
@role_required("admin")
def post_backup():
    backup_now()
    return jsonify({"status": "ok"})
