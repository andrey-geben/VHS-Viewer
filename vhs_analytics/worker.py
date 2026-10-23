"""
Фоновый воркер: RTSP → (ALPR + ObjectDetector) → on_event.

Двухпоточная схема:
- Reader  : непрерывно читает RTSP, кладёт последний кадр в слот.
- Analyzer: раз в analyze_every_n кадров забирает свежий кадр из слота,
            гонит через все активные детекторы, применяет cooldown,
            вызывает on_event для каждого события.

Per-camera параметры:
- analyze_every_n — пропускать N-1 кадров из N (снижение нагрузки на CPU)
- object_imgsz    — размер входа YOLO (640/512/416/320)
"""

import logging
import threading
import time

import cv2

log = logging.getLogger(__name__)

COOLDOWN_BY_CATEGORY = {
    "plate": 15.0,
    "person": 30.0,
    "vehicle": 30.0,
    "animal": 60.0,
    "object": 60.0,
}


class _FrameSlot:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0

    def put(self, frame):
        with self._lock:
            self._frame = frame
            self._seq += 1

    def get(self, last_seq: int = 0):
        with self._lock:
            if self._seq == last_seq:
                return None, last_seq
            return self._frame, self._seq


class StreamWorker(threading.Thread):
    def __init__(
        self,
        stream: dict,
        detectors: dict,
        object_classes: list[str],
        on_event,
        analyze_every_n: int = 1,
        object_imgsz: int = 640,
        debug_frames: int = 5,
    ):
        super().__init__(daemon=True, name=f"worker-{stream['id']}")
        self.stream = stream
        self.detectors = detectors or {}
        self.object_classes = object_classes or []
        self.on_event = on_event
        self.analyze_every_n = max(1, int(analyze_every_n))
        self.object_imgsz = max(160, int(object_imgsz))
        self.debug_frames = max(0, int(debug_frames))
        self._stop_event = threading.Event()
        self._last_event_ts: dict[tuple[str, str], float] = {}

    def stop(self):
        self._stop_event.set()

    def _open_capture(self):
        cap = cv2.VideoCapture(self.stream["rtsp_url"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)
        return cap

    def _apply_cooldown(self, detections: list[dict]) -> list[dict]:
        now = time.time()
        out: list[dict] = []
        for d in detections:
            cat = d.get("category", "object")
            label = d.get("label", "?")
            key = (cat, label)
            cd = COOLDOWN_BY_CATEGORY.get(cat, 30.0)
            last = self._last_event_ts.get(key, 0.0)
            if now - last < cd:
                continue
            self._last_event_ts[key] = now
            out.append(d)
        return out

    def _reader_loop(self, slot: _FrameSlot):
        sid = self.stream["id"]
        url = self.stream["rtsp_url"]
        backoff, max_backoff = 1.0, 30.0
        read_count = 0

        while not self._stop_event.is_set():
            log.info(f"[{sid}] reader connect {url}")
            cap = self._open_capture()
            if not cap.isOpened():
                cap.release()
                log.warning(f"[{sid}] поток недоступен, retry через {backoff:.0f}с")
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, max_backoff)
                continue

            backoff = 1.0
            log.info(f"[{sid}] поток открыт")
            try:
                while not self._stop_event.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        log.warning(f"[{sid}] кадр не получен — реконнект")
                        break
                    read_count += 1
                    slot.put(frame)
                    if read_count <= self.debug_frames:
                        log.info(f"[{sid}] frame #{read_count} shape={frame.shape}")
            finally:
                cap.release()

        log.info(f"[{sid}] reader stopped")

    def _run_all_detectors(self, frame) -> list[dict]:
        out: list[dict] = []

        alpr = self.detectors.get("alpr")
        if alpr is not None:
            try:
                out.extend(alpr.detect(frame))
            except Exception:
                log.exception(f"[{self.stream['id']}] ALPR error")

        obj = self.detectors.get("object")
        if obj is not None:
            try:
                out.extend(
                    obj.detect(
                        frame,
                        classes=self.object_classes,
                        imgsz=self.object_imgsz,
                    )
                )
            except Exception:
                log.exception(f"[{self.stream['id']}] object error")

        return out

    def _analyzer_loop(self, slot: _FrameSlot):
        sid = self.stream["id"]
        last_seq = 0
        counter = 0
        detections_count = 0
        t_start = time.time()
        t_last_report = t_start
        first_frame_logged = False

        while not self._stop_event.is_set():
            frame, last_seq = slot.get(last_seq)
            if frame is None:
                if self._stop_event.wait(0.01):
                    break
                continue

            if not first_frame_logged:
                log.info(
                    f"[{sid}] analyzer got first frame "
                    f"(every={self.analyze_every_n}, object_imgsz={self.object_imgsz})"
                )
                first_frame_logged = True

            counter += 1
            if counter % self.analyze_every_n != 0:
                continue

            detections = self._run_all_detectors(frame)
            detections = self._apply_cooldown(detections)

            if detections:
                detections_count += len(detections)
                log.info(f"[{sid}] detections (после cooldown): {detections}")
                for det in detections:
                    try:
                        self.on_event(self.stream, det, frame)
                    except Exception:
                        log.exception(f"[{sid}] on_event error")

            now = time.time()
            if now - t_last_report >= 15.0:
                elapsed = now - t_start
                fps = counter / elapsed if elapsed > 0 else 0.0
                log.info(
                    f"[{sid}] analyzer: {counter} кадров за {elapsed:.0f}с "
                    f"({fps:.1f} fps), событий: {detections_count}"
                )
                t_last_report = now

        log.info(f"[{sid}] analyzer stopped")

    def run(self):
        slot = _FrameSlot()
        reader = threading.Thread(
            target=self._reader_loop,
            args=(slot,),
            daemon=True,
            name=f"reader-{self.stream['id']}",
        )
        analyzer = threading.Thread(
            target=self._analyzer_loop,
            args=(slot,),
            daemon=True,
            name=f"analyzer-{self.stream['id']}",
        )
        reader.start()
        analyzer.start()

        while not self._stop_event.is_set():
            self._stop_event.wait(0.5)

        reader.join(timeout=5)
        analyzer.join(timeout=5)
        log.info(f"[{self.stream['id']}] worker stopped")
