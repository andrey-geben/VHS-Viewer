"""
Управление HTTP-реле по распознанному номеру.

Архитектура:
- RelayInstance — одно реле: свой URL, свой белый список, свои камеры.
- RelayManager  — список реле + общий лог logs/relay.log с колонкой relay_id.

Логика срабатывания (RelayManager.check_all):
  для каждого реле в списке:
    если реле.enabled
    и (реле.cameras пуст ИЛИ camera_id ∈ реле.cameras)
    и plate ∈ реле.whitelist
    и plate не в cooldown этого реле
    → HTTP url_open; если задан url_close — авто-закрытие через open_duration_sec.

Номер не из белого списка → реле молчит (событие всё равно записывается
в alpr_events.jsonl через EventWriter в bootstrap.on_event).

Backward compatibility: если в конфиге старый плоский формат
(relay.url_open без relay.relays), он автоматически превращается
в одно реле с id="default" и cameras=[] (все камеры).
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Вспомогательные
# ----------------------------------------------------------------------
def normalize_plate(text: str) -> str:
    """Верхний регистр, только буквы (А-Я, A-Z) и цифры."""
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


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


# ----------------------------------------------------------------------
# Одно реле
# ----------------------------------------------------------------------
class RelayInstance:
    def __init__(self, cfg: dict, log_writer):
        self._log_write = log_writer
        self._last_trigger: dict[str, float] = {}
        self._close_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._apply_cfg(cfg)

    def _apply_cfg(self, cfg: dict):
        self.id = str(cfg.get("id") or "").strip() or "default"
        self.name = str(cfg.get("name") or self.id).strip()
        self.enabled = bool(cfg.get("enabled", False))
        self.url_open = (cfg.get("url_open") or "").strip()
        self.url_close = (cfg.get("url_close") or "").strip()
        self.method = (cfg.get("method") or "GET").upper()
        if self.method not in ("GET", "POST"):
            self.method = "GET"
        self.timeout = _clamp_float(cfg.get("timeout", 5.0), 0.5, 60.0, 5.0)
        self.open_duration_sec = _clamp_int(cfg.get("open_duration_sec", 5), 1, 3600, 5)
        self.cooldown_sec = _clamp_int(cfg.get("cooldown_sec", 30), 0, 86400, 30)
        self.headers = cfg.get("headers") or {}
        self.auth = None
        if cfg.get("username"):
            self.auth = (cfg["username"], cfg.get("password") or "")

        cams = cfg.get("cameras") or []
        self.cameras = set(str(c).strip() for c in cams if str(c).strip())

        wl = set()
        for p in (cfg.get("whitelist") or []):
            n = normalize_plate(p)
            if n:
                wl.add(n)
        self.whitelist = wl

    def matches_camera(self, camera_id: str) -> bool:
        if not self.cameras:
            return True
        return camera_id in self.cameras

    def matches_whitelist(self, plate: str) -> bool:
        return normalize_plate(plate) in self.whitelist

    def check(self, plate: str, camera_id: str, camera_name: str) -> bool:
        if not self.enabled or not self.url_open:
            return False
        if not self.matches_camera(camera_id):
            return False
        plate_norm = normalize_plate(plate)
        if not plate_norm or plate_norm not in self.whitelist:
            return False

        now = time.time()
        with self._lock:
            last = self._last_trigger.get(plate_norm, 0.0)
            if now - last < self.cooldown_sec:
                return False
            self._last_trigger[plate_norm] = now

        ok, err = self._trigger(self.url_open)
        if ok:
            self._log_write(self.id, self.name, plate_norm, camera_id,
                            camera_name, "open", f"url_open: {self.url_open}")
            log.info(
                f"Relay[{self.id}]: открыто для {plate_norm} "
                f"(камера: {camera_name})"
            )
            if self.url_close:
                self._schedule_close(plate_norm, self.url_close)
            return True

        self._log_write(self.id, self.name, plate_norm, camera_id,
                        camera_name, "error", f"url_open: {err}")
        log.error(f"Relay[{self.id}]: ошибка для {plate_norm}: {err}")
        return False

    def _trigger(self, url: str) -> tuple[bool, str | None]:
        try:
            if self.method == "POST":
                r = requests.post(url, headers=self.headers,
                                  auth=self.auth, timeout=self.timeout)
            else:
                r = requests.get(url, headers=self.headers,
                                 auth=self.auth, timeout=self.timeout)
            if 200 <= r.status_code < 400:
                return True, None
            return False, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            return False, str(e)
        except Exception as e:
            return False, f"unexpected: {e}"

    def _schedule_close(self, plate: str, url: str):
        with self._lock:
            old = self._close_timers.get(plate)
            if old and old.is_alive():
                old.cancel()

        def _do_close():
            ok, err = self._trigger(url)
            if ok:
                self._log_write(self.id, self.name, plate, "", "",
                                "close", f"url_close: {url}")
                log.info(f"Relay[{self.id}]: закрыто для {plate}")
            else:
                self._log_write(self.id, self.name, plate, "", "",
                                "error", f"url_close: {err}")
                log.warning(
                    f"Relay[{self.id}]: не удалось закрыть {plate}: {err}"
                )

        t = threading.Timer(self.open_duration_sec, _do_close)
        t.daemon = True
        t.start()
        with self._lock:
            self._close_timers[plate] = t

    def test(self) -> dict:
        if not self.url_open:
            return {"ok": False, "error": "url_open не задан"}
        ok, err = self._trigger(self.url_open)
        if ok and self.url_close:
            self._schedule_close("__test__", self.url_close)
        return {"ok": ok, "error": err}

    def stop(self):
        with self._lock:
            for t in self._close_timers.values():
                if t.is_alive():
                    t.cancel()
            self._close_timers.clear()


# ----------------------------------------------------------------------
# Менеджер списка реле
# ----------------------------------------------------------------------
class RelayManager:
    def __init__(self, cfg: dict, log_dir: Path):
        self.log_path = Path(log_dir) / "relay.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self.relays: dict[str, RelayInstance] = {}
        self._apply_cfg(cfg)

    def _apply_cfg(self, cfg: dict):
        relays_raw = cfg.get("relays")
        if not isinstance(relays_raw, list):
            if cfg.get("url_open") or cfg.get("whitelist"):
                old = dict(cfg)
                old["id"] = "default"
                old["name"] = old.get("name") or "Реле по умолчанию"
                old["cameras"] = []
                relays_raw = [old]
                log.info(
                    "relay: плоский формат сконвертирован в одно реле 'default'"
                )
            else:
                relays_raw = []

        new_relays: dict[str, RelayInstance] = {}
        for rc in relays_raw:
            if not isinstance(rc, dict):
                continue
            rid = str(rc.get("id") or "").strip()
            if not rid:
                continue
            try:
                new_relays[rid] = RelayInstance(rc, self._log_write)
            except Exception:
                log.exception(f"relay[{rid}]: не удалось создать")

        for old in self.relays.values():
            old.stop()

        self.relays = new_relays
        enabled_count = sum(1 for r in self.relays.values() if r.enabled)
        log.info(
            f"RelayManager: реле={len(self.relays)}, включено={enabled_count}"
        )

    def reload_from_cfg(self, cfg: dict):
        with self._lock:
            self._apply_cfg(cfg)
        log.info("RelayManager: конфигурация перезагружена")

    def check_all(self, plate: str, camera_id: str, camera_name: str) -> int:
        count = 0
        with self._lock:
            relays = list(self.relays.values())
        for r in relays:
            try:
                if r.check(plate, camera_id, camera_name):
                    count += 1
            except Exception:
                log.exception(f"relay[{r.id}]: ошибка check")
        return count

    def test_relay(self, relay_id: str) -> dict:
        with self._lock:
            r = self.relays.get(relay_id)
        if r is None:
            return {"ok": False, "error": f"реле '{relay_id}' не найдено"}
        return r.test()

    def _log_write(self, relay_id: str, relay_name: str, plate: str,
                   camera_id: str, camera_name: str,
                   action: str, details: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}\t{relay_id}\t{plate}\t{camera_id}\t{action}\t{details}\n"
        try:
            with self._write_lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            log.exception("relay: не удалось записать в relay.log")

    def stop(self):
        with self._lock:
            for r in self.relays.values():
                r.stop()
            self.relays.clear()