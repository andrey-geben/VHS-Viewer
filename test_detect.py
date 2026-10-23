"""
Быстрый тест ALPR-пайплайна в изоляции.

Захватывает один кадр с RTSP, сохраняет его как JPEG,
прогоняет через ALPR и сохраняет аннотированный результат.

Запуск:
    python test_detect.py rtsp://127.0.0.1:8554/analog_1_s1

Что смотреть:
    - test_frame.jpg     — что реально видит камера (до детектора)
    - test_annotated.jpg — что нашёл детектор (с рамками)
    - вывод в консоль    — полная трассировка пайплайна
"""

import logging
import os
import sys
from pathlib import Path

# Тот же env, что в vhs_analytics/__init__.py — до первого импорта cv2
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2

from vhs_analytics.vhs2_alpr import AlprPipeline

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("test_detect")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "rtsp://127.0.0.1:8554/analog_1_s1"
    log.info("открываю %s", url)

    cap = cv2.VideoCapture(url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)
    if not cap.isOpened():
        log.error("не удалось открыть поток")
        return 1

    # Пропускаем первые кадры — нужен ключевой кадр и стабилизация
    for _ in range(10):
        cap.read()

    ok, frame = cap.read()
    cap.release()

    if not ok or frame is None:
        log.error("не удалось получить кадр")
        return 1

    Path("test_frame.jpg").parent.mkdir(exist_ok=True)
    cv2.imwrite("test_frame.jpg", frame)
    log.info("сохранён test_frame.jpg, shape=%s", frame.shape)

    log.info("инициализирую AlprPipeline (debug=True)")
    pipeline = AlprPipeline(debug=True)
    pipeline._ensure_loaded()  # noqa: SLF001

    log.info("запускаю detect()")
    results = pipeline.detect(frame, include_unrecognized=True)

    log.info("РЕЗУЛЬТАТ: %d объектов", len(results))
    for r in results:
        log.info("  %s", r)

    if results:
        annotated = pipeline.annotate_frame(frame, results)
        cv2.imwrite("test_annotated.jpg", annotated)
        log.info("сохранён test_annotated.jpg")
    else:
        log.info(
            "детектор ничего не нашёл — это ожидаемо, если в кадре нет номера "
            "или он слишком мелкий/размытый"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
