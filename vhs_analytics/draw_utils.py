"""
Утилиты для отрисовки текста на кадрах OpenCV.

Зачем нужен этот модуль: cv2.putText рисует только ASCII, кириллица
превращается в «кракозябры». Здесь используем PIL/Pillow с настоящим
шрифтом (Arial / DejaVu), который умеет Unicode.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Порядок важен: первым берём шрифт, который точно есть на машине.
# Windows → Arial; Linux → DejaVu; macOS → Arial в системе.
_FONT_CANDIDATES: list[Path] = [
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
]

# Кэш, чтобы не открывать файл шрифта на каждом кадре.
_font_cache: dict[int, ImageFont.FreeTypeFont | None] = {}


def load_font(size: int = 20) -> ImageFont.FreeTypeFont | None:
    """Возвращает шрифт нужного размера или None, если ни один не найден."""
    if size in _font_cache:
        return _font_cache[size]

    font: ImageFont.FreeTypeFont | None = None
    for path in _FONT_CANDIDATES:
        if path.exists():
            try:
                font = ImageFont.truetype(str(path), size=size)
                break
            except Exception:
                continue

    _font_cache[size] = font
    return font


def draw_text_ru(
    frame_bgr: np.ndarray,
    text: str,
    origin: tuple[int, int],
    color_bgr: tuple[int, int, int] = (0, 255, 0),
    font_size: int = 20,
) -> np.ndarray:
    """
    Рисует русский текст на кадре OpenCV (BGR) и возвращает новый кадр.

    frame_bgr — numpy-массив BGR, как его отдаёт cv2.VideoCapture / cv2.imread.
    text      — уже переведённая строка, например "медведь 0.87".
    origin    — (x, y) левого верхнего угла текста.
    """
    if not text:
        return frame_bgr

    font = load_font(font_size)
    if font is None:
        # Шрифт не найден — тихо возвращаем кадр без текста.
        # (Один раз стоит залогировать, но модуль намеренно без logging.)
        return frame_bgr

    # OpenCV → Pillow: BGR → RGB
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(frame_rgb)
    draw = ImageDraw.Draw(image)

    # Цвет в Pillow — RGB, поэтому переворачиваем порядок из BGR.
    color_rgb = (color_bgr[2], color_bgr[1], color_bgr[0])
    draw.text(origin, text, font=font, fill=color_rgb)

    # Pillow → OpenCV: RGB → BGR
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)