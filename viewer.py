"""
VHS-Viewer — компактный веб-клиент для просмотра потоков VHS-Streamer.

Возможности:
    * Несколько серверов VHS-Streamer, загрузка потоков через публичный API.
    * Ручное добавление потоков, выбор и раскладка сетки.
    * Авторизация (сессии + login), управление пользователями (admin).
    * Страница событий (/events) с фильтрами и удалением (admin).
    * Страница аналитики (/analytics) со счётчиками по камерам.
    * Per-camera настройки аналитики (enabled/plates/objects/every/imgsz).
    * Страница реле (/relay) — список HTTP-реле с per-relay whitelist.
    * Страница уведомлений (/messaging) — Telegram / MAX / webhook.
    * Страница «Железо» (/hardware) — профиль, рекомендации, auto_tune.
    * Встроенная аналитика (autostart) — без отдельного run_analytics.py.
    * Страница ботов (/bots) — управление ботами для событий аналитики.
НЕ входит в проект:
    * Домофон, звонки в квартиры, кнопки «Открыть для звонящего» —
      это отдельный сервис VHS-Intercom (соседний репозиторий).
"""

import hmac
import json
import logging
import os
import re
from urllib.parse import urlsplit
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Timer

from dotenv import load_dotenv
load_dotenv()

import requests
import yaml

try:
    import psutil
except ImportError:  # мониторинг не должен ломать Viewer
    psutil = None
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import auth as auth_mod
from __version__ import (
    APP_FOOTER,
    APP_NAME,
    __author__,
    __copyright__,
    __license__,
    __url__,
    __version__,
)


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
LOGS_DIR = BASE_DIR / "logs"
EVENTS_JSONL = LOGS_DIR / "alpr_events.jsonl"
RELAY_LOG = LOGS_DIR / "relay.log"
MESSAGES_LOG = LOGS_DIR / "messages.log"


# ----------------------------------------------------------------------
# Логирование
# ----------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_console = logging.StreamHandler()
_console.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
_file = logging.FileHandler(LOG_DIR / "vhs-viewer.log", encoding="utf-8")
_file.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)

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
        "users": [],
    },
    "integration": {
        "ingest_key": "",
        "service_name": "viewer",
    },
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        if not isinstance(cfg.get("servers"), list):
            cfg["servers"] = []
        if not isinstance(cfg.get("streams"), list):
            cfg["streams"] = []
        if not isinstance(cfg.get("security"), dict):
            cfg["security"] = dict(DEFAULT_CONFIG["security"])
        if not isinstance(cfg.get("integration"), dict):
            cfg["integration"] = dict(DEFAULT_CONFIG["integration"])
        for srv in cfg.get("servers", []):
            if isinstance(srv, dict):
                srv["api_key"] = str(srv.get("api_key") or "").strip()
        _ingest_key = str(cfg["integration"].get("ingest_key") or "").strip()
        if _ingest_key:
            os.environ["VHS_SECURITY_INGEST_TOKEN"] = _ingest_key
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


def _server_key(server: dict) -> str:
    """Ключ Streamer для конкретного подключения."""
    return str((server or {}).get("api_key") or "").strip()


def _vhs_headers(key: str) -> dict:
    """Единый внутренний заголовок + совместимые алиасы."""
    key = (key or "").strip()
    if not key:
        return {}
    return {
        "X-VHS-Key": key,
        "X-VHS-Service": "viewer",
        "X-API-Token": key,
    }


def _get_streamer_streams(server: dict, timeout: float = 6.0) -> list[dict]:
    """Потоки одного Streamer: при ключе — integration API, иначе public."""
    url = _normalize_server_url((server or {}).get("url", ""))
    if not url:
        return []
    key = _server_key(server)
    headers = _vhs_headers(key)
    endpoints = ["/api/integration/streams", "/api/public/streams"] if key else ["/api/public/streams"]
    last_error = None
    for endpoint in endpoints:
        try:
            response = requests.get(url + endpoint, headers=headers, timeout=timeout)
            if response.status_code == 401 and endpoint.startswith("/api/integration"):
                last_error = "HTTP 401: неверный ключ Streamer"
                continue
            if not response.ok:
                last_error = f"HTTP {response.status_code}"
                continue
            data = response.json() or {}
            streams = data.get("streams") or []
            if isinstance(streams, list):
                return [s for s in streams if isinstance(s, dict)]
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "Streamer не вернул потоки")


def _normalize_plate(text: str) -> str:
    """Верхний регистр, только буквы/цифры. Та же логика, что в relay.py."""
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


# ----------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------

SESSION_SECRET_FILE = BASE_DIR / "logs" / ".session_secret"


def _load_secret_key() -> str:
    """Ключ сессий: VIEWER_SECRET из окружения или случайный файл."""
    from_env = os.environ.get("VIEWER_SECRET", "").strip()
    if from_env:
        return from_env
    try:
        if SESSION_SECRET_FILE.exists():
            value = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        value = __import__("secrets").token_urlsafe(48)
        SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_SECRET_FILE.write_text(value, encoding="utf-8")
        try:
            os.chmod(SESSION_SECRET_FILE, 0o600)
        except OSError:
            pass
        return value
    except OSError:
        logger.warning("Не удалось сохранить ключ сессий, использую временный")
        return __import__("secrets").token_urlsafe(48)


def _safe_next(target: str | None, fallback: str) -> str:
    """Разрешает редирект только на локальный путь (без open redirect)."""
    value = (target or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    return value

app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
)
app.secret_key = _load_secret_key()


# ----------------------------------------------------------------------
# CSRF-защита
# ----------------------------------------------------------------------
CSRF_HEADER = "X-CSRF-Token"
CSRF_EXEMPT = (
    re.compile(r"^/api/relay/[^/]+/open$"),
    re.compile(r"^/api/bots/[^/]+/config$"),
    # Внутренний PTZ: Security → Viewer, авторизация по X-VHS-Key.
    re.compile(r"^/api/integration/ptz/"),
)


def _csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = __import__("secrets").token_urlsafe(32)
        session["_csrf"] = token
    return token


def _csrf_exempt(path: str) -> bool:
    return any(pattern.match(path) for pattern in CSRF_EXEMPT)


@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return None
    if _csrf_exempt(request.path or ""):
        return None
    expected = session.get("_csrf")
    provided = request.headers.get(CSRF_HEADER) or request.form.get("_csrf", "")
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        if request.path.startswith("/api/") or "application/json" in request.headers.get(
            "Accept", ""
        ):
            return jsonify({"error": "csrf", "detail": "invalid csrf token"}), 403
        return "CSRF token missing or invalid", 403
    return None


@app.context_processor
def _csrf_context():
    return {"csrf_token": _csrf_token}


CSRF_SCRIPT = """<script>
(function () {
  var token = %r;
  if (!token) { return; }
  window.CSRF_TOKEN = token;
  var originalFetch = window.fetch;
  window.fetch = function (url, options) {
    options = options || {};
    var headers = new Headers(options.headers || {});
    if (!headers.has("X-CSRF-Token")) { headers.set("X-CSRF-Token", token); }
    options.headers = headers;
    return originalFetch(url, options);
  };
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form").forEach(function (form) {
      if (form.querySelector("input[name=_csrf]")) { return; }
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "_csrf";
      input.value = token;
      form.appendChild(input);
    });
  });
})();
</script>"""


@app.after_request
def _csrf_inject(response):
    if response.mimetype == "text/html" and response.status_code < 400:
        try:
            html = response.get_data(as_text=True)
            if "</body>" in html and "window.CSRF_TOKEN" not in html:
                # Токен нужен и при входе через SSO/прямой POST: до этого
                # session["_csrf"] мог не создаваться, и WHEP получал 403 csrf.
                token = _csrf_token()
                if token:
                    html = html.replace("</body>", CSRF_SCRIPT % token + "</body>", 1)
                    response.set_data(html)
        except RuntimeError:
            pass
    return response


_startup_config = load_config()
app.config["VIEWER_CONFIG"] = _startup_config
app.config.update(
    SESSION_COOKIE_NAME="vhs_viewer_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("VIEWER_HTTPS", "") == "1",
)
auth_mod.apply_session_lifetime(app, _startup_config)
from integration_api import integration_bp
app.register_blueprint(integration_bp)


# ======================================================================
# Аутентификация
# ======================================================================


_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_WINDOW = 600
_LOGIN_LIMIT = 10


def _login_rate_limited(key: str) -> bool:
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= _LOGIN_LIMIT


def _login_failed(key: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())

@app.route("/login", methods=["GET", "POST"])
def login():
    sec = _security_cfg()
    if not sec.get("auth_enabled", True):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        client = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
        client = client.split(",")[0].strip()
        if _login_rate_limited(client):
            error = "Слишком много попыток входа. Подождите 10 минут."
            return render_template("login.html", error=error, footer=APP_FOOTER)
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if auth_mod.do_login(u, p):
            _LOGIN_ATTEMPTS.pop(client, None)
            nxt = _safe_next(request.args.get("next"), url_for("index"))
            response = redirect(nxt)
            sec = _security_cfg()
            user = session.get("user") or {}
            return auth_mod.vhs_sso.set_cookie(
                response, (sec.get("sso_secret") or "").strip(),
                user.get("username", u), user.get("role", "admin"),
            )
        _login_failed(client)
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
    response = redirect(url_for("login"))
    return auth_mod.vhs_sso.clear_cookie(response)


def _security_cfg():
    cfg = load_config()
    return cfg.get("security", {}) or {}


# ======================================================================
# Главная
# ======================================================================
def _enrich_stream_urls(cfg: dict) -> dict:
    """Подставляет WebRTC URL из всех Streamer; конфиг на диске не меняет."""
    servers = [s for s in (cfg.get("servers") or []) if isinstance(s, dict) and s.get("url")]
    if not servers:
        base = (os.environ.get("VHS_STREAMER_URL") or "").strip().rstrip("/")
        key = (os.environ.get("VHS_STREAMER_API_TOKEN") or "").strip()
        if base and key:
            servers = [{"name": "env", "url": base, "api_key": key}]

    by_origin_path = {}
    by_path = {}
    for srv in servers:
        srv_origin = urlsplit(_normalize_server_url(srv.get("url", ""))).netloc
        try:
            streams = _get_streamer_streams(srv, timeout=4)
        except Exception:
            continue
        for item in streams:
            path = str(item.get("path") or "").rstrip("/")
            url = str(item.get("webrtc_url") or "")
            if path and url:
                by_origin_path[(srv_origin, path)] = url
                # запасной путь — только если путь уникален
                if path in by_path:
                    by_path[path] = None
                else:
                    by_path[path] = url

    for stream in cfg.get("streams", []):
        current = str(stream.get("url") or "")
        if not current:
            continue
        parsed = urlsplit(current)
        path = parsed.path.rstrip("/")
        replacement = by_origin_path.get((parsed.netloc, path))
        if replacement is None:
            replacement = by_path.get(path)
        if replacement:
            stream["url"] = replacement
    return cfg


@app.route("/")
@auth_mod.login_required
def index():
    cfg = _enrich_stream_urls(load_config())
    return render_template(
        "index.html",
        cfg=cfg,
        version=__version__,
        author=__author__,
        app_footer=APP_FOOTER,
        user=g.user,
    )


def _public_ptz_caps(force: bool = False) -> dict:
    """Возможности без служебного ключа Streamer."""
    result = {}
    for cam_id, info in _ptz_capabilities(force).items():
        item = {k: v for k, v in info.items() if k != "server_key"}
        result[str(cam_id)] = item
    return result


@app.get("/api/ptz/capabilities")
@auth_mod.login_required
def api_ptz_capabilities():
    force = request.args.get("refresh") in ("1", "true", "yes")
    return jsonify({"cameras": _public_ptz_caps(force)})


@app.post("/api/ptz/<camera_id>/<path:action>")
@auth_mod.admin_required
def api_ptz_command(camera_id: str, action: str):
    status, body = _ptz_request(camera_id, action, request.get_json(silent=True) or {})
    return jsonify(body), status


@app.get("/api/integration/ptz/capabilities")
def integration_ptz_capabilities():
    if not _integration_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"cameras": _public_ptz_caps()})


@app.post("/api/integration/ptz/<camera_id>/<path:action>")
def integration_ptz_command(camera_id: str, action: str):
    if not _integration_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    status, body = _ptz_request(camera_id, action, request.get_json(silent=True) or {})
    return jsonify(body), status


@app.route("/settings")
@auth_mod.admin_required
def settings_page():
    """Настройки Viewer живут в модальном окне главной; /settings — вход."""
    return redirect(url_for("index", settings=1))


_VIEWER_NET_STATE = {"ts": 0.0, "sent": 0, "recv": 0}

# PTZ: возможности кэшируются, чтобы не дёргать ONVIF на каждый рендер.
_PTZ_CACHE = {"ts": 0.0, "data": {}}
_PTZ_TTL = 60.0


def _ptz_servers() -> list[dict]:
    cfg = load_config()
    servers = [s for s in (cfg.get("servers") or []) if isinstance(s, dict) and s.get("url")]
    if not servers:
        base = (os.environ.get("VHS_STREAMER_URL") or "").strip().rstrip("/")
        key = (os.environ.get("VHS_STREAMER_API_TOKEN") or "").strip()
        if base and key:
            servers = [{"name": "env", "url": base, "api_key": key}]
    return servers


def _ptz_capabilities(force: bool = False) -> dict:
    now = time.time()
    if not force and now - _PTZ_CACHE["ts"] < _PTZ_TTL:
        return _PTZ_CACHE["data"]
    result = {}
    for srv in _ptz_servers():
        url = _normalize_server_url(srv.get("url", ""))
        if not url:
            continue
        try:
            response = requests.get(
                url + "/api/ptz/capabilities",
                headers=_vhs_headers(_server_key(srv)),
                timeout=5,
            )
            if not response.ok:
                continue
            cameras = (response.json() or {}).get("cameras") or {}
        except (requests.RequestException, ValueError):
            continue
        for cam_id, info in cameras.items():
            if not isinstance(info, dict) or not info.get("has_ptz"):
                continue
            item = dict(info)
            item["camera_id"] = str(cam_id)
            item["server"] = srv.get("name") or url
            item["server_url"] = url
            item["server_key"] = _server_key(srv)
            result.setdefault(str(cam_id), item)
    _PTZ_CACHE["data"] = result
    # Пустой ответ не кэшируем надолго: Streamer мог быть ещё не готов.
    # Иначе после перезапуска/смены раскладки PTZ пропадает на минуту.
    _PTZ_CACHE["ts"] = now if result else now - _PTZ_TTL + 10
    return result


def _ptz_request(camera_id: str, action: str, payload: dict | None = None):
    caps = _ptz_capabilities()
    cap = caps.get(str(camera_id))
    if not cap:
        return 404, {"ok": False, "error": "PTZ для камеры недоступен"}
    action = str(action or "").strip().strip("/")
    if action not in ("move", "stop", "home", "invert", "preset/goto", "preset/set", "preset/remove", "info"):
        return 400, {"ok": False, "error": "неизвестное PTZ-действие"}
    payload = dict(payload or {})
    if action != "info":
        payload.setdefault("profile_token", cap.get("profile_token"))
    url = f"{cap['server_url']}/api/cameras/{camera_id}/ptz/{action}"
    try:
        headers = _vhs_headers(str(cap.get("server_key") or ""))
        if action == "info":
            response = requests.get(url, headers=headers, timeout=10)
        else:
            headers["Content-Type"] = "application/json"
            response = requests.post(url, headers=headers, json=payload or {}, timeout=10)
        try:
            body = response.json()
        except ValueError:
            body = {"ok": response.ok, "text": response.text[:300]}
        if action == "invert":
            _PTZ_CACHE["ts"] = 0
        return response.status_code, body
    except requests.RequestException as exc:
        return 502, {"ok": False, "error": str(exc)}


@app.get("/api/system/stats")
@auth_mod.login_required
def api_system_stats():
    """CPU/RAM/диски/сеть для мониторинга в подвале Viewer."""
    if psutil is None:
        return jsonify({"ok": False, "error": "psutil не установлен"}), 200
    try:
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        ram = {
            "percent": round(vm.percent, 1),
            "used_mb": round((vm.total - vm.available) / 1024 / 1024, 1),
            "total_mb": round(vm.total / 1024 / 1024, 1),
        }
        disks = []
        seen = set()
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in (p.opts or "").lower() or p.mountpoint in seen:
                continue
            seen.add(p.mountpoint)
            try:
                u = psutil.disk_usage(p.mountpoint)
            except (PermissionError, OSError):
                continue
            label = (p.device or p.mountpoint).rstrip("\\/")
            if label.endswith(":"):
                label = label[:-1]
            disks.append({
                "label": label,
                "mount": p.mountpoint,
                "percent": round(u.percent, 1),
            })

        counters = psutil.net_io_counters()
        now = time.time()
        sent = int(counters.bytes_sent or 0)
        recv = int(counters.bytes_recv or 0)
        elapsed = now - float(_VIEWER_NET_STATE.get("ts") or 0.0)
        sent_mbps = recv_mbps = None
        if elapsed > 0.05 and _VIEWER_NET_STATE.get("ts"):
            sent_mbps = round((sent - _VIEWER_NET_STATE["sent"]) * 8 / elapsed / 1_000_000, 2)
            recv_mbps = round((recv - _VIEWER_NET_STATE["recv"]) * 8 / elapsed / 1_000_000, 2)
        _VIEWER_NET_STATE.update({"ts": now, "sent": sent, "recv": recv})

        return jsonify({
            "ok": True,
            "cpu": {"percent": round(cpu, 1)},
            "ram": ram,
            "disks": disks,
            "network": {"sent_mbps": sent_mbps, "recv_mbps": recv_mbps},
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200


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
# Первый запуск — мастер настройки
# ======================================================================
SETUP_FLAG = "_setup_complete"


def _is_setup_complete() -> bool:
    """True, если viewer_config.yaml существует и содержит _setup_complete: true."""
    if not CONFIG_PATH.exists():
        return False
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return bool(cfg.get(SETUP_FLAG))
    except Exception:
        return False


def _reset_logs_on_setup():
    """
    Очищает журналы событий/реле/уведомлений и снапшоты.
    vhs-viewer.log НЕ трогает — он открыт текущим процессом (WinError 32).
    """
    cleared = []
    for name in ("alpr_events.jsonl", "relay.log", "messages.log"):
        p = LOGS_DIR / name
        if p.exists():
            try:
                p.write_text("", encoding="utf-8")
                cleared.append(name)
            except Exception:
                logger.warning("setup: не удалось очистить %s", name)

    snaps_dir = LOGS_DIR / "snapshots"
    if snaps_dir.exists():
        try:
            for f in snaps_dir.glob("*.jpg"):
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    logger.info("setup: очищены журналы: %s", cleared or "нет файлов")


def _complete_setup(
    streamer_url: str,
    username: str,
    password: str,
    clear_logs: bool,
    streamer_key: str = "",
) -> None:
    """Создаёт свежий viewer_config.yaml с флагом _setup_complete."""
    # 1. Бэкап существующего конфига (если есть)
    if CONFIG_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_PATH.with_name(f"viewer_config.yaml.bak.{ts}")
        try:
            CONFIG_PATH.replace(backup)
            logger.info("setup: старый конфиг сохранён как %s", backup.name)
        except Exception:
            logger.exception("setup: не удалось сделать бэкап конфига")

    # 2. Свежий конфиг
    url = streamer_url.rstrip("/")
    cfg = {
        SETUP_FLAG: True,
        "title": "VHS-Viewer",
        "layout": "2x2",
        "servers": [{"name": "local", "url": url, "api_key": (streamer_key or "").strip()}],
        "streams": [],
        "integration": {
            "service_name": "viewer",
            "ingest_key": __import__("secrets").token_urlsafe(32),
        },
        "security": {
            "auth_enabled": True,
            "session_lifetime_hours": 24,
            "users": [
                {
                    "username": username,
                    "password": auth_mod.hash_password(password),
                    "role": "admin",
                },
            ],
        },
        "analytics": {
            "enabled": True,
            "autostart": True,
            "auto_tune": True,
            "streamer_url": url,
            "rtsp_base": None,
            "poll_interval": 15,
            "conf_threshold": 0.5,
            "object_model": "yolov8n.pt",
            "object_conf": 0.4,
            "object_imgsz": 640,
            "analyze_every_n": 1,
            "intra_op_num_threads": None,
            "cameras": {
                "default": {
                    "enabled": False,
                    "plates": False,
                    "objects": [],
                },
            },
        },
        "relay": {"relays": []},
        "messaging": {"channels": []},
    }

    # 3. Сохраняем
    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg
    try:
        auth_mod.apply_session_lifetime(app, cfg)
    except Exception:
        pass

    # 4. Логи — опционально
    if clear_logs:
        _reset_logs_on_setup()




@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    return response

@app.before_request
def _ensure_setup():
    """Пока setup не завершён — все запросы ведут на /setup."""
    if _is_setup_complete():
        return None

    path = request.path or ""
    allowed = ("/setup", "/static/", "/favicon.ico")
    if any(path.startswith(p) for p in allowed):
        return None

    if path.startswith("/api/"):
        return jsonify(
            {
                "error": "setup required",
                "setup_url": "/setup",
            }
        ), 503

    return redirect(url_for("setup_page"))


@app.route("/setup", methods=["GET", "POST"])
def setup_page():
    if _is_setup_complete():
        return redirect(url_for("index"))

    error = None
    warning = None

    # Предупреждение, если конфиг уже был (пришёл из проекта / старой версии)
    if CONFIG_PATH.exists() and request.method == "GET":
        warning = (
            "Найден существующий viewer_config.yaml. "
            "Он будет сохранён в бэкап как "
            "viewer_config.yaml.bak.<время> и заменён новым."
        )

    if request.method == "POST":
        streamer_url = (request.form.get("streamer_url") or "").strip()
        streamer_key = (request.form.get("streamer_key") or "").strip()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""
        clear_logs = request.form.get("clear_logs") == "on"

        errors = []
        if not streamer_url:
            errors.append("URL Streamer обязателен")
        elif not streamer_url.startswith(("http://", "https://")):
            errors.append("URL должен начинаться с http:// или https://")

        if not username:
            errors.append("Логин администратора обязателен")
        elif len(username) < 3:
            errors.append("Логин: минимум 3 символа")

        if not password:
            errors.append("Пароль обязателен")
        elif len(password) < 10:
            errors.append("Пароль: минимум 10 символов")
        elif password != password2:
            errors.append("Пароли не совпадают")
        elif password.lower() in ("admin", "viewer", "password", "123456", "change-me"):
            errors.append("Пароль слишком простой, выберите другой")
        elif not (re.search(r"[^\W\d_]", password) and re.search(r"\d", password)):
            errors.append("Пароль: минимум 10 символов, буквы и цифры")

        if errors:
            error = "; ".join(errors)
        else:
            try:
                _complete_setup(streamer_url, username, password, clear_logs, streamer_key)
                logger.info("setup: завершён, admin=%s", username)

                # Автологин
                session["user"] = {"username": username, "role": "admin"}
                session.permanent = True

                # Запускаем аналитику динамически (при старте она пропускалась)
                try:
                    _start_embedded_analytics()
                except Exception:
                    logger.exception("setup: не удалось запустить аналитику")

                return redirect(url_for("index"))
            except Exception as e:
                logger.exception("setup: ошибка сохранения")
                error = f"Не удалось сохранить конфигурацию: {e}"

    return render_template(
        "setup.html",
        error=error,
        warning=warning,
        default_streamer="http://127.0.0.1:8100",
        default_username="admin",
        app_footer=APP_FOOTER,
        version=__version__,
    )


# ======================================================================
# Hardware: профиль железа и рекомендации
# ======================================================================
@app.route("/hardware")
@auth_mod.admin_required
def hardware_page():
    return render_template(
        "hardware.html",
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


@app.route("/api/hardware")
@auth_mod.admin_required
def api_hardware():
    try:
        from vhs_analytics.hardware import detect_hardware

        hw = detect_hardware()
        cfg = load_config()
        an = cfg.get("analytics") or {}
        data = hw.to_dict()
        data["auto_tune"] = bool(an.get("auto_tune", True))
        data["intra_op_num_threads"] = an.get("intra_op_num_threads")
        data["analyze_every_n"] = an.get("analyze_every_n")
        data["object_imgsz"] = an.get("object_imgsz")
        data["objects_per_camera"] = an.get("objects_per_camera")
        return jsonify(data)
    except Exception as e:
        logger.exception("hardware detect failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/hardware/auto_tune", methods=["POST"])
@auth_mod.admin_required
def api_hardware_auto_tune():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))

    cfg = load_config()
    an = cfg.get("analytics") or {}
    an["auto_tune"] = enabled
    cfg["analytics"] = an
    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg

    logger.info("auto_tune переключён в %s (применится после перезапуска)", enabled)
    return jsonify({"ok": True, "auto_tune": enabled})


# ======================================================================
# События
# ======================================================================
@app.route("/events")
@auth_mod.login_required
def events_page():
    return render_template(
        "events.html",
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


def _read_recent_events(
    limit: int,
    categories: list[str] | None = None,
    camera_id: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Последние N событий из JSONL с фильтрацией."""
    if not EVENTS_JSONL.exists():
        return []

    cat_set = {c.lower() for c in (categories or []) if c}
    cam = (camera_id or "").strip() or None
    needle = (search or "").strip().lower() or None

    try:
        with open(EVENTS_JSONL, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            chunk = min(size, 4 * 1024 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
            lines = data.split("\n")
            if chunk < size and lines:
                lines = lines[1:]
            lines = [ln for ln in lines if ln.strip()]

            out: list[dict] = []
            for ln in reversed(lines):
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue

                if cat_set:
                    cat = str(e.get("category") or "").lower()
                    if not cat:
                        cat = "plate" if e.get("plate") else "object"
                    if cat not in cat_set:
                        continue

                if cam is not None and e.get("camera_id") != cam:
                    continue

                if needle is not None:
                    plate_s = str(e.get("plate") or "").lower()
                    label_s = str(e.get("label") or "").lower()
                    label_ru_s = str(e.get("label_ru") or "").lower()
                    if needle not in plate_s and needle not in label_s and needle not in label_ru_s:
                        continue

                out.append(e)
                if len(out) >= limit:
                    break

            out.reverse()
            return out
    except Exception:
        logger.exception("не удалось прочитать alpr_events.jsonl")
        return []


def _integration_token_ok() -> bool:
    """Единый ключ Viewer для Security: X-VHS-Key (совместим с X-Ingest-Token)."""
    cfg = load_config()
    expected = str((cfg.get("integration") or {}).get("ingest_key") or "").strip()
    if not expected:
        expected = (os.environ.get("VHS_SECURITY_INGEST_TOKEN") or "").strip()
    provided = (
        request.headers.get("X-VHS-Key")
        or request.headers.get("X-Ingest-Token")
        or ""
    ).strip()
    return bool(expected) and hmac.compare_digest(expected, provided)


@app.get("/api/integration/state")
def integration_state():
    """Единая точка для VHS-Security: камеры, потоки и события Viewer."""
    if not _integration_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    cameras = {}
    cfg = load_config()
    servers = [s for s in (cfg.get("servers") or []) if isinstance(s, dict) and s.get("url")]
    if not servers:
        base = (os.environ.get("VHS_STREAMER_URL") or "").strip().rstrip("/")
        key = (os.environ.get("VHS_STREAMER_API_TOKEN") or "").strip()
        if base and key:
            servers = [{"name": "env", "url": base, "api_key": key}]

    for srv in servers:
        srv_name = str(srv.get("name") or srv.get("url") or "streamer")
        try:
            streams = _get_streamer_streams(srv, timeout=6)
        except Exception as exc:
            logger.warning("integration/state: %s недоступен: %s", srv_name, exc)
            continue
        for stream in streams:
            cid = str(stream.get("camera_id") or stream.get("id") or "")
            if not cid:
                continue
            item = cameras.setdefault(cid, {
                "id": cid,
                "name": str(stream.get("camera_name") or cid),
                "source": srv_name,
                "streams": [],
            })
            item["streams"].append({
                "id": stream.get("id"), "path": stream.get("path"),
                "url": stream.get("webrtc_url", ""),
            })
    if not cameras:
        # Резерв: потоки из конфигурации Viewer.
        for stream in load_config().get("streams", []):
            url = str(stream.get("url") or "")
            path = urlsplit(url).path.rstrip("/")
            sid = path.rsplit("/", 1)[-1] if path else str(stream.get("name") or "camera")
            cameras[sid] = {
                "id": sid, "name": str(stream.get("name") or sid),
                "streams": [{"id": sid, "path": path, "url": url}],
            }
    events = _read_recent_events(limit=1000)
    stream_to_camera = {}
    for camera in cameras.values():
        for stream in camera.get("streams", []):
            if stream.get("id"):
                stream_to_camera[str(stream["id"])] = camera["id"]
    for event in events:
        stream_id = str(event.get("camera_id") or "")
        if stream_id in stream_to_camera:
            event["camera_id"] = stream_to_camera[stream_id]
            event["camera_name"] = next(
                (c["name"] for c in cameras.values() if c["id"] == stream_to_camera[stream_id]),
                event.get("camera_name") or stream_id,
            )
    return jsonify({"cameras": list(cameras.values()), "events": events})


@app.get("/api/integration/events")
def integration_events():
    if not _integration_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"events": _read_recent_events(limit=1000)})


@app.route("/api/events")
@auth_mod.login_required
def api_events():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))

    raw_cats = request.args.getlist("category")
    categories: list[str] = []
    for item in raw_cats:
        for c in item.split(","):
            c = c.strip()
            if c:
                categories.append(c)

    camera_id = request.args.get("camera_id") or None
    search = request.args.get("search") or None

    events = _read_recent_events(
        limit=limit,
        categories=categories or None,
        camera_id=camera_id,
        search=search,
    )
    return jsonify(
        {
            "events": events,
            "count": len(events),
            "filters": {
                "categories": categories,
                "camera_id": camera_id,
                "search": search,
                "limit": limit,
            },
        }
    )


@app.route("/api/events/cameras")
@auth_mod.login_required
def api_events_cameras():
    events = _read_recent_events(limit=1000)
    seen: dict[str, str] = {}
    for e in events:
        cid = e.get("camera_id")
        if cid and cid not in seen:
            seen[cid] = e.get("camera_name") or cid
    return jsonify(
        {
            "cameras": [
                {"id": cid, "name": name}
                for cid, name in sorted(seen.items(), key=lambda x: x[1].lower())
            ],
        }
    )


# ======================================================================
# Аналитика (страница + summary)
# ======================================================================
@app.route("/analytics")
@auth_mod.login_required
def analytics_page():
    cfg = load_config()
    return render_template(
        "analytics.html",
        cfg=cfg,
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


@app.route("/api/analytics/summary")
@auth_mod.login_required
def api_analytics_summary():
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    events = _read_recent_events(limit=1000)

    cameras: dict[str, dict] = {}
    scanned = 0

    for e in events:
        iso = str(e.get("iso") or "")
        if not iso.startswith(today_prefix):
            continue
        scanned += 1

        cid = e.get("camera_id") or "unknown"
        cat = str(e.get("category") or "").lower()
        if not cat:
            cat = "plate" if e.get("plate") else "object"

        entry = cameras.setdefault(
            cid,
            {
                "camera_id": cid,
                "camera_name": e.get("camera_name") or cid,
                "total": 0,
                "by_category": {},
                "last_event": None,
            },
        )
        entry["total"] += 1
        entry["by_category"][cat] = entry["by_category"].get(cat, 0) + 1

        prev = entry["last_event"]
        e_ts = e.get("ts") or 0
        if prev is None or e_ts >= (prev.get("ts") or 0):
            entry["last_event"] = {
                "ts": e_ts,
                "iso": iso,
                "category": cat,
                "label": e.get("label") or e.get("plate") or "?",
                "label_ru": e.get("label_ru") or "",
                "confidence": e.get("confidence"),
                "snapshot": e.get("snapshot"),
            }

    return jsonify(
        {
            "cameras": cameras,
            "date": today_prefix,
            "events_scanned": scanned,
        }
    )


# ======================================================================
# Реле: список, CRUD, whitelist, тест, лог (только admin)
# ======================================================================
def _load_root_yaml_section(section: str) -> dict:
    """Читает секцию верхнего уровня из YAML."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        r = raw.get(section)
        return r if isinstance(r, dict) else {}
    except Exception:
        logger.exception("не удалось прочитать секцию '%s'", section)
        return {}


def _relay_normalize_cfg(raw: dict) -> dict:
    """
    Приводит секцию relay к формату {"relays": [...]} — backward compat
    со старым плоским форматом (url_open + whitelist без relays[]).
    """
    if not isinstance(raw, dict):
        raw = {}

    relays_raw = raw.get("relays")
    if isinstance(relays_raw, list):
        cleaned = [r for r in relays_raw if isinstance(r, dict) and r.get("id")]
        return {"relays": cleaned}

    if raw.get("url_open") or raw.get("whitelist") or raw.get("enabled"):
        old = dict(raw)
        old["id"] = "default"
        old.setdefault("name", "Реле по умолчанию")
        old.setdefault("cameras", [])
        return {"relays": [old]}

    return {"relays": []}


def _get_relay_cfg() -> dict:
    raw = _load_root_yaml_section("relay")
    return _relay_normalize_cfg(raw)


def _save_relay_cfg(relay_cfg: dict):
    """Сохраняет relay в YAML и применяет к живому RelayManager."""
    cfg = load_config()
    cfg["relay"] = relay_cfg
    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg

    mgr = globals().get("_analytics_manager")
    if mgr is None:
        logger.info("RelayManager: аналитика не запущена, изменения только в YAML")
        return

    live = getattr(mgr, "_relay", None)
    if live is not None:
        try:
            live.reload_from_cfg(relay_cfg)
            logger.info("RelayManager перезагружен из UI")
        except Exception:
            logger.exception("не удалось перезагрузить RelayManager")
        return

    try:
        from vhs_analytics.relay import RelayManager

        live = RelayManager(relay_cfg, log_dir=LOGS_DIR)
        mgr._relay = live
        logger.info("RelayManager создан лениво из UI: реле=%d", len(live.relays))
    except Exception:
        logger.exception("не удалось создать RelayManager лениво")


def _get_live_relay_manager():
    mgr = globals().get("_analytics_manager")
    return getattr(mgr, "_relay", None) if mgr else None


def _relay_public(r: dict) -> dict:
    """Копия реле без пароля."""
    out = dict(r)
    out.pop("password", None)
    return out


@app.route("/relay")
@auth_mod.admin_required
def relay_page():
    return render_template(
        "relay.html",
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


@app.route("/api/relay/list")
@auth_mod.admin_required
def api_relay_list():
    rc = _get_relay_cfg()
    return jsonify(
        {
            "relays": [_relay_public(r) for r in rc.get("relays", [])],
        }
    )


@app.route("/api/relay/<relay_id>", methods=["GET"])
@auth_mod.admin_required
def api_relay_get(relay_id: str):
    rc = _get_relay_cfg()
    for r in rc["relays"]:
        if r.get("id") == relay_id:
            return jsonify(_relay_public(r))
    return jsonify({"error": "реле не найдено"}), 404


@app.route("/api/relay/<relay_id>", methods=["POST"])
@auth_mod.admin_required
def api_relay_save(relay_id: str):
    data = request.get_json(silent=True) or {}

    rid = str(relay_id or "").strip()
    if not re.match(r"^[a-zA-Z0-9_\-]{1,50}$", rid):
        return jsonify({"error": "id: только буквы, цифры, _ и - (до 50)"}), 400

    rc = _get_relay_cfg()
    existing = None
    for r in rc["relays"]:
        if r.get("id") == rid:
            existing = r
            break

    def _clamp_int(val, lo, hi, default):
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return default

    def _clamp_float(val, lo, hi, default):
        try:
            return max(lo, min(hi, float(val)))
        except (TypeError, ValueError):
            return default

    url_open = str(data.get("url_open", existing.get("url_open") if existing else "") or "").strip()
    enabled = bool(data.get("enabled", existing.get("enabled", False) if existing else False))
    if enabled and not url_open:
        return jsonify({"error": "url_open обязателен, если реле включено"}), 400

    new_entry: dict = dict(existing or {})
    new_entry["id"] = rid
    new_entry["name"] = str(data.get("name") or (existing or {}).get("name") or rid).strip()
    new_entry["enabled"] = enabled
    new_entry["url_open"] = url_open
    new_entry["url_close"] = str(
        data.get("url_close", existing.get("url_close") if existing else "") or ""
    ).strip()
    m = str(data.get("method", existing.get("method") if existing else "GET") or "GET").upper()
    new_entry["method"] = m if m in ("GET", "POST") else "GET"
    new_entry["timeout"] = _clamp_float(
        data.get("timeout", existing.get("timeout", 5.0) if existing else 5.0), 0.5, 60.0, 5.0
    )
    new_entry["open_duration_sec"] = _clamp_int(
        data.get("open_duration_sec", existing.get("open_duration_sec", 5) if existing else 5),
        1,
        3600,
        5,
    )
    new_entry["cooldown_sec"] = _clamp_int(
        data.get("cooldown_sec", existing.get("cooldown_sec", 30) if existing else 30), 0, 86400, 30
    )

    if "username" in data:
        u = str(data.get("username") or "").strip()
        if u:
            new_entry["username"] = u
        else:
            new_entry.pop("username", None)
            new_entry.pop("password", None)

    if "password" in data and data.get("password"):
        new_entry["password"] = str(data["password"])

    cams = data.get("cameras")
    if cams is not None:
        if isinstance(cams, list):
            new_entry["cameras"] = [str(c).strip() for c in cams if str(c).strip()]
        else:
            new_entry["cameras"] = []

    if "whitelist" in data and isinstance(data.get("whitelist"), list):
        new_entry["whitelist"] = [str(p).strip() for p in data["whitelist"] if str(p).strip()]
    elif "whitelist" not in new_entry:
        new_entry["whitelist"] = []

    if existing is None:
        rc["relays"].append(new_entry)
    else:
        idx = rc["relays"].index(existing)
        rc["relays"][idx] = new_entry

    _save_relay_cfg(rc)
    logger.info("relay: сохранено реле '%s' (enabled=%s)", rid, enabled)
    return jsonify({"ok": True, "relay": _relay_public(new_entry)})


@app.route("/api/relay/<relay_id>", methods=["DELETE"])
@auth_mod.admin_required
def api_relay_delete(relay_id: str):
    rc = _get_relay_cfg()
    before = len(rc["relays"])
    rc["relays"] = [r for r in rc["relays"] if r.get("id") != relay_id]
    if len(rc["relays"]) == before:
        return jsonify({"error": "реле не найдено"}), 404
    _save_relay_cfg(rc)
    logger.info("relay: удалено реле '%s'", relay_id)
    return jsonify({"ok": True})


@app.route("/api/relay/<relay_id>/test", methods=["POST"])
@auth_mod.admin_required
def api_relay_test(relay_id: str):
    live = _get_live_relay_manager()
    if live is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "RelayManager не активен. Возможные причины: "
                    "1) analytics.autostart=false в YAML; "
                    "2) реле добавлено через UI уже после старта, "
                    "перезапустите viewer.py."
                ),
            }
        ), 200
    res = live.test_relay(relay_id)
    return jsonify(res), 200

# ======================================================================
# Bot API: открытие реле для мессенджера MAX
# Авторизация — по заголовку X-Relay-Token, без сессии (бот не умеет
# логиниться через форму). Секрет берётся из переменной окружения
# RELAY_SECRET — та же, что бот передаёт в заголовке.
# ======================================================================
@app.route("/api/relay/<relay_id>/open", methods=["POST"])
def api_relay_open_bot(relay_id: str):
    expected = os.environ.get("RELAY_SECRET", "")
    provided = request.headers.get("X-Relay-Token", "")
    if not expected or provided != expected:
        logger.warning(
            "bot: отклонён запрос открытия реле '%s' (неверный или пустой токен)",
            relay_id,
        )
        return jsonify({"error": "unauthorized"}), 401

    live = _get_live_relay_manager()
    if live is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "RelayManager не активен. Проверьте: "
                    "1) setup завершён; "
                    "2) analytics.autostart=true; "
                    "3) реле включено (enabled=true)."
                ),
            }
        ), 503

    # RelayManager может иметь метод open(); если нет — используем test_relay.
    opener = getattr(live, "open", None) or getattr(live, "test_relay")
    try:
        res = opener(relay_id)
    except Exception as e:
        logger.exception("bot: ошибка открытия реле '%s'", relay_id)
        return jsonify({"ok": False, "error": str(e)}), 500

    logger.info("bot: открытие реле '%s' → %s", relay_id, res)
    return jsonify(res), 200
# ======================================================================

@app.route("/api/relay/<relay_id>/whitelist/add", methods=["POST"])
@auth_mod.admin_required
def api_relay_wl_add(relay_id: str):
    data = request.get_json(silent=True) or {}
    plate = str(data.get("plate") or "").strip()
    if not _normalize_plate(plate):
        return jsonify({"error": "пустой номер"}), 400

    rc = _get_relay_cfg()
    target = None
    for r in rc["relays"]:
        if r.get("id") == relay_id:
            target = r
            break
    if target is None:
        return jsonify({"error": "реле не найдено"}), 404

    wl = list(target.get("whitelist") or [])
    existing_norm = {_normalize_plate(p) for p in wl}
    if _normalize_plate(plate) in existing_norm:
        return jsonify({"ok": True, "whitelist": wl, "note": "уже в списке"})

    wl.append(plate)
    target["whitelist"] = wl
    _save_relay_cfg(rc)
    logger.info("relay[%s]: whitelist + %s", relay_id, plate)
    return jsonify({"ok": True, "whitelist": wl})


@app.route("/api/relay/<relay_id>/whitelist/remove", methods=["POST"])
@auth_mod.admin_required
def api_relay_wl_remove(relay_id: str):
    data = request.get_json(silent=True) or {}
    plate = str(data.get("plate") or "").strip()
    if not _normalize_plate(plate):
        return jsonify({"error": "пустой номер"}), 400

    rc = _get_relay_cfg()
    target = None
    for r in rc["relays"]:
        if r.get("id") == relay_id:
            target = r
            break
    if target is None:
        return jsonify({"error": "реле не найдено"}), 404

    wl = list(target.get("whitelist") or [])
    new_wl = [p for p in wl if _normalize_plate(p) != _normalize_plate(plate)]
    if len(new_wl) == len(wl):
        return jsonify({"error": "номера нет в списке"}), 404
    target["whitelist"] = new_wl
    _save_relay_cfg(rc)
    logger.info("relay[%s]: whitelist - %s", relay_id, plate)
    return jsonify({"ok": True, "whitelist": new_wl})


@app.route("/api/relay/log")
@auth_mod.admin_required
def api_relay_log():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))

    if not RELAY_LOG.exists():
        return jsonify({"lines": []})
    try:
        with open(RELAY_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 512 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
            lines = [ln for ln in data.split("\n") if ln.strip()]
            if chunk < size and lines:
                lines = lines[1:]
            return jsonify({"lines": lines[-limit:]})
    except Exception:
        logger.exception("не удалось прочитать relay.log")
        return jsonify({"lines": []})


# ======================================================================
# Messaging: каналы уведомлений (Telegram / MAX / webhook), только admin
# ======================================================================
def _get_messaging_cfg() -> dict:
    raw = _load_root_yaml_section("messaging")
    channels = raw.get("channels")
    if not isinstance(channels, list):
        return {"channels": []}
    cleaned = [c for c in channels if isinstance(c, dict) and c.get("id")]
    return {"channels": cleaned}


def _save_messaging_cfg(messaging_cfg: dict):
    """Сохраняет messaging в YAML и применяет к живому MessageManager."""
    cfg = load_config()
    cfg["messaging"] = messaging_cfg
    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg

    mgr = globals().get("_analytics_manager")
    if mgr is None:
        logger.info("MessageManager: аналитика не запущена, изменения только в YAML")
        return

    live = getattr(mgr, "_messaging", None)
    if live is not None:
        try:
            live.reload_from_cfg(messaging_cfg)
            logger.info("MessageManager перезагружен из UI")
        except Exception:
            logger.exception("не удалось перезагрузить MessageManager")
        return

    try:
        from vhs_analytics.messaging import MessageManager

        live = MessageManager(messaging_cfg, log_dir=LOGS_DIR)
        mgr._messaging = live
        logger.info(
            "MessageManager создан лениво из UI: каналов=%d",
            len(live.channels),
        )
    except Exception:
        logger.exception("не удалось создать MessageManager лениво")


def _get_live_messaging_manager():
    mgr = globals().get("_analytics_manager")
    return getattr(mgr, "_messaging", None) if mgr else None


def _messaging_public(ch: dict) -> dict:
    """Возвращает копию канала без секретов (token, max_token)."""
    out = dict(ch)
    out.pop("token", None)
    out.pop("max_token", None)
    return out


def _messaging_channel_default() -> dict:
    return {
        "id": "",
        "name": "",
        "type": "telegram",
        "enabled": False,
        "token": "",
        "chat_id": "",
        "url": "",
        "method": "POST",
        "timeout": 10.0,
        "max_token": "",
        "max_recipient_id": "",
        "max_recipient_type": "user",
        "categories": [],
        "cameras": [],
        "min_confidence": 0.0,
        "template": "",
        "attach_snapshot": True,
        "cooldown_sec": 0,
    }


@app.route("/messaging")
@auth_mod.admin_required
def messaging_page():
    return render_template(
        "messaging.html",
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


@app.route("/api/messaging/list")
@auth_mod.admin_required
def api_messaging_list():
    mc = _get_messaging_cfg()
    return jsonify(
        {
            "channels": [_messaging_public(c) for c in mc.get("channels", [])],
        }
    )


@app.route("/api/messaging/<channel_id>", methods=["GET"])
@auth_mod.admin_required
def api_messaging_get(channel_id: str):
    mc = _get_messaging_cfg()
    for c in mc["channels"]:
        if c.get("id") == channel_id:
            return jsonify(_messaging_public(c))
    return jsonify({"error": "канал не найден"}), 404


@app.route("/api/messaging/<channel_id>", methods=["POST"])
@auth_mod.admin_required
def api_messaging_save(channel_id: str):
    data = request.get_json(silent=True) or {}

    cid = str(channel_id or "").strip()
    if not re.match(r"^[a-zA-Z0-9_\-]{1,50}$", cid):
        return jsonify({"error": "id: только латиница, цифры, _ и - (до 50)"}), 400

    mc = _get_messaging_cfg()
    existing = None
    for c in mc["channels"]:
        if c.get("id") == cid:
            existing = c
            break

    def _clamp_int(val, lo, hi, default):
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return default

    def _clamp_float(val, lo, hi, default):
        try:
            return max(lo, min(hi, float(val)))
        except (TypeError, ValueError):
            return default

    base = existing or _messaging_channel_default()
    entry: dict = dict(base)

    entry["id"] = cid
    entry["name"] = str(data.get("name") or base.get("name") or cid).strip()
    ch_type = str(data.get("type") or base.get("type") or "telegram").lower()
    if ch_type not in ("telegram", "max", "webhook"):
        ch_type = "telegram"
    entry["type"] = ch_type
    entry["enabled"] = bool(data.get("enabled", base.get("enabled", False)))

    if "token" in data and data.get("token"):
        entry["token"] = str(data["token"]).strip()
    elif "token" not in entry:
        entry["token"] = ""

    entry["chat_id"] = str(data.get("chat_id", entry.get("chat_id", "")) or "").strip()
    entry["url"] = str(data.get("url", entry.get("url", "")) or "").strip()

    m = str(data.get("method", entry.get("method", "POST")) or "POST").upper()
    entry["method"] = m if m in ("GET", "POST") else "POST"
    entry["timeout"] = _clamp_float(
        data.get("timeout", entry.get("timeout", 10.0)), 1.0, 120.0, 10.0
    )

    if "max_token" in data and data.get("max_token"):
        entry["max_token"] = str(data["max_token"]).strip()
    elif "max_token" not in entry:
        entry["max_token"] = ""

    entry["max_recipient_id"] = str(
        data.get("max_recipient_id", entry.get("max_recipient_id", "")) or ""
    ).strip()

    rt = str(
        data.get("max_recipient_type", entry.get("max_recipient_type", "user")) or "user"
    ).lower()
    entry["max_recipient_type"] = rt if rt in ("user", "chat") else "user"

    if "categories" in data and isinstance(data.get("categories"), list):
        entry["categories"] = [str(x).strip().lower() for x in data["categories"] if str(x).strip()]
    elif "categories" not in entry:
        entry["categories"] = []

    if "cameras" in data and isinstance(data.get("cameras"), list):
        entry["cameras"] = [str(x).strip() for x in data["cameras"] if str(x).strip()]
    elif "cameras" not in entry:
        entry["cameras"] = []

    entry["min_confidence"] = _clamp_float(
        data.get("min_confidence", entry.get("min_confidence", 0.0)), 0.0, 1.0, 0.0
    )

    if "template" in data:
        entry["template"] = str(data.get("template") or "")
    elif "template" not in entry:
        entry["template"] = ""

    entry["attach_snapshot"] = bool(data.get("attach_snapshot", entry.get("attach_snapshot", True)))
    entry["cooldown_sec"] = _clamp_int(
        data.get("cooldown_sec", entry.get("cooldown_sec", 0)), 0, 86400, 0
    )

    if entry["enabled"]:
        if ch_type == "telegram" and (not entry.get("token") or not entry.get("chat_id")):
            return jsonify({"error": "Telegram: для включённого канала нужны token и chat_id"}), 400
        if ch_type == "max" and (not entry.get("max_token") or not entry.get("max_recipient_id")):
            return jsonify(
                {"error": ("MAX: для включённого канала нужны max_token и max_recipient_id")}
            ), 400
        if ch_type == "webhook" and not entry.get("url"):
            return jsonify({"error": "Webhook: для включённого канала нужен url"}), 400

    if existing is None:
        mc["channels"].append(entry)
    else:
        idx = mc["channels"].index(existing)
        mc["channels"][idx] = entry

    _save_messaging_cfg(mc)
    logger.info(
        "messaging: сохранён канал '%s' (type=%s, enabled=%s)", cid, ch_type, entry["enabled"]
    )
    return jsonify({"ok": True, "channel": _messaging_public(entry)})


@app.route("/api/messaging/<channel_id>", methods=["DELETE"])
@auth_mod.admin_required
def api_messaging_delete(channel_id: str):
    mc = _get_messaging_cfg()
    before = len(mc["channels"])
    mc["channels"] = [c for c in mc["channels"] if c.get("id") != channel_id]
    if len(mc["channels"]) == before:
        return jsonify({"error": "канал не найден"}), 404
    _save_messaging_cfg(mc)
    logger.info("messaging: удалён канал '%s'", channel_id)
    return jsonify({"ok": True})


@app.route("/api/messaging/<channel_id>/test", methods=["POST"])
@auth_mod.admin_required
def api_messaging_test(channel_id: str):
    live = _get_live_messaging_manager()
    if live is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "MessageManager не активен. Возможные причины: "
                    "1) analytics.autostart=false в YAML; "
                    "2) канал добавлен через UI уже после старта, "
                    "перезапустите viewer.py."
                ),
            }
        ), 200
    res = live.test_channel(channel_id)
    return jsonify(res), 200


@app.route("/api/messaging/log")
@auth_mod.admin_required
def api_messaging_log():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))

    if not MESSAGES_LOG.exists():
        return jsonify({"lines": []})
    try:
        with open(MESSAGES_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 512 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
            lines = [ln for ln in data.split("\n") if ln.strip()]
            if chunk < size and lines:
                lines = lines[1:]
            return jsonify({"lines": lines[-limit:]})
    except Exception:
        logger.exception("не удалось прочитать messages.log")
        return jsonify({"lines": []})

# ======================================================================
# Bots: управление ботами мессенджеров (входящие команды)
# ======================================================================
def _get_bots_cfg() -> dict:
    raw = _load_root_yaml_section("bots")
    if not isinstance(raw, dict):
        raw = {}
    instances = raw.get("instances")
    if not isinstance(instances, list):
        instances = []
    return {
        "enabled": bool(raw.get("enabled", True)),
        "instances": [i for i in instances if isinstance(i, dict) and i.get("id")],
    }


def _save_bots_cfg(bots_cfg: dict):
    cfg = load_config()
    cfg["bots"] = bots_cfg
    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg


def _bot_public(b: dict) -> dict:
    out = dict(b)
    out.pop("token", None)
    return out


@app.route("/bots")
@auth_mod.admin_required
def bots_page():
    return render_template(
        "bots.html",
        user=g.user,
        app_footer=APP_FOOTER,
        version=__version__,
    )


@app.route("/api/bots/list")
@auth_mod.admin_required
def api_bots_list():
    bc = _get_bots_cfg()
    return jsonify({"instances": [_bot_public(i) for i in bc["instances"]]})


@app.route("/api/bots/<bot_id>", methods=["GET"])
@auth_mod.admin_required
def api_bots_get(bot_id: str):
    bc = _get_bots_cfg()
    for i in bc["instances"]:
        if i.get("id") == bot_id:
            return jsonify(_bot_public(i))
    return jsonify({"error": "бот не найден"}), 404


@app.route("/api/bots/<bot_id>", methods=["POST"])
@auth_mod.admin_required
def api_bots_save(bot_id: str):
    data = request.get_json(silent=True) or {}
    bid = str(bot_id or "").strip()
    if not re.match(r"^[a-zA-Z0-9_\-]{1,50}$", bid):
        return jsonify({"error": "id: только латиница, цифры, _ и - (до 50)"}), 400

    bc = _get_bots_cfg()
    existing = None
    for i in bc["instances"]:
        if i.get("id") == bid:
            existing = i
            break

    entry = dict(existing or {})
    entry["id"] = bid
    entry["name"] = str(data.get("name") or entry.get("name") or bid).strip()

    bt = str(data.get("type") or entry.get("type") or "vk").lower()
    if bt not in ("vk", "max", "telegram", "whatsapp"):
        bt = "vk"
    entry["type"] = bt
    entry["enabled"] = bool(data.get("enabled", entry.get("enabled", False)))

    if "token" in data and data.get("token"):
        entry["token"] = str(data["token"]).strip()
    elif "token" not in entry:
        entry["token"] = ""

    if "allowed_user_ids" in data and isinstance(data["allowed_user_ids"], list):
        cleaned = []
        for x in data["allowed_user_ids"]:
            try:
                cleaned.append(int(x))
            except (TypeError, ValueError):
                pass
        entry["allowed_user_ids"] = cleaned
    elif "allowed_user_ids" not in entry:
        entry["allowed_user_ids"] = []

    entry["relay_id"] = str(
        data.get("relay_id") or entry.get("relay_id") or "default"
    ).strip()

    if "extra" in data and isinstance(data["extra"], dict):
        entry["extra"] = data["extra"]
    elif "extra" not in entry:
        entry["extra"] = {}

    if entry["enabled"] and not entry.get("token"):
        return jsonify({"error": "для включённого бота нужен token"}), 400

    if existing is None:
        bc["instances"].append(entry)
    else:
        idx = bc["instances"].index(existing)
        bc["instances"][idx] = entry

    _save_bots_cfg(bc)
    logger.info(
        "bots: сохранён '%s' (type=%s, enabled=%s)", bid, bt, entry["enabled"]
    )
    return jsonify({"ok": True, "bot": _bot_public(entry)})


@app.route("/api/bots/<bot_id>", methods=["DELETE"])
@auth_mod.admin_required
def api_bots_delete(bot_id: str):
    bc = _get_bots_cfg()
    before = len(bc["instances"])
    bc["instances"] = [i for i in bc["instances"] if i.get("id") != bot_id]
    if len(bc["instances"]) == before:
        return jsonify({"error": "бот не найден"}), 404
    _save_bots_cfg(bc)
    logger.info("bots: удалён '%s'", bot_id)
    return jsonify({"ok": True})


@app.route("/api/bots/<bot_id>/config")
def api_bots_config_for_bot(bot_id: str):
    """Bot-facing endpoint: возвращает полный конфиг с токеном."""
    expected = os.environ.get("BOT_CONFIG_SECRET", "")
    provided = request.headers.get("X-Bot-Token", "")
    if not expected or provided != expected:
        logger.warning(
            "bots: отклонён fetch config '%s' (bad token)", bot_id
        )
        return jsonify({"error": "unauthorized"}), 401

    bc = _get_bots_cfg()
    for i in bc["instances"]:
        if i.get("id") == bot_id:
            if not i.get("enabled"):
                return jsonify({"error": "bot disabled"}), 403
            return jsonify(
                {
                    "id": i["id"],
                    "type": i["type"],
                    "name": i.get("name", i["id"]),
                    "token": i.get("token", ""),
                    "allowed_user_ids": i.get("allowed_user_ids", []),
                    "relay_id": i.get("relay_id", "default"),
                    "extra": i.get("extra", {}),
                }
            )
    return jsonify({"error": "бот не найден"}), 404
# ======================================================================
# Управление событиями (только admin)
# ======================================================================
def _read_all_events() -> list[dict]:
    if not EVENTS_JSONL.exists():
        return []
    out: list[dict] = []
    try:
        with open(EVENTS_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.exception("не удалось прочитать все события")
    return out


def _write_all_events(events: list[dict]):
    tmp_path = EVENTS_JSONL.with_suffix(".jsonl.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp_path, EVENTS_JSONL)
    except Exception:
        logger.exception("не удалось перезаписать alpr_events.jsonl")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/api/events/delete", methods=["POST"])
@auth_mod.admin_required
def api_events_delete():
    data = request.get_json(silent=True) or {}
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return jsonify({"error": "ts обязателен"}), 400

    events = _read_all_events()
    remaining: list[dict] = []
    found = None
    for e in events:
        e_ts = e.get("ts")
        if isinstance(e_ts, (int, float)) and abs(float(e_ts) - float(ts)) < 0.001:
            found = e
        else:
            remaining.append(e)

    if found is None:
        return jsonify({"error": "событие не найдено"}), 404

    _write_all_events(remaining)

    snap = found.get("snapshot")
    if snap:
        try:
            (LOGS_DIR / snap).unlink(missing_ok=True)
        except Exception:
            logger.exception("не удалось удалить снапшот")

    logger.info(
        "admin удалил событие: %s %s %s",
        found.get("iso"),
        found.get("camera_name"),
        found.get("label"),
    )
    return jsonify({"ok": True})


@app.route("/api/events/clear", methods=["POST"])
@auth_mod.admin_required
def api_events_clear():
    data = request.get_json(silent=True) or {}
    delete_snapshots = bool(data.get("delete_snapshots", True))

    try:
        with open(EVENTS_JSONL, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        logger.exception("не удалось очистить JSONL")
        return jsonify({"error": "не удалось очистить файл"}), 500

    removed_snaps = 0
    if delete_snapshots:
        try:
            snaps_dir = LOGS_DIR / "snapshots"
            if snaps_dir.exists():
                for f in snaps_dir.glob("*.jpg"):
                    try:
                        f.unlink()
                        removed_snaps += 1
                    except Exception:
                        pass
        except Exception:
            logger.exception("не удалось удалить снапшоты")

    logger.info("admin очистил все события (снапшотов удалено: %d)", removed_snaps)
    return jsonify({"ok": True, "snapshots_removed": removed_snaps})


@app.route("/api/integration/snapshot/<path:filename>")
def integration_snapshot(filename: str):
    """Выдача снапшота доверенному VHS-Security по токену."""
    if not _integration_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not filename.startswith("snapshots/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(LOGS_DIR, filename)


@app.route("/snapshot/<path:filename>")
@auth_mod.login_required
def snapshot_file(filename: str):
    return send_from_directory(LOGS_DIR, filename)


# ======================================================================
# API: конфиг (серверы + потоки + раскладка + analytics cameras)
# ======================================================================
@app.post("/api/whep")
@auth_mod.login_required
def api_whep():
    """Same-origin WHEP signaling proxy: браузер -> Viewer -> MediaMTX."""
    data = request.get_json(silent=True) or {}
    stream_url = str(data.get("url") or "").strip()
    sdp = str(data.get("sdp") or "")
    if not stream_url or not sdp:
        return jsonify({"ok": False, "error": "url and sdp required"}), 400
    parsed = urlsplit(stream_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return jsonify({"ok": False, "error": "invalid stream url"}), 400
    whep_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/whep"
    auth = None
    if parsed.username or parsed.password:
        auth = (parsed.username or "", parsed.password or "")
    try:
        response = requests.post(
            whep_url, data=sdp,
            headers={"Content-Type": "application/sdp"},
            auth=auth, timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    if not response.ok:
        return jsonify({"ok": False, "error": f"MediaMTX HTTP {response.status_code}"}), 502
    return jsonify({"ok": True, "sdp": response.text})


@app.route("/api/config", methods=["GET"])
@auth_mod.admin_required
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
@auth_mod.admin_required
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
        api_key = str(s.get("api_key") or "").strip()
        servers.append({"name": name, "url": url, "api_key": api_key})
    cfg["servers"] = servers

    # --- Внутренний ключ Viewer для Security ---
    if "integration" in data or "ingest_key" in data:
        integ = cfg.get("integration") if isinstance(cfg.get("integration"), dict) else {}
        incoming = data.get("integration") if isinstance(data.get("integration"), dict) else {}
        key = str(
            incoming.get("ingest_key", data.get("ingest_key", integ.get("ingest_key", "")))
            or ""
        ).strip()
        integ["ingest_key"] = key
        integ.setdefault("service_name", "viewer")
        cfg["integration"] = integ
        if key:
            # EventWriter отправляет события в Security и читает ключ из env.
            os.environ["VHS_SECURITY_INGEST_TOKEN"] = key

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
        name = (s.get("name") or "").strip() or f"Камера {i + 1}"
        enabled = bool(s.get("enabled", True))
        streams.append({"name": name, "url": url, "enabled": enabled})
    cfg["streams"] = streams

    # --- Analytics cameras (из UI) ---
    raw_an_cams = data.get("analytics_cameras")
    if isinstance(raw_an_cams, dict):
        an = cfg.get("analytics")
        if not isinstance(an, dict):
            an = {}
        cams = an.get("cameras")
        if not isinstance(cams, dict):
            cams = {}

        default_entry = dict(cams.get("default") or {})
        default_entry.setdefault("enabled", True)
        default_entry.setdefault("plates", True)
        default_entry.setdefault("objects", [])

        new_cams: dict = {"default": default_entry}
        for sid, sc in raw_an_cams.items():
            if not isinstance(sc, dict):
                continue
            enabled = bool(sc.get("enabled", True))
            plates = bool(sc.get("plates", False))
            objects = [str(x) for x in (sc.get("objects") or []) if isinstance(x, str)]

            sid_key = str(sid)
            old = cams.get(sid_key) or {}
            entry = {
                "enabled": enabled,
                "plates": plates,
                "objects": objects,
            }
            aen = old.get("analyze_every_n")
            if isinstance(aen, int) and aen >= 1:
                entry["analyze_every_n"] = aen
            oisz = old.get("object_imgsz")
            if isinstance(oisz, int) and oisz >= 160:
                entry["object_imgsz"] = oisz

            new_cams[sid_key] = entry

        an["cameras"] = new_cams
        cfg["analytics"] = an

    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg
    logger.info(
        "Сохранено: серверов %d, потоков %d, analytics-cameras %d",
        len(servers),
        len(streams),
        len((cfg.get("analytics") or {}).get("cameras") or {}),
    )
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
    key = str(data.get("api_key") or "").strip()

    try:
        streams = _get_streamer_streams({"url": url, "api_key": key}, timeout=5)
        info = {}
        try:
            info = requests.get(
                url + "/api/integration/info", headers=_vhs_headers(key), timeout=4
            ).json() or {}
        except (requests.RequestException, ValueError):
            pass
        return jsonify(
            {
                "ok": True,
                "server_name": info.get("app", "VHS-Streamer"),
                "version": info.get("version", ""),
                "streams_count": len(streams),
                "auth": bool(key),
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ======================================================================
# API: загрузка потоков со всех серверов
# ======================================================================
@app.route("/api/servers/refresh", methods=["POST"])
@auth_mod.admin_required
def api_servers_refresh():
    cfg = load_config()
    payload = request.get_json(silent=True) or {}
    servers = payload.get("servers") if isinstance(payload.get("servers"), list) else None
    if servers is None:
        servers = cfg.get("servers", [])
    if not servers:
        return jsonify(
            {
                "ok": True,
                "streams": [],
                "errors": [],
                "message": "Список серверов пуст",
            }
        )

    all_streams = []
    errors = []

    for srv in servers:
        srv_url = _normalize_server_url(srv.get("url", ""))
        srv_name = srv.get("name", srv_url)
        if not srv_url:
            continue
        try:
            streams = _get_streamer_streams(srv, timeout=5)
        except Exception as e:
            logger.warning("Сервер %s недоступен: %s", srv_url, e)
            errors.append({"server": srv_name, "url": srv_url, "error": str(e)})
            continue

        for st in streams:
            webrtc_urls = st.get("webrtc_urls", [])
            chosen = None
            for u in webrtc_urls:
                if "локально" in (u.get("label") or "").lower():
                    chosen = u
                    break
            if not chosen and webrtc_urls:
                chosen = webrtc_urls[0]
            url = (chosen or {}).get("url") or st.get("webrtc_url", "")
            if not url:
                continue

            uid = f"{srv_url}|{st.get('path', '')}"
            name = st.get("name") or st.get("id") or "Поток"
            if len(servers) > 1:
                name = f"{srv_name} · {name}"

            all_streams.append(
                {
                    "uid": uid,
                    "server": srv_name,
                    "server_url": srv_url,
                    "name": name,
                    "camera_name": st.get("camera_name", ""),
                    "path": st.get("path", ""),
                    "url": url,
                    "all_urls": webrtc_urls,
                }
            )

    return jsonify(
        {
            "ok": True,
            "streams": all_streams,
            "errors": errors,
            "servers_count": len(servers),
        }
    )


# ======================================================================
# API: безопасность — управление пользователями
# ======================================================================
@app.route("/api/security/config", methods=["GET"])
@auth_mod.admin_required
def api_security_config_get():
    cfg = load_config()
    sec = cfg.get("security", {})
    return jsonify(
        {
            "auth_enabled": sec.get("auth_enabled", True),
            "session_lifetime_hours": sec.get("session_lifetime_hours", 24),
            "users": [
                {
                    "username": u.get("username", ""),
                    "has_password": bool(u.get("password")),
                    "role": u.get("role", "admin"),
                }
                for u in sec.get("users", [])
            ],
        }
    )


@app.route("/api/security/config", methods=["POST"])
@auth_mod.admin_required
def api_security_config_set():
    data = request.get_json(silent=True) or {}

    raw_users = data.get("users", [])
    if not isinstance(raw_users, list):
        return jsonify({"error": "users должен быть списком"}), 400

    cfg_before = load_config()
    existing_users = {
        str(u.get("username", "")): str(u.get("password", ""))
        for u in (cfg_before.get("security", {}) or {}).get("users", [])
    }
    cleaned = []
    seen = set()
    for i, u in enumerate(raw_users):
        if not isinstance(u, dict):
            return jsonify({"error": f"Пользователь #{i + 1} — не объект"}), 400

        username = str(u.get("username", "")).strip()
        password = str(u.get("password", ""))
        role = str(u.get("role", "admin")).strip()

        if not username:
            return jsonify({"error": f"У пользователя #{i + 1} пустой логин"}), 400
        if not password:
            previous = existing_users.get(username)
            if not previous:
                return jsonify(
                    {"error": f"У пользователя '{username}' пустой пароль"}
                ), 400
            password = (
                previous
                if auth_mod.is_password_hash(previous)
                else auth_mod.hash_password(previous)
            )
        elif not auth_mod.is_password_hash(password):
            password = auth_mod.hash_password(password)
        if role not in ("admin", "viewer"):
            return jsonify({"error": f"Недопустимая роль '{role}' у '{username}'"}), 400
        if username in seen:
            return jsonify({"error": f"Логин '{username}' повторяется"}), 400

        seen.add(username)
        cleaned.append(
            {
                "username": username,
                "password": password,
                "role": role,
            }
        )

    if not cleaned:
        return jsonify({"error": "Список пользователей не может быть пустым"}), 400
    if not any(u["role"] == "admin" for u in cleaned):
        return jsonify({"error": "Должен остаться хотя бы один администратор"}), 400

    auth_enabled = bool(data.get("auth_enabled", True))
    lifetime = data.get("session_lifetime_hours")
    try:
        lifetime = int(lifetime) if lifetime is not None else 24
        if lifetime < 1 or lifetime > 24 * 30:
            lifetime = 24
    except (TypeError, ValueError):
        lifetime = 24

    cfg = load_config()
    sec = cfg.setdefault("security", {})
    sec["auth_enabled"] = auth_enabled
    sec["session_lifetime_hours"] = lifetime
    sec["users"] = cleaned

    save_config(cfg)
    app.config["VIEWER_CONFIG"] = cfg

    logger.info(
        "Настройки безопасности обновлены: auth_enabled=%s, пользователей=%d",
        auth_enabled,
        len(cleaned),
    )

    current_username = getattr(g, "user", {}).get("username")
    for u in cleaned:
        if u["username"] == current_username:
            session["user"] = {"username": u["username"], "role": u["role"]}
            break

    return jsonify(
        {
            "ok": True,
            "users_count": len(cleaned),
            "auth_enabled": auth_enabled,
        }
    )


@app.route("/api/security/generate_password", methods=["POST"])
@auth_mod.admin_required
def api_security_generate_password():
    import secrets

    return jsonify({"password": secrets.token_urlsafe(12)})


# ======================================================================
# Служебные
# ======================================================================
@app.route("/api/status")
def api_status():
    """Публичный — health-check."""
    return jsonify(
        {
            "app": {
                "name": APP_NAME,
                "version": __version__,
                "author": __author__,
            },
        }
    )


@app.route("/favicon.ico")
def favicon():
    try:
        return send_from_directory(RES_DIR / "static", "favicon.ico", mimetype="image/x-icon")
    except Exception:
        return "", 404


# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
def _open_browser():
    webbrowser.open("http://127.0.0.1:8200")


# Менеджер встроенной аналитики (глобальный, чтобы можно было остановить)
_analytics_manager = None


def _start_embedded_analytics():
    """
    Запускает AnalyticsManager в том же процессе, что и Viewer.
    ...
    """
    global _analytics_manager

    if _analytics_manager is not None:
        return _analytics_manager

    if not _is_setup_complete():
        logger.info(
            "embedded analytics: мастер настройки не завершён — аналитика запустится после /setup"
        )
        return None

    cfg = load_config()
    an = cfg.get("analytics") or {}

    if not an.get("enabled"):
        logger.info("embedded analytics: отключена (analytics.enabled=false)")
        return None

    if not an.get("autostart", True):
        logger.info(
            "embedded analytics: autostart=false — запустите `python run_analytics.py` отдельно"
        )
        return None

    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return None

    try:
        from vhs_analytics.bootstrap import build_manager
    except Exception:
        logger.exception("embedded analytics: не удалось импортировать vhs_analytics")
        return None

    try:
        manager = build_manager(
            config_path=str(CONFIG_PATH),
            log_dir=LOGS_DIR,
        )
    except Exception:
        logger.exception("embedded analytics: build_manager упал")
        return None

    if manager is None:
        logger.warning(
            "embedded analytics: build_manager вернул None (проверьте analytics.enabled в конфиге)"
        )
        return None

    try:
        manager.start()
    except Exception:
        logger.exception("embedded analytics: не удалось запустить manager")
        return None

    _analytics_manager = manager
    logger.info("embedded analytics: запущена")
    return manager


def _stop_embedded_analytics():
    global _analytics_manager
    if _analytics_manager is None:
        return
    try:
        from vhs_analytics.bootstrap import stop_manager

        stop_manager(_analytics_manager)
        logger.info("embedded analytics: остановлена")
    except Exception:
        logger.exception("embedded analytics: ошибка при остановке")
    _analytics_manager = None


if __name__ == "__main__":
    cfg = load_config()
    sec = cfg.get("security", {})
    auth_on = sec.get("auth_enabled", True)

    logger.info("Старт сервера: http://127.0.0.1:8200")
    logger.info("Конфиг: %s", CONFIG_PATH)

    if not _is_setup_complete():
        logger.warning(
            "ПЕРВЫЙ ЗАПУСК: откройте http://127.0.0.1:8200/setup "
            "для настройки администратора и Streamer"
        )
    else:
        if auth_on:
            users_count = len(sec.get("users", []))
            logger.info("Авторизация: включена (%d пользователей)", users_count)
        else:
            logger.warning("Авторизация: ВЫКЛЮЧЕНА — доступ открыт для всех")
        _start_embedded_analytics()

    import atexit

    atexit.register(_stop_embedded_analytics)

    Timer(1.0, _open_browser).start()
    app.run(host="0.0.0.0", port=8200, debug=False, threaded=True)
