"""
Синхронизация списка потоков из VHS-Streamer с воркерами аналитики.

Per-camera параметры (analytics.cameras.<sid>):
    enabled, plates, objects, analyze_every_n, object_imgsz

Глобально:
    objects_per_camera: true → ObjectDetector на каждую камеру (параллелизм)
                        false → один общий под lock (экономия RAM)

ALPR pipeline всегда общий — ONNX-модели (YOLOv9 + RapidOCR) тяжелее
YOLOv8n, и обычно ALPR нужен только на 1–2 камерах.
"""

import logging
import threading

import yaml

from .stream_client import StreamerClient
from .detector import ALPRDetector
from .object_detector import ObjectDetector
from .worker import StreamWorker
from .vhs2_alpr import AlprPipeline
from .config import get_camera_config

log = logging.getLogger(__name__)


class AnalyticsManager:
    def __init__(
        self,
        config: dict,
        on_event,
        config_path: str = "viewer_config.yaml",
    ):
        self.cfg = config
        self.config_path = config_path
        self.client = StreamerClient(
            base_url=config["streamer_url"],
            rtsp_base=config.get("rtsp_base"),
        )
        self.conf_threshold = config.get(
            "plate_conf", config.get("conf_threshold", 0.5)
        )
        self.object_conf = config.get("object_conf", 0.4)
        self.object_model = config.get("object_model", "yolov8n.pt")
        self.default_object_imgsz = config.get("object_imgsz", 640)
        self.objects_per_camera = bool(config.get("objects_per_camera", True))
        self.poll_interval = config.get("poll_interval", 15)
        self.debug_frames = config.get("debug_frames", 5)
        self.on_event = on_event

        self._workers: dict[str, StreamWorker] = {}
        self._alpr_detectors: dict[str, ALPRDetector] = {}

        # objects_per_camera=true  → self._object_dets[sid] на камеру
        # objects_per_camera=false → единственный self._object_det
        self._object_dets: dict[str, ObjectDetector] = {}
        self._object_det: ObjectDetector | None = None

        self._pipeline: AlprPipeline | None = None
        self._pipeline_lock = threading.Lock()

        self._last_decisions: dict[str, str] = {}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _get_or_create_pipeline(self) -> AlprPipeline:
        with self._pipeline_lock:
            if self._pipeline is not None:
                return self._pipeline
            log.info("Создаю общий AlprPipeline...")
            self._pipeline = AlprPipeline(
                detection_confidence=self.conf_threshold,
            )
            self._pipeline._ensure_loaded()  # noqa: SLF001
            log.info("Общий AlprPipeline готов")
            return self._pipeline

    def _build_object_detector(self, sid: str | None = None) -> ObjectDetector:
        label = f" [{sid}]" if sid else " (общий)"
        log.info(f"Создаю ObjectDetector{label} ({self.object_model})...")
        det = ObjectDetector(
            model_name=self.object_model,
            conf_threshold=self.object_conf,
            default_imgsz=self.default_object_imgsz,
        )
        det._ensure_loaded()  # noqa: SLF001
        if det.is_available():
            log.info(f"ObjectDetector{label} готов")
        else:
            log.warning(f"ObjectDetector{label} недоступен (нет ultralytics?)")
        return det

    def _get_or_create_object_detector(self, sid: str) -> ObjectDetector:
        with self._pipeline_lock:
            if self.objects_per_camera:
                if sid not in self._object_dets:
                    self._object_dets[sid] = self._build_object_detector(sid)
                return self._object_dets[sid]
            else:
                if self._object_det is None:
                    self._object_det = self._build_object_detector(None)
                return self._object_det

    def _drop_object_detector(self, sid: str):
        with self._pipeline_lock:
            self._object_dets.pop(sid, None)

    def _load_cameras_section(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            section = raw.get("analytics") or {}
            cameras = section.get("cameras") or {}
            return cameras if isinstance(cameras, dict) else {}
        except Exception:
            log.exception("не удалось перечитать cameras из конфига")
            return self.cfg.get("cameras") or {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="analytics-manager"
        )
        self._thread.start()
        log.info(
            f"AnalyticsManager started "
            f"(objects_per_camera={self.objects_per_camera})"
        )

    def stop(self):
        self._stop.set()
        for w in list(self._workers.values()):
            w.stop()
        for w in list(self._workers.values()):
            w.join(timeout=5)
        self._workers.clear()
        self._alpr_detectors.clear()
        self._object_dets.clear()
        self._object_det = None
        if self._thread:
            self._thread.join(timeout=5)
        log.info("AnalyticsManager stopped")

    def _run(self):
        while not self._stop.is_set():
            try:
                self.cfg["cameras"] = self._load_cameras_section()
                streams = self.client.get_streams()
                self._reconcile(streams)
            except Exception:
                log.exception("manager poll error")
            self._stop.wait(self.poll_interval)

    def _log_decision(self, sid: str, decision: str):
        if self._last_decisions.get(sid) != decision:
            log.info(f"[{sid}] config: {decision}")
            self._last_decisions[sid] = decision

    def _worker_matches(self, worker: StreamWorker, wanted: dict) -> bool:
        if worker.analyze_every_n != int(wanted.get("analyze_every_n", 1)):
            return False
        if worker.object_imgsz != int(wanted.get("object_imgsz", 640)):
            return False
        if sorted(worker.object_classes) != sorted(wanted.get("object_classes") or []):
            return False
        wants_alpr = bool(wanted.get("plates"))
        has_alpr = "alpr" in worker.detectors
        if wants_alpr != has_alpr:
            return False
        return True

    def _reconcile(self, streams: list[dict]):
        wanted: dict[str, dict] = {}

        for s in streams:
            sid = s["id"]
            cc = get_camera_config(self.cfg, sid)
            enabled = bool(cc.get("enabled"))
            plates = bool(cc.get("plates"))
            objects = cc.get("objects") or []
            every_n = max(1, int(cc.get("analyze_every_n", 1)))
            object_imgsz = int(cc.get("object_imgsz", 640))

            if not enabled:
                self._log_decision(sid, "enabled=false → skip")
                continue
            if not plates and not objects:
                self._log_decision(
                    sid,
                    "enabled=true, но plates=false и objects пусто → skip",
                )
                continue

            self._log_decision(
                sid,
                f"enabled=true, plates={plates}, objects={objects or '—'}, "
                f"every={every_n}, imgsz={object_imgsz}",
            )
            wanted[sid] = {
                "stream": s,
                "config": cc,
                "plates": plates,
                "object_classes": list(objects) if objects else [],
                "analyze_every_n": every_n,
                "object_imgsz": object_imgsz,
            }

        # 1. Останавливаем воркеры, которых больше не должно быть
        for sid in list(self._workers.keys()):
            if sid not in wanted:
                log.info(f"stream {sid} — больше не активен, стоп воркера")
                self._workers[sid].stop()
                del self._workers[sid]
                self._alpr_detectors.pop(sid, None)
                self._drop_object_detector(sid)

        # 2. Перезапускаем воркеры с изменившимися параметрами
        for sid, item in wanted.items():
            existing = self._workers.get(sid)
            if existing is not None and existing.is_alive():
                if self._worker_matches(existing, item):
                    continue
                log.info(f"stream {sid} — параметры изменились, перезапуск воркера")
                existing.stop()
                del self._workers[sid]

        # 3. Запускаем недостающие
        for sid, item in wanted.items():
            if sid in self._workers:
                continue

            detectors: dict = {}

            if item["plates"]:
                if sid not in self._alpr_detectors:
                    self._alpr_detectors[sid] = ALPRDetector(
                        camera_id=sid,
                        conf_threshold=self.conf_threshold,
                        pipeline=self._get_or_create_pipeline(),
                    )
                detectors["alpr"] = self._alpr_detectors[sid]

            object_classes = item["object_classes"]
            if object_classes:
                obj = self._get_or_create_object_detector(sid)
                if obj.is_available():
                    detectors["object"] = obj
                else:
                    log.warning(
                        f"[{sid}] objects заданы, но ObjectDetector недоступен"
                    )
                    object_classes = []

            worker = StreamWorker(
                stream=item["stream"],
                detectors=detectors,
                object_classes=object_classes,
                on_event=self.on_event,
                analyze_every_n=item["analyze_every_n"],
                object_imgsz=item["object_imgsz"],
                debug_frames=self.debug_frames,
            )
            worker.start()
            self._workers[sid] = worker
            log.info(
                f"stream {sid} — worker started "
                f"(plates={item['plates']}, objects={object_classes or '—'}, "
                f"every={item['analyze_every_n']}, imgsz={item['object_imgsz']})"
            )