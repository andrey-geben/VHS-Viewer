"""
Отправка сообщений о событиях в мессенджеры и вебхуки.

Архитектура:
- MessageChannel — один канал (Telegram-бот, MAX-бот, webhook).
  Свой набор фильтров: какие категории/камеры, минимальный confidence,
  cooldown, шаблон сообщения, attach_snapshot.
- MessageManager — список каналов. Проходит по всем при событии,
  каждый канал сам решает, отправлять или нет.

Поддерживаемые типы:
  * telegram — Bot API sendMessage (текст) / sendPhoto (текст + JPEG)
  * max      — MAX Bot API POST /messages. Для изображений — двухшаговая
               загрузка через POST /uploads?type=image (см. _send_max)
  * webhook  — HTTP POST/GET с JSON-телом {text, event}

MAX особенности:
  - домен platform-api2.max.ru
  - токен ТОЛЬКО в заголовке Authorization
  - получатель: user_id (личный диалог) или chat_id (группа/канал)
  - лимит 2 сообщения/сек в один диалог
  - если отправить сразу после загрузки картинки — attachment.not.ready,
    поэтому retry с нарастающей паузой.
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CATEGORY_EMOJI = {
    "plate": "🚗",
    "person": "👤",
    "vehicle": "🚙",
    "animal": "🐻",
    "object": "📦",
}

CATEGORY_RUS = {
    "plate": "Номер",
    "person": "Человек",
    "vehicle": "Транспорт",
    "animal": "Животное",
    "object": "Объект",
}

DEFAULT_TEMPLATE = (
    "{emoji} {category_rus} на *{camera_name}*\n"
    "{detail}\n"
    "Время: {iso}\n"
    "Уверенность: {confidence_pct}%"
)

MAX_API_BASE = "https://platform-api2.max.ru"


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


def _normalize_plate(text: str) -> str:
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


def _render_template(template: str, ctx: dict) -> str:
    """Подставляет {placeholder} в шаблон. Неизвестные — пустая строка."""

    def repl(match):
        key = match.group(1)
        return str(ctx.get(key, ""))

    return re.sub(r"\{(\w+)\}", repl, template or "")


class MessageChannel:
    def __init__(self, cfg: dict):
        self._apply_cfg(cfg)
        self._lock = threading.Lock()
        self._last_sent: dict[tuple, float] = {}

    def _apply_cfg(self, cfg: dict):
        self.id = str(cfg.get("id") or "").strip() or "default"
        self.name = str(cfg.get("name") or self.id).strip()
        self.type = str(cfg.get("type") or "telegram").strip().lower()
        if self.type not in ("telegram", "max", "webhook"):
            self.type = "telegram"
        self.enabled = bool(cfg.get("enabled", False))

        # Подключение
        self.token = str(cfg.get("token") or "").strip()
        self.chat_id = str(cfg.get("chat_id") or "").strip()
        self.url = str(cfg.get("url") or "").strip()
        self.method = str(cfg.get("method") or "POST").upper()
        if self.method not in ("GET", "POST"):
            self.method = "POST"
        self.timeout = _clamp_float(cfg.get("timeout", 10.0), 1.0, 120.0, 10.0)
        self.headers = cfg.get("headers") or {}

        # MAX-специфичные поля
        self.max_token = str(cfg.get("max_token") or "").strip()
        self.max_recipient_id = str(cfg.get("max_recipient_id") or "").strip()
        rtype = str(cfg.get("max_recipient_type") or "user").strip().lower()
        self.max_recipient_type = rtype if rtype in ("user", "chat") else "user"

        # Фильтры
        cats = cfg.get("categories") or []
        self.categories = set(str(c).strip().lower() for c in cats if str(c).strip())
        cams = cfg.get("cameras") or []
        self.cameras = set(str(c).strip() for c in cams if str(c).strip())
        self.min_confidence = _clamp_float(cfg.get("min_confidence", 0.0), 0.0, 1.0, 0.0)

        # Что отправляем
        self.template = str(cfg.get("template") or DEFAULT_TEMPLATE)
        self.attach_snapshot = bool(cfg.get("attach_snapshot", True))
        self.cooldown_sec = _clamp_int(cfg.get("cooldown_sec", 0), 0, 86400, 0)

    def matches(self, detection: dict, stream: dict) -> bool:
        """Подходит ли событие под фильтры канала."""
        if not self.enabled:
            return False

        category = str(detection.get("category") or "object").lower()
        if self.categories and category not in self.categories:
            return False

        cid = str(stream.get("id") or "")
        if self.cameras and cid not in self.cameras:
            return False

        conf = float(detection.get("confidence") or 0.0)
        if conf < self.min_confidence:
            return False

        return True

    def _check_cooldown(self, detection: dict, stream: dict) -> bool:
        """True если можно отправлять (cooldown прошёл)."""
        if self.cooldown_sec <= 0:
            return True
        key = (
            str(detection.get("category") or ""),
            str(detection.get("label") or detection.get("plate") or ""),
            str(stream.get("id") or ""),
        )
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < self.cooldown_sec:
                return False
            self._last_sent[key] = now
            return True

    def _build_context(self, detection: dict, stream: dict) -> dict:
        category = str(detection.get("category") or "object").lower()
        label = detection.get("label") or detection.get("plate") or "?"
        plate = detection.get("plate") or ""
        conf = float(detection.get("confidence") or 0.0)

        if category == "plate" and plate:
            detail = f"Номер: `{plate}`"
            event_rus = f"Номер {plate}"
        else:
            detail = f"Метка: {label}"
            event_rus = f"{CATEGORY_RUS.get(category, category)} ({label})"

        return {
            "emoji": CATEGORY_EMOJI.get(category, "📦"),
            "category": category,
            "category_rus": CATEGORY_RUS.get(category, category),
            "label": label,
            "plate": plate,
            "event_rus": event_rus,
            "detail": detail,
            "camera_id": stream.get("id", ""),
            "camera_name": stream.get("name", ""),
            "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": f"{conf:.2f}",
            "confidence_pct": f"{conf * 100:.0f}",
        }

    def send(
        self, detection: dict, stream: dict, snapshot_path: Path | None = None
    ) -> tuple[bool, str | None]:
        """Отправляет одно событие. Возвращает (ok, error)."""
        ctx = self._build_context(detection, stream)
        text = _render_template(self.template, ctx)

        if self.type == "telegram":
            return self._send_telegram(text, snapshot_path)
        elif self.type == "max":
            return self._send_max(text, snapshot_path)
        elif self.type == "webhook":
            return self._send_webhook(text, detection, stream, snapshot_path)
        return False, f"неизвестный тип канала: {self.type}"

    # ------------------------------------------------------------------
    #  Telegram
    # ------------------------------------------------------------------
    def _send_telegram(self, text: str, snapshot_path: Path | None) -> tuple[bool, str | None]:
        if not self.token or not self.chat_id:
            return False, "telegram: token или chat_id не заданы"

        try:
            if self.attach_snapshot and snapshot_path and snapshot_path.exists():
                url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                with open(snapshot_path, "rb") as f:
                    r = requests.post(
                        url,
                        data={
                            "chat_id": self.chat_id,
                            "caption": text[:1024],
                            "parse_mode": "Markdown",
                        },
                        files={"photo": f},
                        timeout=self.timeout,
                    )
            else:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                r = requests.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text[:4096],
                        "parse_mode": "Markdown",
                    },
                    timeout=self.timeout,
                )

            if r.ok:
                return True, None
            try:
                desc = r.json().get("description", "")
            except Exception:
                desc = r.text[:200]
            return False, f"telegram HTTP {r.status_code}: {desc}"
        except requests.RequestException as e:
            return False, f"telegram: {e}"
        except Exception as e:
            return False, f"telegram unexpected: {e}"

    # ------------------------------------------------------------------
    #  MAX
    # ------------------------------------------------------------------
    def _send_max(self, text: str, snapshot_path: Path | None) -> tuple[bool, str | None]:
        if not self.max_token:
            return False, "max: max_token не задан"
        if not self.max_recipient_id:
            return False, "max: max_recipient_id не задан"

        headers = {
            "Authorization": self.max_token,
            "Content-Type": "application/json",
        }

        attachments: list[dict] = []

        # Шаг 1-2: если есть снапшот и attach_snapshot — загружаем картинку
        if self.attach_snapshot and snapshot_path and snapshot_path.exists():
            ok, token_or_err = self._max_upload_image(snapshot_path, headers)
            if ok:
                attachments.append(
                    {
                        "type": "image",
                        "payload": {"token": token_or_err},
                    }
                )
            else:
                # Не падаем: отправляем хотя бы текст, но фиксируем в логе
                log.warning(
                    "max[%s]: не удалось загрузить изображение — %s; отправляю только текст",
                    self.id,
                    token_or_err,
                )

        # Шаг 3: POST /messages с retry на attachment.not.ready
        params_key = "user_id" if self.max_recipient_type == "user" else "chat_id"
        params = {params_key: str(self.max_recipient_id)}

        body = {"text": text[:4000]}
        if attachments:
            body["attachments"] = attachments

        url = f"{MAX_API_BASE}/messages"

        # До 4 попыток с нарастающей паузой
        delays = [0.0, 1.0, 2.0, 4.0]
        last_err = None
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            try:
                r = requests.post(
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = f"max: {e}"
                continue

            if r.ok:
                return True, None

            # attachment.not.ready — повторяем
            err_text = r.text[:300]
            try:
                j = r.json()
                code = str(j.get("code") or "")
                if code == "attachment.not.ready":
                    last_err = "max: attachment.not.ready"
                    continue
            except Exception:
                pass

            return False, f"max HTTP {r.status_code}: {err_text}"

        return False, last_err or "max: не удалось отправить"

    def _max_upload_image(self, snapshot_path: Path, headers: dict) -> tuple[bool, str]:
        """
        Двухшаговая загрузка изображения в MAX.

        Возвращает (True, mediafile_token) или (False, error_text).

        Шаг 1: POST /uploads?type=image → {"url": "..."}
        Шаг 2: POST {url} multipart/form-data, data=@file
               → {"photos": {"photoIds": {"token": "..."}}}
        """
        # Шаг 1
        try:
            r = requests.post(
                f"{MAX_API_BASE}/uploads",
                params={"type": "image"},
                headers={"Authorization": self.max_token},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return False, f"uploads: {e}"

        if not r.ok:
            return False, f"uploads HTTP {r.status_code}: {r.text[:200]}"

        try:
            upload_url = r.json().get("url")
        except Exception:
            return False, "uploads: не JSON в ответе"

        if not upload_url:
            return False, "uploads: нет поля url в ответе"

        # Шаг 2
        try:
            with open(snapshot_path, "rb") as f:
                r2 = requests.post(
                    upload_url,
                    files={"data": f},
                    timeout=self.timeout,
                )
        except requests.RequestException as e:
            return False, f"upload file: {e}"

        if not r2.ok:
            return False, f"upload file HTTP {r2.status_code}: {r2.text[:200]}"

        # Токен может быть в разных местах — ищем рекурсивно
        try:
            data = r2.json()
        except Exception:
            return False, "upload file: не JSON в ответе"

        token = self._extract_max_token(data)
        if not token:
            return False, f"upload file: не найден token в ответе {str(data)[:200]}"

        return True, token

    @staticmethod
    def _extract_max_token(data) -> str | None:
        """
        Ищет mediafile_token в ответе MAX. Структура может быть:
          {"photos": {"photoIds": {"token": "..."}}}
        или вложенной глубже. Рекурсивный обход.
        """
        if isinstance(data, dict):
            if "token" in data and isinstance(data["token"], str):
                return data["token"]
            for v in data.values():
                t = MessageChannel._extract_max_token(v)
                if t:
                    return t
        elif isinstance(data, list):
            for v in data:
                t = MessageChannel._extract_max_token(v)
                if t:
                    return t
        return None

    # ------------------------------------------------------------------
    #  Webhook
    # ------------------------------------------------------------------
    def _send_webhook(
        self, text: str, detection: dict, stream: dict, snapshot_path: Path | None
    ) -> tuple[bool, str | None]:
        if not self.url:
            return False, "webhook: url не задан"

        payload = {
            "text": text,
            "channel_id": self.id,
            "event": {
                "category": detection.get("category"),
                "label": detection.get("label"),
                "plate": detection.get("plate"),
                "confidence": detection.get("confidence"),
                "bbox": detection.get("bbox"),
                "camera_id": stream.get("id"),
                "camera_name": stream.get("name"),
                "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "snapshot": str(snapshot_path) if snapshot_path else None,
        }

        try:
            if self.method == "POST":
                r = requests.post(
                    self.url, json=payload, headers=self.headers, timeout=self.timeout
                )
            else:
                r = requests.get(
                    self.url,
                    params={
                        "text": text,
                        **{
                            "category": detection.get("category"),
                            "camera": stream.get("id"),
                        },
                    },
                    headers=self.headers,
                    timeout=self.timeout,
                )

            if 200 <= r.status_code < 400:
                return True, None
            return False, f"webhook HTTP {r.status_code}"
        except requests.RequestException as e:
            return False, f"webhook: {e}"
        except Exception as e:
            return False, f"webhook unexpected: {e}"

    # ------------------------------------------------------------------
    def test(self) -> dict:
        """Тестовое сообщение (для UI). Возвращает {ok, error}."""
        fake_det = {
            "category": "plate",
            "label": "А000АА00",
            "plate": "А000АА00",
            "confidence": 0.99,
        }
        fake_stream = {
            "id": "test_camera",
            "name": "Тестовая камера",
        }
        ok, err = self.send(fake_det, fake_stream, None)
        return {"ok": ok, "error": err}


class MessageManager:
    def __init__(self, cfg: dict, log_dir: Path | None = None):
        self._lock = threading.Lock()
        self.channels: dict[str, MessageChannel] = {}
        self.log_path = (Path(log_dir) / "messages.log") if log_dir else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._apply_cfg(cfg)

    def _apply_cfg(self, cfg: dict):
        raw = cfg.get("channels")
        if not isinstance(raw, list):
            raw = []

        new: dict[str, MessageChannel] = {}
        for rc in raw:
            if not isinstance(rc, dict):
                continue
            rid = str(rc.get("id") or "").strip()
            if not rid:
                continue
            try:
                new[rid] = MessageChannel(rc)
            except Exception:
                log.exception(f"messaging[{rid}]: не удалось создать канал")

        self.channels = new
        n_enabled = sum(1 for c in self.channels.values() if c.enabled)
        log.info(f"MessageManager: каналов={len(self.channels)}, включено={n_enabled}")

    def reload_from_cfg(self, cfg: dict):
        with self._lock:
            self._apply_cfg(cfg)
        log.info("MessageManager: конфигурация перезагружена")

    def send_all(self, detection: dict, stream: dict, snapshot_path: Path | None = None) -> int:
        """Проходит по всем каналам, отправляет подходящим."""
        count = 0
        with self._lock:
            channels = list(self.channels.values())
        for ch in channels:
            try:
                if not ch.matches(detection, stream):
                    continue
                if not ch._check_cooldown(detection, stream):
                    continue
                ok, err = ch.send(detection, stream, snapshot_path)
                if ok:
                    count += 1
                    self._log_write(ch.id, ch.type, detection, stream, "sent", "")
                else:
                    self._log_write(ch.id, ch.type, detection, stream, "error", err or "")
                    log.warning(f"messaging[{ch.id}]: ошибка — {err}")
            except Exception:
                log.exception(f"messaging[{ch.id}]: ошибка send")
        return count

    def test_channel(self, channel_id: str) -> dict:
        with self._lock:
            ch = self.channels.get(channel_id)
        if ch is None:
            return {"ok": False, "error": f"канал '{channel_id}' не найден"}
        fake_det = {
            "category": "plate",
            "label": "А000АА00",
            "plate": "А000АА00",
            "confidence": 0.99,
        }
        fake_stream = {"id": "test_camera", "name": "Тестовая камера"}
        ok, err = ch.send(fake_det, fake_stream, None)
        if ok:
            self._log_write(ch.id, ch.type, fake_det, fake_stream, "test", "")
        return {"ok": ok, "error": err}

    def _log_write(
        self,
        channel_id: str,
        channel_type: str,
        detection: dict,
        stream: dict,
        action: str,
        details: str,
    ):
        if not self.log_path:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cat = detection.get("category") or "?"
        label = detection.get("label") or detection.get("plate") or "?"
        cam = stream.get("id") or "?"
        line = f"{ts}\t{channel_id}\t{channel_type}\t{cat}:{label}\t{cam}\t{action}\t{details}\n"
        try:
            with self._write_lock, open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            log.exception("messaging: не удалось записать в messages.log")
