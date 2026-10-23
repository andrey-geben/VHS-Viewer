"""
Пакет видеоаналитики для VHS-Viewer.

Работает как отдельный процесс: подключается к RTSP из VHS-Streamer
и прогоняет кадры через ALPR из VHS2.

ВАЖНО: env-переменные для OpenCV/FFmpeg ставятся ЗДЕСЬ, до первого
импорта cv2 в подмодулях. Иначе OpenCV не увидит их и продолжит
использовать UDP, что на 1440p-потоках даёт corrupted macroblocks.
"""

import os

# RTSP через TCP — надёжнее на высоких разрешениях, где UDP теряет пакеты.
# Без этого ffmpeg-декодер сыпет "corrupted macroblock" и не может
# восстановить кадр. Ставим default: если пользователь захотел udp —
# он может переопределить снаружи.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp",
)

# Приглушаем FFmpeg-логи: на повреждённых пакетах он забивает консоль
# сообщениями AV_LOG_ERROR, которые дублируются в лог аналитики.
# 8 = AV_LOG_FATAL (показываем только фатальные). 0 = AV_LOG_PANIC.
# Ставим 8, чтобы реальные сбои (нет соединения, codec unsupported)
# были видны, но мусор от corrupted macroblocks — нет.
os.environ.setdefault(
    "OPENCV_FFMPEG_LOGLEVEL",
    "8",
)

__version__ = "0.1.0"

from .manager import AnalyticsManager

__all__ = ["AnalyticsManager"]
