"""
VHS-Viewer — компактный веб-клиент для просмотра потоков VHS-Streamer.

Поддерживает:
    * Несколько серверов VHS-Streamer.
    * Автоматическую загрузку списка потоков через публичный API.
    * Ручное добавление потоков по URL (fallback).
    * Выбор потоков галочками и раскладку сетки.
    * Авторизацию (сессии + login).
    * Управление пользователями (только для admin).
"""
import logging
import sys
import webbrowser
from pathlib import Path
from threading import Timer

import requests
import yaml
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    redirect, url_for, g, session,
)

# ----------------------------------------------------------------------
# Авторство и версия
# ----------------------------------------------------------------------
from __version__ import (
    APP_NAME, APP_TITLE, APP_FOOTER,
    __version__, __author__, __copyright__, __license__, __url__,
)

# ----------------------------------------------------------------------
# Аутентификация
# ----------------------------------------------------------------------
import auth as auth_mod


# ----------------------------------------------------------------------
# Пути
# ----------------------------------------------------------------------
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).parent.resolve()


BASE_DIR = app_dir()
RES_DIR = resource_dir()
CONFIG_PATH = BASE_DIR / "viewer_config.yaml"


# ----------------------------------------------------------------------
# Логирование
# ----------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_file = logging.FileHandler(LOG_DIR / "vhs-viewer.log", encoding="utf-8")
_file.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console, _file],
    force=True,
)
logger = logging.getLogger("vhs-viewer")

logger.info("=" * 60)
logger.info(APP_FOOTER)
logger.info("=" * 60)
logger.info("Логи: %s", LOG_DIR / "vhs-viewer.log")


# ----------------------------------------------------------------------
# Конфиг
# ----------------------------------------------------------------------
DEFAULT_CONFIG = {
    "title": "VHS-Viewer",
    "layout": "2x2",
    "servers": [],
    "streams": [],
    "security": {
        "auth_enabled": True,
        "session_lifetime_hours": 24,
        "users": [
            {"username": "admin", "password": "viewer", "role": "admin"},
        ],
    },
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        if not isinstance(cfg.get("servers"), list):
            cfg["servers"] = []
        if not isinstance(cfg.get("streams"), list):
            cfg["streams"] = []
        if not isinstance(cfg.get("security"), dict):
            cfg["security"] = dict(DEFAULT_CONFIG["security"])
        return cfg
    except Exception as e:
        logger.error("Ошибка чтения конфига: %s", e)
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if not url.endswith("/"):
        url += "/"
    return url


def _normalize_server_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


# ----------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
)
app.secret_key = "vhs-viewer-secret-change-me"

# Загружаем конфиг и применяем настройки безопасности
_startup_config = load_config()
app.config["VIEWER_CONFIG"] = _startup_config
auth_mod.apply_session_lifetime(app, _startup_config)


# ======================================================================
# Аутентификация
# ======================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    sec = _security_cfg()
    if not sec.get("auth_enabled", True):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if auth_mod.do_login(u, p):
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        error = "Неверный логин или пароль"
        logger.warning("Неудачная попытка входа: %s", u)

    return render_template(
        "login.html",
        error=error,
        footer=APP_FOOTER,
    )


@app.route("/logout")
def logout():
    auth_mod.do_logout()
    return redirect(url_for("login"))


def _security_cfg():
    cfg = load_config()
    return cfg.get("security", {}) or {}


# ======================================================================
# Главная
# ======================================================================
@app.route("/")
@auth_mod.login_required
def index():
    cfg = load_config()
    return render_template(
        "index.html",
        cfg=cfg,
        version=__version__,
        author=__author__,
        app_footer=APP_FOOTER,
        user=g.user,
    )


@app.route("/about")
@auth_mod.login_required
def about_page():
    return render_template(
        "about.html",
        info={
            "version": __version__,
            "author": __author__,
            "copyright": __copyright__,
            "license": __license__,
            "url": __url__,
        },
        user=g.user,
    )


# ======================================================================
# API: конфиг (серверы + потоки + раскладка)
# ======================================================================
@app.route("/api/config", methods=["GET"])
@auth_mod.admin_required
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
@auth_mod.login_required
def api_set_config():
    data = request.get_json(silent=True) or {}
    cfg = load_config()

    cfg["title"] = (data.get("title") or cfg.get("title") or "VHS-Viewer").strip()

    layout = data.get("layout") or cfg.get("layout") or "2x2"
    if layout not in ("1x1", "2x2", "3x3", "4x4", "auto"):
        layout = "2x2"
    cfg["layout"] = layout

    # --- Серверы ---
    raw_servers = data.get("servers", [])
    if not isinstance(raw_servers, list):
        raw_servers = []
    servers = []
    for s in raw_servers:
        if not isinstance(s, dict):
            continue
        url = _normalize_server_url(s.get("url", ""))
        if not url:
            continue
        name = (s.get("name") or "").strip() or url
        servers.append({"name": name, "url": url})
    cfg["servers"] = servers

    # --- Потоки ---
    raw_streams = data.get("streams", [])
    if not isinstance(raw_streams, list):
        raw_streams = []
    streams = []
    for i, s in enumerate(raw_streams):
        if not isinstance(s, dict):
            continue
        url = _normalize_url(s.get("url", ""))
        if not url:
            continue
        name = (s.get("name") or "").strip() or f"Камера {i+1}"
        enabled = bool(s.get("enabled", True))
        streams.append({"name": name, "url": url, "enabled": enabled})
    cfg["streams"] = streams

    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg
    logger.info("Сохранено: серверов %d, потоков %d",
                len(servers), len(streams))
    return jsonify({"ok": True, "config": cfg})


# ======================================================================
# API: проверка сервера VHS-Streamer
# ======================================================================
@app.route("/api/servers/test", methods=["POST"])
@auth_mod.admin_required
def api_server_test():
    data = request.get_json(silent=True) or {}
    url = _normalize_server_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "URL обязателен"}), 400

    try:
        r = requests.get(f"{url}/api/public/streams", timeout=5)
        if r.status_code != 200:
            return jsonify({
                "ok": False,
                "error": f"HTTP {r.status_code}",
            }), 200
        j = r.json()
        return jsonify({
            "ok": True,
            "server_name": j.get("server_name", "VHS-Streamer"),
            "streams_count": j.get("count", 0),
        })
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ======================================================================
# API: загрузка потоков со всех серверов
# ======================================================================
@app.route("/api/servers/refresh", methods=["POST"])
@auth_mod.admin_required
def api_servers_refresh():
    cfg = load_config()
    servers = cfg.get("servers", [])
    if not servers:
        return jsonify({
            "ok": True,
            "streams": [],
            "errors": [],
            "message": "Список серверов пуст",
        })

    all_streams = []
    errors = []

    for srv in servers:
        srv_url = srv.get("url", "")
        srv_name = srv.get("name", srv_url)
        if not srv_url:
            continue
        try:
            r = requests.get(f"{srv_url}/api/public/streams", timeout=5)
            r.raise_for_status()
            j = r.json()
        except requests.RequestException as e:
            logger.warning("Сервер %s недоступен: %s", srv_url, e)
            errors.append({
                "server": srv_name,
                "url": srv_url,
                "error": str(e),
            })
            continue

        for st in j.get("streams", []):
            webrtc_urls = st.get("webrtc_urls", [])
            if not webrtc_urls:
                continue
            chosen = None
            for u in webrtc_urls:
                if "локально" in (u.get("label") or "").lower():
                    chosen = u
                    break
            if not chosen:
                chosen = webrtc_urls[0]

            uid = f"{srv_url}|{st.get('path', '')}"

            all_streams.append({
                "uid": uid,
                "server": srv_name,
                "server_url": srv_url,
                "name": st.get("name") or st.get("id") or "Поток",
                "camera_name": st.get("camera_name", ""),
                "path": st.get("path", ""),
                "url": chosen["url"],
                "all_urls": webrtc_urls,
            })

    return jsonify({
        "ok": True,
        "streams": all_streams,
        "errors": errors,
        "servers_count": len(servers),
    })


# ======================================================================
# API: безопасность — управление пользователями
# ======================================================================
@app.route("/api/security/config", methods=["GET"])
@auth_mod.admin_required
def api_security_config_get():
    """
    Возвращает список пользователей с паролями. Только для admin.
    """
    cfg = load_config()
    sec = cfg.get("security", {})
    return jsonify({
        "auth_enabled": sec.get("auth_enabled", True),
        "session_lifetime_hours": sec.get("session_lifetime_hours", 24),
        "users": [
            {
                "username": u.get("username", ""),
                "password": u.get("password", ""),
                "role": u.get("role", "admin"),
            }
            for u in sec.get("users", [])
        ],
    })


@app.route("/api/security/config", methods=["POST"])
@auth_mod.admin_required
def api_security_config_set():
    """
    Сохраняет список пользователей и настройки безопасности.
    Только для admin.
    """
    data = request.get_json(silent=True) or {}

    # --- Валидация пользователей ---
    raw_users = data.get("users", [])
    if not isinstance(raw_users, list):
        return jsonify({"error": "users должен быть списком"}), 400

    cleaned = []
    seen = set()
    for i, u in enumerate(raw_users):
        if not isinstance(u, dict):
            return jsonify({"error": f"Пользователь #{i+1} — не объект"}), 400

        username = str(u.get("username", "")).strip()
        password = str(u.get("password", ""))
        role = str(u.get("role", "admin")).strip()

        if not username:
            return jsonify({"error": f"У пользователя #{i+1} пустой логин"}), 400
        if not password:
            return jsonify({"error": f"У пользователя '{username}' пустой пароль"}), 400
        if role not in ("admin", "viewer"):
            return jsonify({"error": f"Недопустимая роль '{role}' у '{username}'"}), 400
        if username in seen:
            return jsonify({"error": f"Логин '{username}' повторяется"}), 400

        seen.add(username)
        cleaned.append({
            "username": username,
            "password": password,
            "role": role,
        })

    if not cleaned:
        return jsonify({"error": "Список пользователей не может быть пустым"}), 400
    if not any(u["role"] == "admin" for u in cleaned):
        return jsonify({"error": "Должен остаться хотя бы один администратор"}), 400

    # --- Дополнительные настройки ---
    auth_enabled = bool(data.get("auth_enabled", True))
    lifetime = data.get("session_lifetime_hours")
    try:
        lifetime = int(lifetime) if lifetime is not None else 24
        if lifetime < 1 or lifetime > 24 * 30:
            lifetime = 24
    except (TypeError, ValueError):
        lifetime = 24

    # --- Применяем ---
    cfg = load_config()
    sec = cfg.setdefault("security", {})
    sec["auth_enabled"] = auth_enabled
    sec["session_lifetime_hours"] = lifetime
    sec["users"] = cleaned

    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg

    logger.info(
        "Настройки безопасности обновлены: auth_enabled=%s, пользователей=%d",
        auth_enabled, len(cleaned),
    )

    # Если текущий пользователь изменился — обновляем сессию
    current_username = getattr(g, "user", {}).get("username")
    for u in cleaned:
        if u["username"] == current_username:
            session["user"] = {"username": u["username"], "role": u["role"]}
            break

    return jsonify({
        "ok": True,
        "users_count": len(cleaned),
        "auth_enabled": auth_enabled,
    })


@app.route("/api/security/generate_password", methods=["POST"])
@auth_mod.admin_required
def api_security_generate_password():
    """Генерирует случайный пароль для нового пользователя."""
    import secrets
    return jsonify({"password": secrets.token_urlsafe(12)})


# ======================================================================
# Служебные
# ======================================================================
@app.route("/api/status")
def api_status():
    """Публичный — для health-check и обнаружения."""
    return jsonify({
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "author": __author__,
        },
    })


@app.route("/favicon.ico")
def favicon():
    try:
        return send_from_directory(
            RES_DIR / "static", "favicon.ico", mimetype="image/x-icon"
        )
    except Exception:
        return "", 404


# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
def _open_browser():
    webbrowser.open("http://127.0.0.1:8200")


if __name__ == "__main__":
    cfg = load_config()
    sec = cfg.get("security", {})
    auth_on = sec.get("auth_enabled", True)

    logger.info("Старт сервера: http://127.0.0.1:8200")
    logger.info("Конфиг: %s", CONFIG_PATH)
    if auth_on:
        users_count = len(sec.get("users", []))
        logger.info("Авторизация: включена (%d пользователей)", users_count)
    else:
        logger.warning("Авторизация: ВЫКЛЮЧЕНА — доступ открыт для всех")

    Timer(1.0, _open_browser).start()
    app.run(host="0.0.0.0", port=8200, debug=False, threaded=True)