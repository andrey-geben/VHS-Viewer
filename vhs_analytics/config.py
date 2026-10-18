"""
Загрузка и валидация конфигурации аналитики из viewer_config.yaml.

Per-camera настройки (analytics.cameras.<sid>):
    enabled         — вкл/выкл всю аналитику камеры
    plates          — искать номера (ALPR)
    objects         — список YOLO-классов
    analyze_every_n — пропускать N-1 кадров из N
    object_imgsz    — размер входа YOLO (640/512/416/320)

Глобальные (analytics.<name>):
    objects_per_camera — true → свой ObjectDetector на камеру (параллелизм)
                         false → один общий (экономия RAM)
"""

import logging
import os
import yaml

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "autostart": True,
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
    "model_path": None,
    "cameras": {},
    "telegram": {"enabled": False, "token": "", "chat_id": ""},
}

STREAM_DEFAULTS = {
    "enabled": True,
    "plates": True,
    "objects": [],
    "analyze_every_n": 1,
    "object_imgsz": 640,
}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_path: str = "viewer_config.yaml") -> dict:
    if not os.path.exists(config_path):
        log.warning(f"конфиг не найден: {config_path}, analytics disabled")
        return dict(DEFAULTS)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    section = raw.get("analytics", {}) or {}
    merged = _merge(DEFAULTS, section)

    if "conf_threshold" in section and "plate_conf" not in section:
        merged["plate_conf"] = section["conf_threshold"]

    if not isinstance(merged.get("cameras"), dict):
        merged["cameras"] = {}

    return merged


def get_camera_config(cfg: dict, camera_id: str) -> dict:
    """
    Возвращает настройки камеры:
    STREAM_DEFAULTS → cameras.default → cameras.<camera_id>.
    """
    cameras = cfg.get("cameras") or {}
    default = _merge(STREAM_DEFAULTS, cameras.get("default", {}))
    override = cameras.get(camera_id, {}) or {}
    merged = _merge(default, override)

    if "analyze_every_n" not in merged or merged["analyze_every_n"] is None:
        merged["analyze_every_n"] = cfg.get("analyze_every_n", 1)
    if "object_imgsz" not in merged or merged["object_imgsz"] is None:
        merged["object_imgsz"] = cfg.get("object_imgsz", 640)

    return merged