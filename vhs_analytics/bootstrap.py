"""
Сборка AnalyticsManager + EventWriter + Relay + Messaging.

Порядок в on_event:
1. EventWriter — всегда сохраняет событие в alpr_events.jsonl + снапшот.
2. RelayManager — только для category='plate'. Каждое реле проверяет
   свой whitelist, свой список камер, свой cooldown.
3. MessageManager — отправляет сообщение во все подходящие каналы
   (Telegram, webhook) с учётом фильтров категорий/камер/confidence
   и cooldown. Снапшот прикрепляется, если у канала attach_snapshot=true.

Backward compat: если в YAML нет messaging.channels[], но есть старая
плоская секция telegram: (token + chat_id) — используется старый
TelegramNotifier как fallback, чтобы не сломать существующие конфиги.
"""

import logging
from pathlib import Path

import yaml

from .archive import ArchiveManager
from .config import load_config
from .event_writer import EventWriter
from .manager import AnalyticsManager
from .messaging import MessageManager
from .relay import RelayManager

log = logging.getLogger(__name__)


def _load_root_section(config_path: str, section: str) -> dict:
    """
    Читает секцию верхнего уровня из YAML (relay, messaging, telegram).
    config.py::load_config возвращает только analytics, а relay/messaging
    живут в корне — поэтому читаем отдельно.
    """
    log.info("DIAG: читаю секцию '%s' из %s", section, config_path)
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            log.warning("DIAG: YAML не словарь (%s)", type(raw).__name__)
            return {}
        if section not in raw:
            log.warning("DIAG: секция '%s' отсутствует в YAML", section)
            return {}
        r = raw.get(section)
        if not isinstance(r, dict):
            log.warning("DIAG: '%s' не словарь (%s)", section, type(r).__name__)
            return {}
        return r
    except FileNotFoundError:
        log.error("DIAG: файл не найден: %s", config_path)
        return {}
    except Exception:
        log.exception("DIAG: не удалось прочитать '%s'", section)
        return {}


def _load_root_streams(config_path: str) -> list:
    """Потоки нужны архиву для режима непрерывной записи."""
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        streams = raw.get("streams") if isinstance(raw, dict) else None
        return [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []
    except Exception:
        log.exception("DIAG: не удалось прочитать streams для архива")
        return []


def build_manager(
    config_path: str = "viewer_config.yaml",
    log_dir: str | Path = "logs",
) -> AnalyticsManager | None:
    log_dir = Path(log_dir)
    cfg = load_config(config_path)  # только analytics.*
    if not cfg.get("enabled"):
        log.info("analytics disabled in config")
        return None

    # --- EventWriter ---
    writer = EventWriter(log_dir=log_dir)

    # --- Видеоархив (Viewer) ---
    archive_cfg = _load_root_section(config_path, "archive")
    archive_mgr = ArchiveManager(
        archive_cfg,
        base_dir=Path(config_path).resolve().parent,
        streams=_load_root_streams(config_path),
        log_dir=log_dir,
    )

    # Реле и сообщения перенесены в VHS-Security.
    # Viewer оставлен источником событий/аналитики и не выполняет реакции.
    relay_mgr = None
    msg_mgr = None
    legacy_notifier = None
    log.info("Reactions moved to VHS-Security; relay/messaging disabled in Viewer")

    def on_event(stream: dict, detection: dict, frame=None):
        category = detection.get("category", "object")
        label = detection.get("label") or detection.get("plate") or "?"
        plate = detection.get("plate")
        conf = float(detection.get("confidence", 0.0))
        camera_id = stream.get("id", "")
        camera_name = stream.get("name", "")

        # 1. EventWriter — всегда. Запоминаем путь к снапшоту.
        snapshot_abs: Path | None = None
        if frame is not None:
            try:
                snapshot_rel = writer.add(stream, detection, frame)
                if snapshot_rel:
                    snapshot_abs = log_dir / snapshot_rel
            except Exception:
                log.exception("EventWriter.add failed")

        # 2. Видеоархив — независимо от реакций Security.
        if archive_mgr is not None:
            try:
                archive_mgr.on_event(stream, detection)
            except Exception:
                log.exception("ArchiveManager.on_event failed")

        # 3. Relay — только для номеров
        if relay_mgr is not None and category == "plate" and plate:
            try:
                triggered = relay_mgr.check_all(
                    plate,
                    camera_id,
                    camera_name,
                )
                if triggered > 0:
                    log.info("Relay: сработало %d реле для %s", triggered, plate)
            except Exception:
                log.exception("RelayManager.check_all failed")

        # 3. Messaging (новый) или legacy Telegram
        if msg_mgr is not None:
            try:
                sent = msg_mgr.send_all(
                    detection,
                    stream,
                    snapshot_path=snapshot_abs,
                )
                if sent > 0:
                    log.info("Messaging: отправлено в %d канал(ов)", sent)
            except Exception:
                log.exception("MessageManager.send_all failed")
        elif legacy_notifier is not None:
            # Старый формат сообщения
            emoji_map = {
                "plate": "🚗",
                "person": "👤",
                "vehicle": "🚙",
                "animal": "🐻",
                "object": "📦",
            }
            emoji = emoji_map.get(category, "📦")
            if category == "plate" and plate:
                msg = f"{emoji} Номер `{plate}` на *{camera_name}* (conf={conf:.2f})"
            else:
                rus_map = {
                    "person": "Человек",
                    "vehicle": "Транспорт",
                    "animal": "Животное",
                    "object": "Объект",
                }
                rus = rus_map.get(category, category.capitalize())
                msg = f"{emoji} {rus} ({label}) на *{camera_name}* (conf={conf:.2f})"
            try:
                legacy_notifier.send(msg)
            except Exception:
                log.exception("legacy Telegram send failed")

        # 4. Лог (краткий)
        log.info(
            "Event: %s %s на *%s* (conf=%.2f)",
            category,
            label,
            camera_name,
            conf,
        )

    manager = AnalyticsManager(
        cfg,
        on_event=on_event,
        config_path=config_path,
    )
    manager._writer = writer
    manager._archive = archive_mgr
    manager._relay = relay_mgr
    manager._messaging = msg_mgr
    return manager


def stop_manager(manager):
    if manager is None:
        return
    for attr in ("_archive", "_relay", "_messaging"):
        svc = getattr(manager, attr, None)
        if svc is not None and hasattr(svc, "stop"):
            try:
                svc.stop()
            except Exception:
                log.exception(f"{attr}.stop failed")
    manager.stop()
