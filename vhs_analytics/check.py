"""
CLI-диагностика. Запуск:
    python -m vhs_analytics.check viewer_config.yaml
Проверяет по шагам: конфиг → API Streamer → RTSP → детектор.
"""

import logging
import sys
import time

import cv2

from .config import load_config
from .detector import ALPRDetector
from .stream_client import StreamerClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("check")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "viewer_config.yaml"
    cfg = load_config(config_path)
    print("=== config ===")
    for k, v in cfg.items():
        if k == "telegram":
            v = {kk: ("***" if kk == "token" else vv) for kk, vv in v.items()}
        print(f"  {k}: {v}")

    if not cfg["streamer_url"]:
        print("FAIL: streamer_url is empty")
        return 1

    print("\n=== streamer api ===")
    client = StreamerClient(cfg["streamer_url"], cfg.get("rtsp_base"))
    streams = client.get_streams()
    if not streams:
        print("FAIL: no streams returned (check URL and API response shape)")
        return 1
    for s in streams:
        print(f"  {s['id']}  rtsp={s['rtsp_url']}")

    print("\n=== detector ===")
    # Явно передаём аргументы по имени — чтобы не путать camera_id с model_path
    det = ALPRDetector(
        camera_id="check",
        conf_threshold=cfg.get("conf_threshold", 0.5),
    )
    print("  detector initialized")

    print("\n=== rtsp probe (first stream, 3 sec) ===")
    target = streams[0]
    cap = cv2.VideoCapture(target["rtsp_url"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        print(f"FAIL: cannot open {target['rtsp_url']}")
        return 1

    frames = 0
    detections_total = 0
    t0 = time.time()
    while time.time() - t0 < 3.0:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1
        if frames == 1:
            print(f"  first frame shape={frame.shape}")
        # Гоним каждый кадр в детектор — как в боевом режиме при analyze_every_n=1
        dets = det.detect(frame)
        detections_total += len(dets)
        for d in dets:
            print(f"  DETECTED: {d}")
    cap.release()
    print(f"  received {frames} frames in 3s, detections={detections_total}")
    if frames == 0:
        print("FAIL: no frames read")
        return 1

    print("\nOK: chain works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
