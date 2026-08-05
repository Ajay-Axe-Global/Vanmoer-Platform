import datetime
import os

import jwt
from werkzeug.security import check_password_hash, generate_password_hash

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-.env")
JWT_ALGO = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "12"))


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def issue_token(user) -> str:
    """user: database.models.User instance (with .grants eagerly usable)."""
    payload = {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        # One entry per UserTaskAccess grant — a user with 2 grants (e.g.
        # Carpenter Inbound + Outbound) carries both in a single token.
        "grants": [
            {"client_slug": g.client.slug, "task_slug": g.task.slug}
            for g in user.grants
        ],
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
