# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для VHS-Viewer (с аналитикой)."""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_all, collect_data_files, collect_submodules,
    collect_dynamic_libs,
)

ROOT = Path(SPECPATH).resolve()

# ============================================================
# Автоматический сбор данных/бинарей для тяжёлых пакетов
# ============================================================
datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]
binaries = []
hiddenimports = []

# --- torch / torchvision: собираем ВСЁ (DLL, .pyd, метаданные) ---
for pkg in ("torch", "torchvision"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# --- ultralytics: cfg-файлы моделей + все подмодули ---
d, b, h = collect_all("ultralytics")
datas += d
binaries += b
hiddenimports += h
hiddenimports += collect_submodules("ultralytics")

# --- rapidocr_onnxruntime: config.yaml + ONNX-модели + utils ---
d, b, h = collect_all("rapidocr_onnxruntime")
datas += d
binaries += b
hiddenimports += h

# --- open_image_models: веса детектора номеров ---
d, b, h = collect_all("open_image_models")
datas += d
binaries += b
hiddenimports += h

# --- onnxruntime: нативные либы ---
d, b, h = collect_all("onnxruntime")
datas += d
binaries += b
hiddenimports += h

# ============================================================
# Наши модули и стандартные зависимости
# ============================================================
hiddenimports += [
    "yaml", "flask", "werkzeug", "jinja2", "requests",
    "auth", "__version__",
    "vhs_analytics",
    "vhs_analytics.bootstrap",
    "vhs_analytics.manager",
    "vhs_analytics.stream_client",
    "vhs_analytics.detector",
    "vhs_analytics.object_detector",
    "vhs_analytics.worker",
    "vhs_analytics.notifier",
    "vhs_analytics.event_writer",
    "vhs_analytics.relay",
    "vhs_analytics.messaging",
    "vhs_analytics.config",
    "vhs_analytics.vhs2_alpr",
    "PIL", "PIL.Image",
    "cv2", "numpy",
    "certifi", "charset_normalizer", "urllib3",
    "vhs_analytics.hardware",     # ← новое
    "vhs_analytics.labels_ru",    # ← новое
]

a = Analysis(
    ["viewer.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "pandas.tests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VHS-Viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "icon.ico"),
    version=str(ROOT / "version_info.txt"),
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="VHS-Viewer",
)