from flask import Blueprint, jsonify, request

from database.db import SessionLocal
from database.models import User
from helpers.jwt_utils import issue_token, verify_password

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user or not verify_password(password, user.password_hash):
            return jsonify({"error": "Invalid username or password"}), 401
        if not user.is_active:
            return jsonify({"error": "This account has been deactivated"}), 403

        return jsonify({
            "token": issue_token(user),
            "role": user.role,
            "name": user.name,
            "username": user.username,
            "grants": [
                {"client_slug": grant.client.slug, "client_name": grant.client.name,
                 "task_slug": grant.task.slug, "task_name": grant.task.name}
                for grant in user.grants
            ],
        })

    finally:
        session.close()
