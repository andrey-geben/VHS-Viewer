#!/usr/bin/env python3
"""
Отдельный процесс аналитики VHS-Viewer.

Запуск:
    python run_analytics.py [viewer_config.yaml]

Останавливается по SIGINT/SIGTERM. Подходит для systemd / supervisor / docker.
"""

import logging
import signal
import sys
import time

from vhs_analytics.bootstrap import build_manager


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    _setup_logging()
    log = logging.getLogger("run_analytics")

    config_path = sys.argv[1] if len(sys.argv) > 1 else "viewer_config.yaml"
    manager = build_manager(config_path)

    if manager is None:
        log.error("Аналитика отключена (analytics.enabled=false). Выход.")
        return 1

    stopping = {"flag": False}

    def _handle_signal(signum, _frame):
        log.info(f"Получен сигнал {signum}, останавливаюсь...")
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    manager.start()
    log.info("Аналитика запущена. Ctrl+C для остановки.")

    try:
        while not stopping["flag"]:
            time.sleep(1)
    finally:
        manager.stop()
        log.info("Остановлено.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
