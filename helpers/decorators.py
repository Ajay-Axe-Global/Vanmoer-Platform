from functools import wraps

import jwt as pyjwt
from flask import g, jsonify, request

from helpers.jwt_utils import decode_token


def _get_token_from_header():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _authenticate():
    token = _get_token_from_header()
    if not token:
        return None, (jsonify({"error": "Missing or invalid Authorization header"}), 401)
    try:
        payload = decode_token(token)
    except pyjwt.ExpiredSignatureError:
        return None, (jsonify({"error": "Token expired"}), 401)
    except pyjwt.InvalidTokenError:
        return None, (jsonify({"error": "Invalid token"}), 401)
    return payload, None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        payload, err = _authenticate()
        if err:
            return err
        g.user = payload
        return f(*args, **kwargs)
    return wrapper


def role_required(role):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            payload, err = _authenticate()
            if err:
                return err
            if payload.get("role") != role:
                return jsonify({"error": "Forbidden"}), 403
            g.user = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


def task_access_required(client_slug: str, task_slug: str):
    """
    Admins can reach every task. Regular users may only reach a client/task
    they've been explicitly granted — the JWT carries a list of grants
    (see database/models.py UserTaskAccess), since one login can now hold
    several (e.g. Carpenter Inbound + Outbound for the same person).
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            payload, err = _authenticate()
            if err:
                return err
            if payload.get("role") != "admin":
                grants = payload.get("grants", [])
                has_access = any(
                    g_.get("client_slug") == client_slug and g_.get("task_slug") == task_slug
                    for g_ in grants
                )
                if not has_access:
                    return jsonify({"error": "Forbidden — you do not have access to this task"}), 403
            g.user = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator
