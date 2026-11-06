"""Единый формат логов для всех ботов."""
import logging
from pathlib import Path


def setup_logging(name, log_dir=None):
    log_dir = log_dir or (Path(__file__).resolve().parent.parent / "logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8"),
        ],
        force=True,
    )
    return logging.getLogger(name)
