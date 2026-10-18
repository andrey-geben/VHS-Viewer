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
    * Встроенная аналитика (autostart) — без отдельного run_analytics.py.
"""
import json
import logging
import os
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Timer

import requests
import yaml
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    redirect, url_for, g, session,
)

from __version__ import (
    APP_NAME, APP_TITLE, APP_FOOTER,
    __version__, __author__, __copyright__, __license__, __url__,
)

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


def _normalize_plate(text: str) -> str:
    """Верхний регистр, только буквы/цифры. Та же логика, что в relay.py."""
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


# ----------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------
app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
)
app.secret_key = "vhs-viewer-secret-change-me"

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
                    if needle not in plate_s and needle not in label_s:
                        continue

                out.append(e)
                if len(out) >= limit:
                    break

            out.reverse()
            return out
    except Exception:
        logger.exception("не удалось прочитать alpr_events.jsonl")
        return []


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
    return jsonify({
        "events": events,
        "count": len(events),
        "filters": {
            "categories": categories,
            "camera_id": camera_id,
            "search": search,
            "limit": limit,
        },
    })


@app.route("/api/events/cameras")
@auth_mod.login_required
def api_events_cameras():
    events = _read_recent_events(limit=1000)
    seen: dict[str, str] = {}
    for e in events:
        cid = e.get("camera_id")
        if cid and cid not in seen:
            seen[cid] = e.get("camera_name") or cid
    return jsonify({
        "cameras": [
            {"id": cid, "name": name}
            for cid, name in sorted(seen.items(), key=lambda x: x[1].lower())
        ],
    })


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

        entry = cameras.setdefault(cid, {
            "camera_id": cid,
            "camera_name": e.get("camera_name") or cid,
            "total": 0,
            "by_category": {},
            "last_event": None,
        })
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
                "confidence": e.get("confidence"),
                "snapshot": e.get("snapshot"),
            }

    return jsonify({
        "cameras": cameras,
        "date": today_prefix,
        "events_scanned": scanned,
    })


# ======================================================================
# Реле: список, CRUD, whitelist, тест, лог (только admin)
# ======================================================================
def _load_root_yaml_section(section: str) -> dict:
    """Читает секцию верхнего уровня из YAML."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
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
        logger.info(
            "RelayManager создан лениво из UI: реле=%d", len(live.relays)
        )
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
    return jsonify({
        "relays": [_relay_public(r) for r in rc.get("relays", [])],
    })


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

    url_open = str(
        data.get("url_open", existing.get("url_open") if existing else "") or ""
    ).strip()
    enabled = bool(
        data.get("enabled", existing.get("enabled", False) if existing else False)
    )
    if enabled and not url_open:
        return jsonify({"error": "url_open обязателен, если реле включено"}), 400

    new_entry: dict = dict(existing or {})
    new_entry["id"] = rid
    new_entry["name"] = str(
        data.get("name") or (existing or {}).get("name") or rid
    ).strip()
    new_entry["enabled"] = enabled
    new_entry["url_open"] = url_open
    new_entry["url_close"] = str(
        data.get("url_close", existing.get("url_close") if existing else "") or ""
    ).strip()
    m = str(
        data.get("method", existing.get("method") if existing else "GET") or "GET"
    ).upper()
    new_entry["method"] = m if m in ("GET", "POST") else "GET"
    new_entry["timeout"] = _clamp_float(
        data.get("timeout", existing.get("timeout", 5.0) if existing else 5.0),
        0.5, 60.0, 5.0)
    new_entry["open_duration_sec"] = _clamp_int(
        data.get("open_duration_sec",
                 existing.get("open_duration_sec", 5) if existing else 5),
        1, 3600, 5)
    new_entry["cooldown_sec"] = _clamp_int(
        data.get("cooldown_sec",
                 existing.get("cooldown_sec", 30) if existing else 30),
        0, 86400, 30)

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
            new_entry["cameras"] = [
                str(c).strip() for c in cams if str(c).strip()
            ]
        else:
            new_entry["cameras"] = []

    if "whitelist" in data and isinstance(data.get("whitelist"), list):
        new_entry["whitelist"] = [
            str(p).strip() for p in data["whitelist"] if str(p).strip()
        ]
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
        return jsonify({
            "ok": False,
            "error": (
                "RelayManager не активен. Возможные причины: "
                "1) analytics.autostart=false в YAML; "
                "2) реле добавлено через UI уже после старта, "
                "перезапустите viewer.py."
            ),
        }), 200
    res = live.test_relay(relay_id)
    return jsonify(res), 200


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
    new_wl = [
        p for p in wl
        if _normalize_plate(p) != _normalize_plate(plate)
    ]
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
        logger.info(
            "MessageManager: аналитика не запущена, изменения только в YAML"
        )
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
    return jsonify({
        "channels": [_messaging_public(c) for c in mc.get("channels", [])],
    })


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

    # Подключение Telegram
    if "token" in data and data.get("token"):
        entry["token"] = str(data["token"]).strip()
    elif "token" not in entry:
        entry["token"] = ""

    entry["chat_id"] = str(
        data.get("chat_id", entry.get("chat_id", "")) or ""
    ).strip()
    entry["url"] = str(data.get("url", entry.get("url", "")) or "").strip()

    m = str(data.get("method", entry.get("method", "POST")) or "POST").upper()
    entry["method"] = m if m in ("GET", "POST") else "POST"
    entry["timeout"] = _clamp_float(
        data.get("timeout", entry.get("timeout", 10.0)),
        1.0, 120.0, 10.0)

    # MAX-специфичные поля
    if "max_token" in data and data.get("max_token"):
        entry["max_token"] = str(data["max_token"]).strip()
    elif "max_token" not in entry:
        entry["max_token"] = ""

    entry["max_recipient_id"] = str(
        data.get("max_recipient_id", entry.get("max_recipient_id", "")) or ""
    ).strip()

    rt = str(
        data.get("max_recipient_type", entry.get("max_recipient_type", "user"))
        or "user"
    ).lower()
    entry["max_recipient_type"] = rt if rt in ("user", "chat") else "user"

    # Фильтры
    if "categories" in data and isinstance(data.get("categories"), list):
        entry["categories"] = [
            str(x).strip().lower()
            for x in data["categories"] if str(x).strip()
        ]
    elif "categories" not in entry:
        entry["categories"] = []

    if "cameras" in data and isinstance(data.get("cameras"), list):
        entry["cameras"] = [
            str(x).strip() for x in data["cameras"] if str(x).strip()
        ]
    elif "cameras" not in entry:
        entry["cameras"] = []

    entry["min_confidence"] = _clamp_float(
        data.get("min_confidence", entry.get("min_confidence", 0.0)),
        0.0, 1.0, 0.0)

    # Шаблон / снапшот / cooldown
    if "template" in data:
        entry["template"] = str(data.get("template") or "")
    elif "template" not in entry:
        entry["template"] = ""

    entry["attach_snapshot"] = bool(
        data.get("attach_snapshot", entry.get("attach_snapshot", True)))
    entry["cooldown_sec"] = _clamp_int(
        data.get("cooldown_sec", entry.get("cooldown_sec", 0)),
        0, 86400, 0)

    # Валидация обязательных полей при enabled
    if entry["enabled"]:
        if ch_type == "telegram" and (
            not entry.get("token") or not entry.get("chat_id")
        ):
            return jsonify({
                "error": "Telegram: для включённого канала нужны token и chat_id"
            }), 400
        if ch_type == "max" and (
            not entry.get("max_token") or not entry.get("max_recipient_id")
        ):
            return jsonify({
                "error": (
                    "MAX: для включённого канала нужны "
                    "max_token и max_recipient_id"
                )
            }), 400
        if ch_type == "webhook" and not entry.get("url"):
            return jsonify({
                "error": "Webhook: для включённого канала нужен url"
            }), 400

    if existing is None:
        mc["channels"].append(entry)
    else:
        idx = mc["channels"].index(existing)
        mc["channels"][idx] = entry

    _save_messaging_cfg(mc)
    logger.info("messaging: сохранён канал '%s' (type=%s, enabled=%s)",
                cid, ch_type, entry["enabled"])
    return jsonify({"ok": True, "channel": _messaging_public(entry)})


@app.route("/api/messaging/<channel_id>", methods=["DELETE"])
@auth_mod.admin_required
def api_messaging_delete(channel_id: str):
    mc = _get_messaging_cfg()
    before = len(mc["channels"])
    mc["channels"] = [
        c for c in mc["channels"] if c.get("id") != channel_id
    ]
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
        return jsonify({
            "ok": False,
            "error": (
                "MessageManager не активен. Возможные причины: "
                "1) analytics.autostart=false в YAML; "
                "2) канал добавлен через UI уже после старта, "
                "перезапустите viewer.py."
            ),
        }), 200
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
# Управление событиями (только admin)
# ======================================================================
def _read_all_events() -> list[dict]:
    if not EVENTS_JSONL.exists():
        return []
    out: list[dict] = []
    try:
        with open(EVENTS_JSONL, "r", encoding="utf-8") as f:
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
        found.get("iso"), found.get("camera_name"), found.get("label"),
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

    logger.info(
        "admin очистил все события (снапшотов удалено: %d)", removed_snaps
    )
    return jsonify({"ok": True, "snapshots_removed": removed_snaps})


@app.route("/snapshot/<path:filename>")
@auth_mod.login_required
def snapshot_file(filename: str):
    return send_from_directory(LOGS_DIR, filename)


# ======================================================================
# API: конфиг (серверы + потоки + раскладка + analytics cameras)
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

    # --- Analytics cameras (из UI) ---
    raw_an_cams = data.get("analytics_cameras")
    if isinstance(raw_an_cams, dict):
        an = cfg.get("analytics")
        if not isinstance(an, dict):
            an = {}
        cams = an.get("cameras")
        if not isinstance(cams, dict):
            cams = {}

        # FIX: сохраняем analyze_every_n и object_imgsz из старых записей,
        # UI их пока не редактирует, но они должны остаться.
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
            objects = [
                str(x) for x in (sc.get("objects") or []) if isinstance(x, str)
            ]

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

    try:
        r = requests.get(f"{url}/api/public/streams", timeout=5)
        if r.status_code != 200:
            return jsonify({"ok": False,
                            "error": f"HTTP {r.status_code}"}), 200
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
            "ok": True, "streams": [], "errors": [],
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
            errors.append({"server": srv_name, "url": srv_url,
                           "error": str(e)})
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
    data = request.get_json(silent=True) or {}

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
        auth_enabled, len(cleaned),
    )

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
    import secrets
    return jsonify({"password": secrets.token_urlsafe(12)})


# ======================================================================
# Служебные
# ======================================================================
@app.route("/api/status")
def api_status():
    """Публичный — health-check."""
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


# Менеджер встроенной аналитики (глобальный, чтобы можно было остановить)
_analytics_manager = None


def _start_embedded_analytics():
    """
    Запускает AnalyticsManager в том же процессе, что и Viewer.

    Условия запуска:
    - analytics.enabled: true в viewer_config.yaml
    - analytics.autostart: true (по умолчанию true)
    - не под Flask reloader (WERKZEUG_RUN_MAIN != "false")
    """
    global _analytics_manager

    if _analytics_manager is not None:
        return _analytics_manager

    cfg = load_config()
    an = cfg.get("analytics") or {}

    if not an.get("enabled"):
        logger.info("embedded analytics: отключена (analytics.enabled=false)")
        return None

    if not an.get("autostart", True):
        logger.info(
            "embedded analytics: autostart=false — "
            "запустите `python run_analytics.py` отдельно"
        )
        return None

    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return None

    try:
        from vhs_analytics.bootstrap import build_manager
    except Exception:
        logger.exception(
            "embedded analytics: не удалось импортировать vhs_analytics"
        )
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
            "embedded analytics: build_manager вернул None "
            "(проверьте analytics.enabled в конфиге)"
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
    if auth_on:
        users_count = len(sec.get("users", []))
        logger.info("Авторизация: включена (%d пользователей)", users_count)
    else:
        logger.warning("Авторизация: ВЫКЛЮЧЕНА — доступ открыт для всех")

    import atexit
    atexit.register(_stop_embedded_analytics)
    _start_embedded_analytics()

    Timer(1.0, _open_browser).start()
    app.run(host="0.0.0.0", port=8200, debug=False, threaded=True)