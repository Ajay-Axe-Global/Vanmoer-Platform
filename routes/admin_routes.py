import datetime

from flask import Blueprint, g, jsonify, request
from admin import service
from database.backup import backup_now
from helpers import billing
from helpers.decorators import role_required

bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def _billing_period_and_filters():
    """Shared arg-parsing for every /billing/* GET route below — same
    period/since/until/tz convention as /jobs/summary etc., plus the
    billing-specific client/task/user/model filters. Returns
    (since, until, filters_dict) or raises ValueError (caller returns 400)."""
    period = request.args.get("period", "today")
    since, until = service.period_range(
        period, request.args.get("since"), request.args.get("until"),
        tz_name=request.args.get("tz"),
    )
    filters = {
        "client_slug": request.args.get("client_slug") or None,
        "task_slug": request.args.get("task_slug") or None,
        "user_id": request.args.get("user_id", type=int),
        "model_name": request.args.get("model_name") or None,
    }
    return since, until, filters


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
    return jsonify(service.list_users(
        client_slug=request.args.get("client_slug") or None,
        task_slug=request.args.get("task_slug") or None,
    ))


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
    period = request.args.get("period", "today")
    try:
        since, until = service.period_range(
            period, request.args.get("since"), request.args.get("until"),
            tz_name=request.args.get("tz"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.jobs_summary(
        since=since, until=until,
        user_id=request.args.get("user_id", type=int),
        client_slug=request.args.get("client_slug") or None,
        task_slug=request.args.get("task_slug") or None,
        search=request.args.get("search") or None,
    ))


@bp.route("/jobs/productivity", methods=["GET"])
@role_required("admin")
def get_jobs_productivity():
    period = request.args.get("period", "today")
    try:
        since, until = service.period_range(
            period, request.args.get("since"), request.args.get("until"),
            tz_name=request.args.get("tz"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.productivity_by_user(
        since=since, until=until,
        client_slug=request.args.get("client_slug") or None,
        task_slug=request.args.get("task_slug") or None,
        user_id=request.args.get("user_id", type=int),
        search=request.args.get("search") or None,
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
        tz_name=request.args.get("tz"),
    ))


@bp.route("/stats/by-client", methods=["GET"])
@role_required("admin")
def get_stats_by_client():
    period = request.args.get("period", "today")
    try:
        since, until = service.period_range(
            period, request.args.get("since"), request.args.get("until"),
            tz_name=request.args.get("tz"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.files_by_client(since, until, task_slug=request.args.get("task_slug") or None))


@bp.route("/backup", methods=["POST"])
@role_required("admin")
def post_backup():
    backup_now()
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════════════
# BILLING & USAGE (Gemini token cost)
# ═══════════════════════════════════════════════════════════════════════════

@bp.route("/billing/summary", methods=["GET"])
@role_required("admin")
def get_billing_summary():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.billing_summary(since, until, **f))


@bp.route("/billing/usage-by-day", methods=["GET"])
@role_required("admin")
def get_billing_usage_by_day():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.usage_by_day(since, until, **f, tz_name=request.args.get("tz")))


@bp.route("/billing/high-demand-days", methods=["GET"])
@role_required("admin")
def get_billing_high_demand_days():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(service.high_demand_days(
        since, until, **f, tz_name=request.args.get("tz"),
        top_n=request.args.get("top_n", default=5, type=int),
    ))


@bp.route("/billing/usage-by-model", methods=["GET"])
@role_required("admin")
def get_billing_usage_by_model():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    f.pop("model_name", None)
    return jsonify(service.usage_by_model(since, until, **f))


@bp.route("/billing/usage-by-client", methods=["GET"])
@role_required("admin")
def get_billing_usage_by_client():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    f.pop("client_slug", None)
    return jsonify(service.usage_by_client(since, until, **f))


@bp.route("/billing/usage-by-task", methods=["GET"])
@role_required("admin")
def get_billing_usage_by_task():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    f.pop("task_slug", None)
    return jsonify(service.usage_by_task(since, until, **f))


@bp.route("/billing/usage-by-user", methods=["GET"])
@role_required("admin")
def get_billing_usage_by_user():
    try:
        since, until, f = _billing_period_and_filters()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    f.pop("user_id", None)
    return jsonify(service.usage_by_user(since, until, **f))


@bp.route("/billing/pricing", methods=["GET"])
@role_required("admin")
def get_billing_pricing():
    return jsonify(billing.list_model_pricing())


@bp.route("/billing/pricing", methods=["POST"])
@role_required("admin")
def post_billing_pricing():
    data = request.get_json(silent=True) or {}
    try:
        effective_from = (
            datetime.datetime.fromisoformat(data["effective_from"])
            if data.get("effective_from") else datetime.datetime.utcnow()
        )
        result = billing.create_model_pricing(
            model_name=data.get("model_name", ""),
            input_price=float(data.get("input_price_usd_per_million", -1)),
            output_price=float(data.get("output_price_usd_per_million", -1)),
            effective_from=effective_from,
            created_by=g.user["user_id"],
        )
        return jsonify(result), 201
    except (ValueError, TypeError, KeyError) as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/billing/exchange-rate", methods=["GET"])
@role_required("admin")
def get_billing_exchange_rate():
    return jsonify(billing.list_exchange_rates())


@bp.route("/billing/exchange-rate", methods=["POST"])
@role_required("admin")
def post_billing_exchange_rate():
    data = request.get_json(silent=True) or {}
    try:
        effective_from = (
            datetime.datetime.fromisoformat(data["effective_from"])
            if data.get("effective_from") else datetime.datetime.utcnow()
        )
        result = billing.create_exchange_rate(
            usd_to_inr=float(data.get("usd_to_inr", -1)),
            effective_from=effective_from,
            created_by=g.user["user_id"],
        )
        return jsonify(result), 201
    except (ValueError, TypeError, KeyError) as e:
        return jsonify({"error": str(e)}), 400
