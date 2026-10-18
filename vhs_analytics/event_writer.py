"""Запись событий (ALPR + объекты) в JSONL + снапшоты."""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

log = logging.getLogger(__name__)

CATEGORY_COLORS = {
    "plate": (0, 200, 0),
    "person": (0, 165, 255),
    "vehicle": (255, 100, 0),
    "animal": (0, 0, 220),
    "object": (200, 200, 200),
}


class EventWriter:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "alpr_events.jsonl"
        self.snapshot_dir = self.log_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        log.info(
            f"EventWriter: jsonl={self.jsonl_path}, "
            f"snapshots={self.snapshot_dir}"
        )

    def add(self, stream: dict, detection: dict, frame) -> str | None:
        ts = time.time()
        iso = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        camera_id = stream.get("id", "unknown")
        camera_name = stream.get("name", camera_id)
        category = detection.get("category") or (
            "plate" if detection.get("plate") else "object"
        )
        label = detection.get("label") or detection.get("plate") or "?"
        plate = detection.get("plate")
        conf = float(detection.get("confidence", 0.0))

        snapshot_rel = None
        if frame is not None:
            try:
                annotated = frame.copy()
                bbox = detection.get("bbox")
                if bbox:
                    x, y, w, h = map(int, bbox)
                    color = CATEGORY_COLORS.get(category, (200, 200, 200))
                    cv2.rectangle(
                        annotated, (x, y), (x + w, y + h), color, 3
                    )
                    cv2.putText(
                        annotated, label, (x, max(24, y - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2,
                    )
                safe = "".join(c for c in label if c.isalnum()) or "x"
                fname = f"{int(ts * 1000)}_{camera_id}_{safe}.jpg"
                fpath = self.snapshot_dir / fname
                cv2.imwrite(
                    str(fpath), annotated,
                    [cv2.IMWRITE_JPEG_QUALITY, 85],
                )
                snapshot_rel = f"snapshots/{fname}"
            except Exception:
                log.exception("не удалось сохранить снапшот")

        event = {
            "ts": ts,
            "iso": iso,
            "camera_id": camera_id,
            "camera_name": camera_name,
            "category": category,
            "label": label,
            "plate": plate,
            "confidence": round(conf, 3),
            "snapshot": snapshot_rel,
        }

        with self._lock:
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception:
                log.exception("не удалось записать событие в JSONL")

        return snapshot_rel