"""
Multi-class детектор на YOLOv8 (ultralytics, COCO-80).

Возвращает события с двумя полями:
    label    — английское имя класса из COCO ("dog", "bear", ...)
    label_ru — русское название для UI ("собака", "медведь", ...)

Использование:
    det = ObjectDetector(model_name="yolov8n.pt", conf_threshold=0.4)
    det._ensure_loaded()
    results = det.detect(frame, classes=["person", "dog", "bear"])
    # → [{"category": "animal", "label": "dog", "label_ru": "собака",
    #     "bbox": (x,y,w,h), "confidence": 0.87}, ...]
"""

import logging
import threading

from .labels_ru import label_ru

log = logging.getLogger(__name__)


NAME_TO_CATEGORY = {
    "person": "person",
    "bicycle": "vehicle",
    "car": "vehicle",
    "motorcycle": "vehicle",
    "bus": "vehicle",
    "truck": "vehicle",
    "train": "vehicle",
    "bird": "animal",
    "cat": "animal",
    "dog": "animal",
    "horse": "animal",
    "sheep": "animal",
    "cow": "animal",
    "elephant": "animal",
    "bear": "animal",
    "zebra": "animal",
    "giraffe": "animal",
}


class ObjectDetector:
    def __init__(
        self,
        model_name: str = "yolov8n.pt",
        conf_threshold: float = 0.4,
        default_imgsz: int = 640,
    ):
        self.model_name = model_name
        self.conf_threshold = conf_threshold
        self.default_imgsz = default_imgsz
        self._model = None
        self._loaded = False
        self._available = False
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True

        try:
            from ultralytics import YOLO  # noqa: F401
        except ImportError:
            log.warning(
                "ultralytics не установлен — детекция объектов отключена. "
                "Установите: pip install ultralytics"
            )
            return

        log.info(f"Загружаю YOLO-модель {self.model_name}...")
        try:
            from ultralytics import YOLO

            self._model = YOLO(self.model_name)
            self._available = True
            log.info(f"YOLO {self.model_name} готова, классов: {len(self._model.names)}")
        except Exception:
            log.exception("не удалось загрузить YOLO-модель")

    def is_available(self) -> bool:
        self._ensure_loaded()
        return self._available

    def detect(
        self,
        frame,
        classes: list[str] = None,
        imgsz: int = None,
    ) -> list[dict]:
        """
        Возвращает список:
        [{"category": "animal", "label": "dog", "label_ru": "собака",
          "bbox": (x, y, w, h), "confidence": 0.92}, ...]

        classes: список английских имён классов (dog, bear, person, ...).
        imgsz:   переопределяет default_imgsz для этого вызова.
        """
        self._ensure_loaded()
        if not self._available or self._model is None:
            return []

        use_imgsz = int(imgsz) if imgsz else self.default_imgsz

        try:
            with self._lock:
                results = self._model.predict(
                    frame,
                    conf=self.conf_threshold,
                    imgsz=use_imgsz,
                    verbose=False,
                )
        except Exception:
            log.exception("YOLO predict error")
            return []

        allowed = set(classes) if classes else None
        out: list[dict] = []

        for r in results:
            names = r.names or {}
            for box in r.boxes:
                cls_id = int(box.cls[0])
                cls_name = names.get(cls_id, str(cls_id))
                if allowed is not None and cls_name not in allowed:
                    continue
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                out.append(
                    {
                        "category": NAME_TO_CATEGORY.get(cls_name, "object"),
                        "label": cls_name,
                        "label_ru": label_ru(cls_name),
                        "bbox": (int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
                        "confidence": round(conf, 3),
                    }
                )
        return out
