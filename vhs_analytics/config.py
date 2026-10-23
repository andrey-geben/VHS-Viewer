"""
Загрузка и валидация конфигурации аналитики из viewer_config.yaml.

Поддерживает auto_tune: если analytics.auto_tune=true (по умолчанию),
при загрузке вызывается hardware.detect_hardware().recommend() и
переопределяются глобальные параметры:
    objects_per_camera, analyze_every_n, object_imgsz, intra_op_num_threads

Per-camera override в analytics.cameras.<sid> имеет приоритет над
глобальными — так пользователь может точечно подстроить конкретную камеру.

Кэш профиля железа доступен в cfg["_hardware"] (dict) для UI.
"""

import logging
import os

import yaml

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "autostart": True,
    "auto_tune": True,
    "streamer_url": "http://localhost:8080",
    "rtsp_base": None,
    "analyze_every_n": 1,
    "poll_interval": 15,
    "conf_threshold": 0.5,
    "plate_conf": 0.5,
    "object_conf": 0.4,
    "object_model": "yolov8n.pt",
    "object_imgsz": 640,
    "objects_per_camera": True,
    "intra_op_num_threads": None,
    "model_path": None,
    "cameras": {},
    "telegram": {"enabled": False, "token": "", "chat_id": ""},
}

STREAM_DEFAULTS = {
    "enabled": True,
    "plates": True,
    "objects": [],
    "analyze_every_n": None,
    "object_imgsz": None,
}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_auto_tune(cfg: dict) -> dict:
    """Переопределяет глобальные параметры рекомендациями железа."""
    if not cfg.get("auto_tune", True):
        log.info("auto_tune: выключен, использую параметры из YAML")
        return cfg

    try:
        from .hardware import detect_hardware

        hw = detect_hardware()
        rec = hw.recommend()
        log.info("auto_tune: применяю рекомендации железа: %s", rec)

        cfg["objects_per_camera"] = rec["objects_per_camera"]
        cfg["analyze_every_n"] = rec["analyze_every_n"]
        cfg["object_imgsz"] = rec["object_imgsz"]
        # intra_op: если пользователь явно не задал — берём рекомендацию
        if not cfg.get("intra_op_num_threads"):
            cfg["intra_op_num_threads"] = rec["intra_op_num_threads"]
        cfg["_hardware"] = hw.to_dict()
    except Exception:
        log.exception("auto_tune: не удалось применить рекомендации")
        cfg.setdefault("_hardware", {})

    return cfg


def load_config(config_path: str = "viewer_config.yaml") -> dict:
    if not os.path.exists(config_path):
        log.warning(f"конфиг не найден: {config_path}, analytics disabled")
        merged = dict(DEFAULTS)
        return _apply_auto_tune(merged)

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    section = raw.get("analytics", {}) or {}
    merged = _merge(DEFAULTS, section)

    if "conf_threshold" in section and "plate_conf" not in section:
        merged["plate_conf"] = section["conf_threshold"]

    if not isinstance(merged.get("cameras"), dict):
        merged["cameras"] = {}

    merged = _apply_auto_tune(merged)
    return merged


def get_camera_config(cfg: dict, camera_id: str) -> dict:
    """
    Возвращает настройки камеры:
    STREAM_DEFAULTS → cameras.default → cameras.<camera_id> →
    глобальные фоллбэки (analyze_every_n, object_imgsz).
    """
    cameras = cfg.get("cameras") or {}
    default = _merge(STREAM_DEFAULTS, cameras.get("default", {}))
    override = cameras.get(camera_id, {}) or {}
    merged = _merge(default, override)

    if merged.get("analyze_every_n") is None:
        merged["analyze_every_n"] = cfg.get("analyze_every_n", 1)
    if merged.get("object_imgsz") is None:
        merged["object_imgsz"] = cfg.get("object_imgsz", 640)

    return merged
