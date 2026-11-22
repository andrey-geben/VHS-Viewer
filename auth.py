"""
Аутентификация VHS-Viewer.

Простая авторизация по сессиям:
    * логины и пароли — в viewer_config.yaml;
    * сессия — cookie, время жизни — из конфига;
    * если security.auth_enabled: false — авторизация отключена.
"""

import hmac
import logging
from datetime import timedelta
from functools import wraps

from werkzeug.security import check_password_hash, generate_password_hash

from flask import (
    current_app,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Внутренние хелперы
# ----------------------------------------------------------------------
def _security_cfg() -> dict:
    cfg = current_app.config.get("VIEWER_CONFIG", {}) or {}
    return cfg.get("security", {}) or {}


HASH_PREFIXES = ("pbkdf2:", "scrypt:")


def hash_password(password: str) -> str:
    """Возвращает хеш пароля (pbkdf2/scrypt, как в Werkzeug)."""
    return generate_password_hash(password)


def verify_password(stored: str, provided: str) -> bool:
    """Проверяет пароль: поддерживает хеши и старый открытый формат."""
    stored = stored or ""
    if stored.startswith(HASH_PREFIXES):
        try:
            return check_password_hash(stored, provided)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(stored, provided or "")


def is_password_hash(value: str) -> bool:
    return bool(value) and value.startswith(HASH_PREFIXES)


def _find_user(username: str, password: str) -> dict | None:
    sec = _security_cfg()
    if not sec.get("auth_enabled", True):
        return {"username": username or "anonymous", "role": "admin"}
    for u in sec.get("users", []):
        if u.get("username") == username and verify_password(u.get("password", ""), password):
            return {
                "username": u["username"],
                "role": u.get("role", "admin"),
            }
    return None


def _current_user() -> dict | None:
    sec = _security_cfg()
    if not sec.get("auth_enabled", True):
        return {"username": "anonymous", "role": "admin"}
    return session.get("user")


# ----------------------------------------------------------------------
# Декораторы
# ----------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if user is None:
            accept = request.headers.get("Accept", "")
            if request.path.startswith("/api/") or "application/json" in accept:
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        g.user = user
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    """Пускает только пользователей с ролью 'admin'."""

    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if getattr(g, "user", {}).get("role") != "admin":
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)

    return wrapper


# ----------------------------------------------------------------------
# Публичные функции
# ----------------------------------------------------------------------
def do_login(username: str, password: str) -> bool:
    user = _find_user(username, password)
    if not user:
        return False
    session["user"] = user
    session.permanent = True
    logger.info("Вход: %s (%s)", user["username"], user["role"])
    return True


def do_logout():
    user = session.pop("user", None)
    if user:
        logger.info("Выход: %s", user.get("username"))


def get_current_user() -> dict | None:
    return _current_user()


def apply_session_lifetime(app, config: dict):
    """Настраивает время жизни сессии из конфига."""
    sec = config.get("security", {}) or {}
    hours = int(sec.get("session_lifetime_hours", 24))
    app.permanent_session_lifetime = timedelta(hours=hours)
