"""
Определение характеристик железа и рекомендации по настройке аналитики.

Использование:
    from vhs_analytics.hardware import detect_hardware
    hw = detect_hardware()
    hw.log_summary()             # в лог: ядра, RAM, частота
    rec = hw.recommend()         # {'intra_op_num_threads': 3, 'objects_per_camera': False, ...}
    for w in hw.warnings(): log.warning(w)
    print(hw.advice())           # человекочитаемый совет

CLI:
    python -m vhs_analytics.hardware              # показать профиль
    python -m vhs_analytics.hardware --benchmark  # прогнать нагрузку и замерить fps
"""

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    log.info(
        "psutil не установлен — определение железа будет неточным. Установите: pip install psutil"
    )


# ----------------------------------------------------------------------
#  Профиль
# ----------------------------------------------------------------------
@dataclass
class HardwareProfile:
    physical_cores: int
    logical_cores: int
    ram_total_gb: float
    ram_available_gb: float
    cpu_freq_current_mhz: int | None = None
    cpu_freq_max_mhz: int | None = None
    has_cuda: bool = False
    has_intel_igpu: bool = False
    is_on_battery: bool = False
    has_battery: bool = False
    os_name: str = ""

    @property
    def is_throttling(self) -> bool:
        """True, если текущая частота < 60% от макс (эвристика)."""
        if not self.cpu_freq_current_mhz or not self.cpu_freq_max_mhz:
            return False
        if self.cpu_freq_max_mhz <= 0:
            return False
        return (self.cpu_freq_current_mhz / self.cpu_freq_max_mhz) < 0.6

    @property
    def is_hyperthreaded(self) -> bool:
        return self.logical_cores > self.physical_cores

    def warnings(self) -> list[str]:
        out: list[str] = []

        if self.is_on_battery:
            out.append(
                "⚠ Ноутбук работает НА БАТАРЕЕ. CPU ограничен ~800–1600 MHz. "
                "Подключите зарядку и план питания «Высокая производительность»."
            )

        if self.is_throttling:
            pct = int(100 * self.cpu_freq_current_mhz / self.cpu_freq_max_mhz)
            out.append(
                f"⚠ CPU работает на {self.cpu_freq_current_mhz} MHz "
                f"из {self.cpu_freq_max_mhz} MHz ({pct}%) — троттлинг. "
                f"Проверьте охлаждение / power plan. "
                f"FPS будет падать на длительной нагрузке."
            )

        if self.ram_available_gb < 2.0:
            out.append(
                f"⚠ Мало свободной RAM: {self.ram_available_gb:.1f} GB "
                f"из {self.ram_total_gb:.1f} GB. Модели YOLO+OCR требуют ~1.5 GB."
            )

        if self.physical_cores <= 2:
            out.append(
                f"⚠ Всего {self.physical_cores} физических ядер. "
                f"На таком CPU реально тянуть только 1 камеру с номерами, "
                f"imgsz=320, analyze_every=3."
            )

        return out

    def recommend(self) -> dict:
        """
        Возвращает оптимальные параметры для этого железа.
        Ключи совпадают с полями viewer_config.yaml (analytics.*).
        """
        phys = self.physical_cores

        # стало
        # intra_op: OCR отдаём (phys-1) ядра, оставляя 1 на reader/Flask/YOLO.
        # Но если ядер очень мало — отдаём все.
        intra = phys if phys <= 2 else max(1, min(phys - 1, 8))

        # objects_per_camera: параллельные ONNX-сессии оправданы только при 6+ ядрах
        # и достаточной RAM.
        objects_per_camera = phys >= 6 and self.ram_available_gb >= 4.0

        # analyze_every_n: чем мощнее CPU, тем чаще можно вызывать детектор
        if phys >= 8:
            analyze_every_n = 1
        elif phys >= 4:
            analyze_every_n = 2
        else:
            analyze_every_n = 3

        # object_imgsz: YOLO на полном 640 — только для мощных CPU
        if phys >= 8:
            object_imgsz = 640
        elif phys >= 4:
            object_imgsz = 416
        else:
            object_imgsz = 320

        return {
            "intra_op_num_threads": intra,
            "inter_op_num_threads": 1,
            "objects_per_camera": objects_per_camera,
            "analyze_every_n": analyze_every_n,
            "object_imgsz": object_imgsz,
        }

    def advice(self) -> str:
        rec = self.recommend()
        phys = self.physical_cores
        log_cores = self.logical_cores

        if phys <= 2:
            profile = "слабое железо (ноутбук / неттоп)"
        elif phys <= 4:
            profile = "средний CPU (ультрабук / мини-ПК)"
        elif phys <= 8:
            profile = "хороший CPU (десктоп / сервер)"
        else:
            profile = "мощный сервер"

        lines = [
            f"Профиль: {profile} — {phys} физ. / {log_cores} лог. ядер, "
            f"{self.ram_total_gb:.1f} GB RAM",
            f"Рекомендация: intra_op={rec['intra_op_num_threads']}, "
            f"objects_per_camera={rec['objects_per_camera']}, "
            f"analyze_every_n={rec['analyze_every_n']}, "
            f"object_imgsz={rec['object_imgsz']}",
        ]

        if self.has_cuda:
            lines.append(
                "✓ Обнаружен NVIDIA GPU (CUDA). "
                "Рекомендуется `pip install onnxruntime-gpu` и "
                "экспорт YOLO в ONNX с провайдером CUDA — прирост 5–10x."
            )

        if phys <= 4:
            lines.append(
                "Совет: не включайте больше 1–2 камер с plates одновременно. "
                "Для остальных — plates=false, objects=[person]."
            )
        else:
            lines.append("Совет: можно включить все камеры + objects_per_camera=true.")

        return "\n".join(lines)

    def log_summary(self):
        log.info(
            "Железо: %d физ. / %d лог. ядер, %.1f GB RAM (%.1f GB свободно), "
            "частота %s/%s MHz, CUDA=%s, батарея=%s",
            self.physical_cores,
            self.logical_cores,
            self.ram_total_gb,
            self.ram_available_gb,
            self.cpu_freq_current_mhz or "?",
            self.cpu_freq_max_mhz or "?",
            self.has_cuda,
            self.is_on_battery,
        )

    def to_dict(self) -> dict:
        """Для JSON-сериализации в API."""
        return {
            "physical_cores": self.physical_cores,
            "logical_cores": self.logical_cores,
            "ram_total_gb": round(self.ram_total_gb, 1),
            "ram_available_gb": round(self.ram_available_gb, 1),
            "cpu_freq_current_mhz": self.cpu_freq_current_mhz,
            "cpu_freq_max_mhz": self.cpu_freq_max_mhz,
            "has_cuda": self.has_cuda,
            "is_on_battery": self.is_on_battery,
            "is_throttling": self.is_throttling,
            "os_name": self.os_name,
            "recommend": self.recommend(),
            "warnings": self.warnings(),
            "advice": self.advice(),
        }


# ----------------------------------------------------------------------
#  Определение
# ----------------------------------------------------------------------
def _detect_physical_cores() -> int:
    """Физические ядра без hyperthreading."""
    if _HAS_PSUTIL:
        try:
            n = psutil.cpu_count(logical=False)
            if n:
                return int(n)
        except Exception:
            pass

    if platform.system() == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "cpu", "get", "NumberOfCores"],
                text=True,
                timeout=5,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        except Exception:
            pass

    return os.cpu_count() or 1


def _detect_cpu_freq() -> tuple[int | None, int | None]:
    """Возвращает (current_mhz, max_mhz)."""
    if _HAS_PSUTIL:
        try:
            freq = psutil.cpu_freq()
            if freq and freq.max and freq.max > 0:
                return int(freq.current), int(freq.max)
        except Exception:
            pass

    if platform.system() == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "cpu", "get", "CurrentClockSpeed,MaxClockSpeed"],
                text=True,
                timeout=5,
            )
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            for line in lines:
                parts = line.split()
                if len(parts) == 2 and all(p.isdigit() for p in parts):
                    return int(parts[0]), int(parts[1])
        except Exception:
            pass

    return None, None


def _detect_battery() -> tuple[bool, bool]:
    """Возвращает (has_battery, is_on_battery)."""
    if not _HAS_PSUTIL:
        return False, False
    try:
        bat = psutil.sensors_battery()
        if bat is None:
            return False, False
        return True, (not bat.power_plugged)
    except Exception:
        return False, False


def _detect_cuda() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def detect_hardware() -> HardwareProfile:
    """Главная функция — собирает профиль."""
    physical = _detect_physical_cores()
    logical = os.cpu_count() or physical

    ram_total = 0.0
    ram_avail = 0.0
    if _HAS_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            ram_total = vm.total / (1024**3)
            ram_avail = vm.available / (1024**3)
        except Exception:
            pass

    cur, mx = _detect_cpu_freq()
    has_battery, on_battery = _detect_battery()

    return HardwareProfile(
        physical_cores=physical,
        logical_cores=logical,
        ram_total_gb=ram_total,
        ram_available_gb=ram_avail,
        cpu_freq_current_mhz=cur,
        cpu_freq_max_mhz=mx,
        has_cuda=_detect_cuda(),
        is_on_battery=on_battery,
        has_battery=has_battery,
        os_name=f"{platform.system()} {platform.release()}",
    )


# ----------------------------------------------------------------------
#  CLI
# ----------------------------------------------------------------------
def _cli():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )

    benchmark = "--benchmark" in sys.argv

    hw = detect_hardware()
    hw.log_summary()
    print()
    print(hw.advice())
    print()
    for w in hw.warnings():
        print(w)

    if not benchmark:
        print()
        print("Совет: запустите с --benchmark для теста реальной нагрузки.")
        return

    print()
    print("=" * 60)
    print("  BENCHMARK: 30 секунд нагрузки")
    print("=" * 60)
    _run_benchmark(hw)


def _run_benchmark(hw: HardwareProfile):
    """Прогоняет YOLO на тестовом кадре и меряет fps."""
    import time

    import numpy as np

    rec = hw.recommend()
    print(f"Параметры: {rec}")

    print("\nЗагружаю ObjectDetector...")
    try:
        from .object_detector import ObjectDetector

        det = ObjectDetector(
            model_name="yolov8n.pt",
            conf_threshold=0.4,
            default_imgsz=rec["object_imgsz"],
        )
        det._ensure_loaded()
    except Exception as e:
        print(f"Не удалось загрузить YOLO: {e}")
        return

    frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)

    for _ in range(3):
        det.detect(frame, imgsz=rec["object_imgsz"])

    print("Жду 30 секунд, считаю fps...")
    t0 = time.time()
    frames = 0
    while time.time() - t0 < 30:
        det.detect(frame, imgsz=rec["object_imgsz"])
        frames += 1

    elapsed = time.time() - t0
    fps = frames / elapsed
    print(f"\nРезультат: {frames} кадров за {elapsed:.1f}с — {fps:.1f} fps")

    if fps >= 10:
        print("✓ Отлично. Можно включить analyze_every_n=1 на всех камерах.")
        print("  Совет: objects_per_camera=true (если камер 2+).")
    elif fps >= 5:
        print("✓ Норма. Рекомендуется analyze_every_n=2, object_imgsz=416.")
        print("  Совет: не включайте больше 2 камер с plates=true.")
    elif fps >= 2:
        print("⚠ Медленно. Рекомендуется analyze_every_n=3, object_imgsz=320.")
        print("  Совет: только 1 камера с plates, остальные — person-only.")
    else:
        print("✗ Очень медленно. Для боевого сервера нужно более мощное железо.")
        print("  Совет: переезд на сервер 8+ ядер, или GPU (CUDA), или снижение")
        print("  количества активных камер до 1.")

    print("\nПроверяю стабильность под нагрузкой (троттлинг)...")
    t0 = time.time()
    frames2 = 0
    fps_per_sec = []
    last_sec = 0
    while time.time() - t0 < 10:
        det.detect(frame, imgsz=rec["object_imgsz"])
        frames2 += 1
        sec = int(time.time() - t0)
        if sec > last_sec:
            fps_per_sec.append(frames2)
            frames2 = 0
            last_sec = sec

    if len(fps_per_sec) >= 2:
        first = fps_per_sec[0]
        last = fps_per_sec[-1]
        drop_pct = 100 * (first - last) / max(1, first)
        print(f"  Первая секунда: {first} fps, последняя: {last} fps (падение {drop_pct:.0f}%)")
        if drop_pct > 20:
            print("  ⚠ ТРОТТЛИНГ: fps падает на длительной нагрузке.")
            print("  Лечение: охлаждение, power plan High Performance, undervolt.")


if __name__ == "__main__":
    _cli()
