"""
Обёртка над ALPR из VHS2.

Принимает общий AlprPipeline (один на всё приложение) — модели ONNX
грузятся один раз, а per-camera состояние (буфер, голосование, cooldown)
остаётся внутри каждого экземпляра VHS2AlprDetector.

Параметры голосования подобраны под наш двухпоточный worker:
analyzer даёт ~9 fps, VHS2 пропускает каждый 2-й кадр (~4.5 инференса/сек),
окно 3 сек = ~13 попыток набрать 2 голоса — гораздо надёжнее дефолтных
process_every=5 / window=2 / min_votes=3.
"""

import logging

from .vhs2_alpr import AlprDetector as VHS2AlprDetector
from .vhs2_alpr import AlprPipeline

log = logging.getLogger(__name__)


class ALPRDetector:
    def __init__(
        self,
        camera_id: str = "default",
        conf_threshold: float = 0.5,
        pipeline: AlprPipeline = None,
        process_every_n_frames: int = 2,
        voting_window_sec: float = 3.0,
        min_votes: int = 2,
    ):
        self._detector = VHS2AlprDetector(
            camera_id=camera_id,
            detection_confidence=conf_threshold,
            pipeline=pipeline,
            process_every_n_frames=process_every_n_frames,
            voting_window_sec=voting_window_sec,
            min_votes=min_votes,
            cooldown_same_plate_sec=15.0,  # было 30 — не спамим, но и не ждём долго
            cooldown_any_sec=2.0,
        )
        mode = "shared pipeline" if pipeline is not None else "own pipeline"
        log.info(
            f"ALPRDetector (VHS2) init camera={camera_id} ({mode}, "
            f"every={process_every_n_frames}, window={voting_window_sec}s, "
            f"min_votes={min_votes})"
        )

    def detect(self, frame) -> list[dict]:
        """
        Возвращает список в едином формате:
        [{"category": "plate", "label": "А123ВС77", "plate": "А123ВС77",
          "bbox": (x,y,w,h), "confidence": 0.87}, ...]
        """
        try:
            raw = self._detector.detect(frame)
        except Exception:
            log.exception("ALPR error")
            return []

        results = []
        for det in raw:
            plate = det.get("plate_text")
            if not plate:
                continue
            results.append(
                {
                    "category": "plate",
                    "label": plate,
                    "plate": plate,
                    "bbox": det.get("bbox"),
                    "confidence": det.get("confidence", 0.0),
                }
            )
        return results

    def annotate_frame(self, frame):
        return self._detector.annotate_frame(frame)
