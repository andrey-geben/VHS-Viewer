"""
ALPR-пайплайн: детектор номеров + RapidOCR + постобработка.

Содержит два класса:
- AlprPipeline — низкоуровневый пайплайн (детекция + OCR + постобработка
  за один кадр);
- AlprDetector — обёртка с буфером, голосованием по нескольким кадрам
  и cooldown'ами.

Производительность:
- ONNX Runtime для OCR настраивается через параметр `ocr_intra_op_threads`
  в конструкторе AlprPipeline. Значение приходит из config.py
  (auto_tune подбирает по числу физических ядер CPU) или из YAML.
- inter_op всегда 1 — минимум накладных расходов на переключение контекста.
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Константы
# ---------------------------------------------------------------------- #
PLATE_REGEX = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}(\d{2,3})?$")

LAT_TO_CYR = {
    "A": "А",
    "B": "В",
    "E": "Е",
    "K": "К",
    "M": "М",
    "H": "Н",
    "O": "О",
    "P": "Р",
    "C": "С",
    "T": "Т",
    "X": "Х",
    "Y": "У",
}

GARBAGE_PREFIXES = ("RUS", "РУС")

# Значение по умолчанию, если в конструктор не передан ocr_intra_op_threads.
# Обычно переопределяется из config.py (auto_tune).
OCR_INTRA_OP_THREADS = 4
OCR_INTER_OP_THREADS = 1


# ---------------------------------------------------------------------- #
#  Утилиты текста
# ---------------------------------------------------------------------- #
def normalize(text: str) -> str:
    text = re.sub(r"[^А-ЯA-Z0-9]", "", text.upper())
    for lat, cyr in LAT_TO_CYR.items():
        text = text.replace(lat, cyr)
    return text


def fix_letter_digit_confusion(text: str) -> str:
    if len(text) < 6:
        return text
    chars = list(text)

    def to_letter(ch):
        return "О" if ch == "0" else ch

    def to_digit(ch):
        return "0" if ch == "О" else ch

    chars[0] = to_letter(chars[0])
    for i in (1, 2, 3):
        if i < len(chars):
            chars[i] = to_digit(chars[i])
    if len(chars) > 4:
        chars[4] = to_letter(chars[4])
    if len(chars) > 5:
        chars[5] = to_letter(chars[5])
    for i in range(6, len(chars)):
        chars[i] = to_digit(chars[i])
    return "".join(chars)


def is_valid_plate(text: str) -> bool:
    return bool(PLATE_REGEX.match(text))


# ---------------------------------------------------------------------- #
#  Предобработка кропа перед OCR
# ---------------------------------------------------------------------- #
def preprocess_crop(
    crop: np.ndarray, target_height: int = 96, use_threshold: bool = False
) -> np.ndarray:
    if crop is None or crop.size == 0:
        return crop

    h, w = crop.shape[:2]

    if h < target_height:
        scale = target_height / max(1, h)
        new_w = int(w * scale)
        crop = cv2.resize(crop, (new_w, target_height), interpolation=cv2.INTER_CUBIC)

    if len(crop.shape) == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop

    try:
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    except Exception:
        pass

    if use_threshold:
        try:
            _, binary = cv2.threshold(
                gray,
                0,
                255,
                cv2.THRESH_BINARY + cv2.THRESH_OTSU,
            )
            gray = binary
        except Exception:
            pass

    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------------- #
#  Отрисовка подписи номера
# ---------------------------------------------------------------------- #
def _draw_plate_label(
    frame: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
) -> None:
    font = cv2.FONT_HERSHEY_DUPLEX
    font_scale = max(0.6, min(h / 28.0, 1.8))
    thickness = max(1, int(round(font_scale * 1.4)))

    (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)

    pad_x = max(6, int(font_scale * 8))
    pad_y = max(3, int(font_scale * 4))

    box_w = tw + 2 * pad_x
    box_h = th + baseline + 2 * pad_y

    box_x1 = x
    box_y1 = y - box_h - 4
    box_x2 = box_x1 + box_w
    box_y2 = box_y1 + box_h

    if box_y1 < 0:
        box_y1 = y + h + 4
        box_y2 = box_y1 + box_h

    frame_h = frame.shape[0]
    if box_y2 > frame_h:
        box_y2 = frame_h - 1
        box_y1 = box_y2 - box_h

    cv2.rectangle(frame, (box_x1, box_y1), (box_x2, box_y2), (255, 255, 255), -1)
    cv2.rectangle(frame, (box_x1, box_y1), (box_x2, box_y2), (0, 0, 0), 1)

    text_x = box_x1 + pad_x
    text_y = box_y1 + pad_y + th
    cv2.putText(frame, text, (text_x, text_y), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)


# ---------------------------------------------------------------------- #
#  Пайплайн
# ---------------------------------------------------------------------- #
class AlprPipeline:
    """ALPR-пайплайн на один кадр: детектор + OCR + постобработка."""

    def __init__(
        self,
        detector_model: str = "yolo-v9-t-384-license-plate-end2end",
        detection_confidence: float = 0.4,
        ocr_confidence: float = 0.5,
        padding_ratio: float = 0.1,
        min_plate_aspect: float = 2.0,
        max_plate_aspect: float = 8.0,
        min_plate_area: int = 300,
        ocr_target_height: int = 96,
        ocr_use_threshold: bool = False,
        ocr_intra_op_threads: int | None = None,
        debug: bool = False,
        debug_dir: Path | None = None,
    ):
        self.detector_model = detector_model
        self.detection_confidence = detection_confidence
        self.ocr_confidence = ocr_confidence
        self.padding_ratio = padding_ratio
        self.min_plate_aspect = min_plate_aspect
        self.max_plate_aspect = max_plate_aspect
        self.min_plate_area = min_plate_area
        self.ocr_target_height = ocr_target_height
        self.ocr_use_threshold = ocr_use_threshold

        # Потоки OCR: из параметра (auto_tune) или константа по умолчанию
        self.ocr_intra_op_threads = (
            int(ocr_intra_op_threads) if ocr_intra_op_threads else OCR_INTRA_OP_THREADS
        )

        self.debug = debug
        self.debug_dir = Path(debug_dir) if debug_dir else None
        if self.debug and self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

        self._detector = None
        self._ocr = None
        self._loaded = False
        self._debug_counter = 0

        logger.info(
            "AlprPipeline: модель=%s, conf_det=%.2f, conf_ocr=%.2f, "
            "ocr_target_h=%d, ocr_intra_op=%d, debug=%s",
            detector_model,
            detection_confidence,
            ocr_confidence,
            ocr_target_height,
            self.ocr_intra_op_threads,
            debug,
        )

    def _ensure_loaded(self):
        if self._loaded:
            return
        logger.info("Загружаю ALPR-модели...")
        t0 = time.time()

        try:
            from open_image_models import LicensePlateDetector

            self._detector = LicensePlateDetector(detection_model=self.detector_model)
        except ImportError:
            raise ImportError(
                "Модуль open_image_models не установлен. Установите: pip install fast-alpr[onnx]"
            )

        # ------------------------------------------------------------------
        # OCR с настройкой потоков ONNX Runtime.
        # Значение intra_op приходит из config.py (auto_tune подбирает по
        # числу физических ядер) или из YAML (при auto_tune: false).
        # ------------------------------------------------------------------
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise ImportError(
                "Модуль rapidocr_onnxruntime не установлен. "
                "Установите: pip install rapidocr-onnxruntime"
            )

        try:
            # Основной путь: прямая передача параметров потоков
            self._ocr = RapidOCR(
                intra_op_num_threads=self.ocr_intra_op_threads,
                inter_op_num_threads=OCR_INTER_OP_THREADS,
            )
            logger.info(
                "RapidOCR: intra_op=%d, inter_op=%d (direct kwargs)",
                self.ocr_intra_op_threads,
                OCR_INTER_OP_THREADS,
            )
        except TypeError:
            # Fallback: некоторые версии RapidOCR не принимают эти kwargs
            # напрямую. Пробуем через onnxruntime.SessionOptions.
            logger.warning(
                "RapidOCR не принял intra/inter_op_num_threads напрямую — "
                "пробую через SessionOptions"
            )
            try:
                import onnxruntime as ort

                sess_options = ort.SessionOptions()
                sess_options.intra_op_num_threads = self.ocr_intra_op_threads
                sess_options.inter_op_num_threads = OCR_INTER_OP_THREADS
                self._ocr = RapidOCR(sess_options=sess_options)
                logger.info(
                    "RapidOCR: intra_op=%d, inter_op=%d (SessionOptions)",
                    self.ocr_intra_op_threads,
                    OCR_INTER_OP_THREADS,
                )
            except Exception:
                logger.exception(
                    "Не удалось настроить потоки OCR — "
                    "использую RapidOCR() с параметрами по умолчанию"
                )
                self._ocr = RapidOCR()

        self._loaded = True
        logger.info("ALPR-модели загружены за %.1f сек", time.time() - t0)

    def _debug_save(self, image: np.ndarray, tag: str):
        if not (self.debug and self.debug_dir):
            return
        try:
            self._debug_counter += 1
            ts = datetime.now().strftime("%H%M%S_%f")[:-3]
            path = self.debug_dir / f"{ts}_{tag}_{self._debug_counter}.jpg"
            cv2.imwrite(str(path), image)
        except Exception as e:
            logger.debug("Не удалось сохранить debug-кадр: %s", e)

    def detect(
        self,
        frame: np.ndarray,
        include_unrecognized: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_loaded()

        try:
            detections = self._detector.predict(frame)
        except Exception as e:
            logger.exception("Ошибка детектора: %s", e)
            return []

        if self.debug:
            logger.debug(
                "Detector: %d raw detections (conf >= %.2f)",
                len(detections),
                self.detection_confidence,
            )

        if not detections:
            return []

        results: list[dict[str, Any]] = []

        for idx, det in enumerate(detections):
            if self.debug:
                logger.debug(
                    "  det[%d]: conf=%.3f, bbox=%s",
                    idx,
                    det.confidence,
                    det.bounding_box,
                )

            if det.confidence < self.detection_confidence:
                if self.debug:
                    logger.debug("    → отклонён по confidence")
                continue

            bbox_xyxy = det.bounding_box
            x1, y1, x2, y2 = map(int, bbox_xyxy)
            bw = x2 - x1
            bh = y2 - y1
            if bw <= 0 or bh <= 0:
                continue
            aspect = bw / bh
            area = bw * bh

            if self.debug:
                logger.debug(
                    "    bbox size=%dx%d, aspect=%.2f, area=%d",
                    bw,
                    bh,
                    aspect,
                    area,
                )

            if not (self.min_plate_aspect <= aspect <= self.max_plate_aspect):
                if self.debug:
                    logger.debug("    → отклонён по aspect")
                continue
            if area < self.min_plate_area:
                if self.debug:
                    logger.debug("    → отклонён по area")
                continue

            h_img, w_img = frame.shape[:2]
            pad_x = int(bw * self.padding_ratio)
            pad_y = int(bh * self.padding_ratio)
            cx1 = max(0, x1 - pad_x)
            cy1 = max(0, y1 - pad_y)
            cx2 = min(w_img, x2 + pad_x)
            cy2 = min(h_img, y2 + pad_y)
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue

            if self.debug:
                self._debug_save(crop, f"crop_det{idx}_raw")

            crop_processed = preprocess_crop(
                crop,
                target_height=self.ocr_target_height,
                use_threshold=self.ocr_use_threshold,
            )

            if self.debug:
                self._debug_save(crop_processed, f"crop_det{idx}_proc")

            try:
                ocr_result, _ = self._ocr(crop_processed)
            except Exception as e:
                logger.warning("Ошибка OCR: %s", e)
                ocr_result = None

            if self.debug:
                if ocr_result:
                    logger.debug(
                        "    OCR вернул %d блоков: %s",
                        len(ocr_result),
                        [item[1] for item in ocr_result],
                    )
                else:
                    logger.debug("    OCR вернул пусто")

            candidates = []
            if ocr_result:
                candidates = self._postprocess_ocr(ocr_result)

            if not candidates:
                if self.debug:
                    logger.debug("    → номер не собран из блоков")
                if include_unrecognized:
                    results.append(
                        {
                            "text": None,
                            "raw_text": "",
                            "confidence": float(det.confidence),
                            "bbox": (x1, y1, bw, bh),
                            "_unrecognized": True,
                        }
                    )
                continue

            if self.debug:
                logger.debug(
                    "    → кандидаты: %s",
                    [(c["text"], round(c["confidence"], 2)) for c in candidates],
                )

            best = max(candidates, key=lambda c: c["confidence"])

            results.append(
                {
                    "text": best["text"],
                    "raw_text": best["raw_text"],
                    "confidence": best["confidence"],
                    "bbox": (x1, y1, bw, bh),
                }
            )

        recognized = [r for r in results if r.get("text")]
        unrecognized = [r for r in results if not r.get("text")]

        recognized = self._deduplicate(recognized)
        return recognized + unrecognized

    def _postprocess_ocr(self, ocr_result) -> list[dict[str, Any]]:
        blocks = []
        for item in ocr_result:
            bbox, text, conf = item
            if conf < self.ocr_confidence:
                continue
            norm = normalize(text)
            if any(norm.startswith(p) for p in GARBAGE_PREFIXES):
                continue
            blocks.append({"bbox": bbox, "text": text, "conf": conf})

        if not blocks:
            return []

        candidates: list[dict[str, Any]] = []

        for b in blocks:
            text_fixed = fix_letter_digit_confusion(normalize(b["text"]))
            if is_valid_plate(text_fixed):
                candidates.append(
                    {
                        "text": text_fixed,
                        "raw_text": b["text"],
                        "confidence": b["conf"],
                    }
                )

        for i, b1 in enumerate(blocks):
            for j, b2 in enumerate(blocks):
                if i == j:
                    continue
                if not self._blocks_are_neighbors(b1["bbox"], b2["bbox"]):
                    continue
                combined_raw = b1["text"] + b2["text"]
                combined = fix_letter_digit_confusion(normalize(combined_raw))
                if is_valid_plate(combined):
                    candidates.append(
                        {
                            "text": combined,
                            "raw_text": combined_raw,
                            "confidence": min(b1["conf"], b2["conf"]),
                        }
                    )

        with_region = [c for c in candidates if len(c["text"]) >= 8]
        if with_region:
            candidates = with_region

        seen = set()
        unique = []
        for c in candidates:
            if c["text"] not in seen:
                seen.add(c["text"])
                unique.append(c)
        return unique

    @staticmethod
    def _blocks_are_neighbors(bbox1, bbox2, max_gap_ratio: float = 2.0) -> bool:
        x1 = min(p[0] for p in bbox1)
        y1 = min(p[1] for p in bbox1)
        x2 = max(p[0] for p in bbox1)
        y2 = max(p[1] for p in bbox1)
        X1 = min(p[0] for p in bbox2)
        Y1 = min(p[1] for p in bbox2)
        X2 = max(p[0] for p in bbox2)
        Y2 = max(p[1] for p in bbox2)

        h1 = y2 - y1
        h2 = Y2 - Y1
        if h1 <= 0 or h2 <= 0:
            return False
        if abs(h1 - h2) / max(h1, h2) > 0.5:
            return False
        if x1 > X1:
            return False
        gap = X1 - x2
        if gap > max_gap_ratio * max(h1, h2):
            return False
        cy1 = (y1 + y2) / 2
        cy2 = (Y1 + Y2) / 2
        if abs(cy1 - cy2) > 0.5 * max(h1, h2):
            return False
        return True

    @staticmethod
    def _deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_text: dict[str, dict[str, Any]] = {}
        for r in results:
            t = r["text"]
            if t not in by_text or r["confidence"] > by_text[t]["confidence"]:
                by_text[t] = r
        return list(by_text.values())

    @staticmethod
    def annotate_frame(
        frame: np.ndarray,
        plates: list[dict[str, Any]],
    ) -> np.ndarray:
        out = frame.copy()

        for p in plates:
            x, y, w, h = p["bbox"]
            text = p.get("text") or p.get("plate_text")

            if text:
                cv2.rectangle(out, (x, y), (x + w, y + h), (255, 255, 255), 2)
                _draw_plate_label(out, x, y, w, h, text)
            else:
                cv2.rectangle(out, (x, y), (x + w, y + h), (0, 0, 255), 2)

        return out


# ---------------------------------------------------------------------- #
#  Обёртка для движка
# ---------------------------------------------------------------------- #
class AlprDetector:
    """
    Обёртка над AlprPipeline с буфером, голосованием и cooldown.

    Кэширует bbox'ы последнего обработанного кадра (включая
    нераспознанные) для последующей отрисовки на снапшоте события.

    Параметр pipeline позволяет передать ОБЩИЙ AlprPipeline (один на все
    камеры) — тогда модели ONNX не грузятся отдельно для каждой камеры.
    Per-camera состояние (буфер голосования, cooldown, кэш bbox'ов)
    при этом остаётся своим в каждом экземпляре AlprDetector.
    """

    def __init__(
        self,
        camera_id: str,
        detector_model: str = "yolo-v9-t-384-license-plate-end2end",
        detection_confidence: float = 0.4,
        ocr_confidence: float = 0.5,
        padding_ratio: float = 0.1,
        process_every_n_frames: int = 5,
        voting_window_sec: float = 2.0,
        min_votes: int = 3,
        cooldown_same_plate_sec: float = 30.0,
        cooldown_any_sec: float = 5.0,
        zones=None,
        ocr_target_height: int = 96,
        ocr_use_threshold: bool = False,
        debug: bool = False,
        debug_dir: Path | None = None,
        pipeline: AlprPipeline | None = None,
    ):
        self.camera_id = camera_id
        self.process_every_n_frames = max(1, int(process_every_n_frames))
        self.voting_window_sec = float(voting_window_sec)
        self.min_votes = max(1, int(min_votes))
        self.cooldown_same_plate_sec = float(cooldown_same_plate_sec)
        self.cooldown_any_sec = float(cooldown_any_sec)
        self.zones = zones or []
        self.debug = debug

        if pipeline is not None:
            # Передан извне — используем общий AlprPipeline на все камеры,
            # чтобы не грузить модели по разу на камеру и не занимать
            # память несколькими копиями ONNX-сессий.
            self._pipeline = pipeline
        else:
            self._pipeline = AlprPipeline(
                detector_model=detector_model,
                detection_confidence=detection_confidence,
                ocr_confidence=ocr_confidence,
                padding_ratio=padding_ratio,
                ocr_target_height=ocr_target_height,
                ocr_use_threshold=ocr_use_threshold,
                debug=debug,
                debug_dir=debug_dir,
            )

        # Кэш bbox'ов последнего обработанного кадра — для отрисовки
        self._last_frame_annotations: list[dict[str, Any]] = []

        self._debug_dir_full: Path | None = None
        self._debug_save_interval = 3.0
        self._last_debug_save = 0.0
        if debug and debug_dir:
            self._debug_dir_full = Path(debug_dir) / f"_full_{camera_id}"
            self._debug_dir_full.mkdir(parents=True, exist_ok=True)

        self._frame_count = 0
        self._lock = threading.Lock()

        self._buffer: dict[str, dict[str, Any]] = {}
        self._buffer_start: float = 0.0

        self._last_event_time_by_plate: dict[str, float] = {}
        self._last_any_event_time: float = 0.0

        logger.info(
            "[%s] AlprDetector инициализирован (process_every=%d, "
            "window=%.1fs, min_votes=%d, debug=%s, pipeline=%s)",
            camera_id,
            self.process_every_n_frames,
            self.voting_window_sec,
            self.min_votes,
            debug,
            "shared" if pipeline is not None else "own",
        )

    # ------------------------------------------------------------------ #
    #  Detect
    # ------------------------------------------------------------------ #
    def detect(self, frame) -> list[dict[str, Any]]:
        self._frame_count += 1

        # Периодически сохраняем полный кадр для отладки
        if self._debug_dir_full:
            now_dbg = time.time()
            if now_dbg - self._last_debug_save >= self._debug_save_interval:
                self._last_debug_save = now_dbg
                try:
                    ts = datetime.now().strftime("%H%M%S_%f")[:-3]
                    p = self._debug_dir_full / f"{ts}_full.jpg"
                    cv2.imwrite(str(p), frame)
                except Exception:
                    pass

        if self._frame_count % self.process_every_n_frames != 0:
            return self._maybe_flush()

        try:
            all_dets = self._pipeline.detect(frame, include_unrecognized=True)
        except Exception as e:
            logger.exception("[%s] Ошибка ALPR: %s", self.camera_id, e)
            return []

        # Обновляем кэш для отрисовки
        self._last_frame_annotations = all_dets

        # В буфер голосования — только распознанные
        recognized = [d for d in all_dets if d.get("text")]

        if self.debug and recognized:
            logger.debug(
                "[%s] Пайплайн вернул распознанных: %s",
                self.camera_id,
                [(p["text"], round(p["confidence"], 2)) for p in recognized],
            )

        now = time.time()

        if not self._buffer:
            self._buffer_start = now

        for p in recognized:
            text = p["text"]
            base_key = text[:6] if len(text) >= 6 else text

            if base_key not in self._buffer:
                self._buffer[base_key] = {
                    "readings": [],
                    "bbox": p["bbox"],
                    "raw_texts": [],
                    "texts": [],
                }
            self._buffer[base_key]["readings"].append(p["confidence"])
            self._buffer[base_key]["raw_texts"].append(p["raw_text"])
            self._buffer[base_key]["texts"].append(text)
            self._buffer[base_key]["bbox"] = p["bbox"]

        if self.debug and self._buffer:
            logger.debug(
                "[%s] Буфер: %s",
                self.camera_id,
                {k: len(v["readings"]) for k, v in self._buffer.items()},
            )

        if now - self._buffer_start >= self.voting_window_sec:
            return self._flush()

        return []

    # ------------------------------------------------------------------ #
    #  Голосование
    # ------------------------------------------------------------------ #
    def _maybe_flush(self) -> list[dict[str, Any]]:
        if self._buffer and (time.time() - self._buffer_start) >= self.voting_window_sec:
            return self._flush()
        return []

    def _flush(self) -> list[dict[str, Any]]:
        with self._lock:
            buffer = self._buffer
            self._buffer = {}
            self._buffer_start = 0.0

        if not buffer:
            return []

        if self.debug:
            logger.debug(
                "[%s] Закрываю окно: %s",
                self.camera_id,
                {k: len(v["readings"]) for k, v in buffer.items()},
            )

        candidates = []
        for base_key, data in buffer.items():
            n_votes = len(data["readings"])
            if n_votes < self.min_votes:
                if self.debug:
                    logger.debug(
                        "  %s: %d голосов < min_votes=%d — отбрасываю",
                        base_key,
                        n_votes,
                        self.min_votes,
                    )
                continue

            texts = data.get("texts") or [base_key]
            longest = max(texts, key=len)

            avg_conf = sum(data["readings"]) / n_votes
            candidates.append(
                {
                    "text": longest,
                    "votes": n_votes,
                    "confidence": avg_conf,
                    "bbox": data["bbox"],
                    "raw_texts": data["raw_texts"],
                }
            )

        if not candidates:
            if self.debug:
                logger.debug(
                    "[%s] Ни один номер не набрал %d голосов",
                    self.camera_id,
                    self.min_votes,
                )
            return []

        candidates.sort(key=lambda c: (c["votes"], c["confidence"]), reverse=True)
        winner = candidates[0]

        now = time.time()

        last_same = self._last_event_time_by_plate.get(winner["text"], 0.0)
        if now - last_same < self.cooldown_same_plate_sec:
            logger.debug(
                "[%s] Номер %s в cooldown (%.1f сек назад)",
                self.camera_id,
                winner["text"],
                now - last_same,
            )
            return []

        if now - self._last_any_event_time < self.cooldown_any_sec:
            return []

        self._last_event_time_by_plate[winner["text"]] = now
        self._last_any_event_time = now

        x, y, w, h = winner["bbox"]
        cx, cy = x + w // 2, y + h // 2

        zone_name = None
        for zone in self.zones:
            if zone.contains_bbox(winner["bbox"], min_overlap=0.3):
                zone_name = zone.name
                break

        logger.info(
            "[%s] ALPR: %s (голосов %d, conf %.2f, raw %s)",
            self.camera_id,
            winner["text"],
            winner["votes"],
            winner["confidence"],
            winner["raw_texts"],
        )

        return [
            {
                "bbox": winner["bbox"],
                "area": w * h,
                "class_id": -1,
                "class_name": "plate",
                "plate_text": winner["text"],
                "confidence": winner["confidence"],
                "solidity": 1.0,
                "zone": zone_name,
                "center": (cx, cy),
                "_votes": winner["votes"],
                "_raw_texts": winner["raw_texts"],
            }
        ]

    # ------------------------------------------------------------------ #
    #  Совместимость / отрисовка
    # ------------------------------------------------------------------ #
    def annotate_frame(self, frame, detections=None):
        """
        Рисует все bbox'ы последнего обработанного кадра:
        - распознанные — белая рамка + подпись с номером;
        - нераспознанные — красная рамка.
        """
        plates = self._last_frame_annotations or []
        prepared = []
        for d in plates:
            prepared.append(
                {
                    "text": d.get("text"),
                    "raw_text": d.get("raw_text") or "",
                    "confidence": d["confidence"],
                    "bbox": d["bbox"],
                }
            )
        return self._pipeline.annotate_frame(frame, prepared)

    def reset(self):
        with self._lock:
            self._buffer = {}
            self._buffer_start = 0.0
        self._last_frame_annotations = []
