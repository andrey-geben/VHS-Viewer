"""Видеоархив VHS-Viewer.

Режимы:
  events — клип вокруг события аналитики (по умолчанию, 15 секунд после события);
  always — непрерывная запись сегментами по камерам с квотой;
  off    — запись выключена.

Источник — RTSP-поток Streamer. Если в потоке задан rtsp_url, используется он,
иначе адрес собирается из WebRTC URL и rtsp_port.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE = {
    "enabled": False,
    "mode": "events",
    "path": "",
    "quota_gb": 50,
    "event_types": ["plate", "person", "vehicle", "animal"],
    "clip_seconds": 15,
    "cooldown_sec": 30,
    "rtsp_port": 8554,
    "ffmpeg_path": "",
    "cameras": [],
}


def _clamp_int(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return text.strip("._")[:80] or "camera"


class ArchiveManager:
    def __init__(self, cfg: dict, base_dir: str | Path, streams: list | None = None,
                 log_dir: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.log_dir = Path(log_dir) if log_dir else self.base_dir / "logs"
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._last_event: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._cfg: dict = dict(DEFAULT_ARCHIVE)
        self._streams: list[dict] = []
        self.reconfigure(cfg, streams or [])

    # ------------------------------------------------------------------
    # Конфигурация
    # ------------------------------------------------------------------
    def reconfigure(self, cfg: dict, streams: list | None = None) -> None:
        self.stop_continuous()
        source = cfg if isinstance(cfg, dict) else {}
        merged = dict(DEFAULT_ARCHIVE)
        merged.update({k: v for k, v in source.items() if k in DEFAULT_ARCHIVE})
        merged["enabled"] = bool(merged.get("enabled"))
        merged["mode"] = str(merged.get("mode") or "events").lower()
        if merged["mode"] not in ("events", "always", "off"):
            merged["mode"] = "events"
        merged["quota_gb"] = max(0.0, float(merged.get("quota_gb") or 0))
        merged["clip_seconds"] = _clamp_int(merged.get("clip_seconds"), 1, 3600, 15)
        merged["cooldown_sec"] = _clamp_int(merged.get("cooldown_sec"), 0, 86400, 30)
        merged["rtsp_port"] = _clamp_int(merged.get("rtsp_port"), 1, 65535, 8554)
        types = merged.get("event_types")
        if not isinstance(types, list):
            types = DEFAULT_ARCHIVE["event_types"]
        merged["event_types"] = [str(x).lower() for x in types if str(x).strip()]
        cameras = merged.get("cameras")
        merged["cameras"] = [str(x) for x in cameras] if isinstance(cameras, list) else []
        self._cfg = merged
        self._streams = [s for s in (streams or []) if isinstance(s, dict)]

        raw_path = str(merged.get("path") or "").strip()
        self.path = Path(os.path.expandvars(os.path.expanduser(raw_path))) if raw_path else (self.base_dir / "archive")
        if not self.path.is_absolute():
            self.path = (self.base_dir / self.path).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = self._find_ffmpeg(str(merged.get("ffmpeg_path") or ""))
        self._stop.clear()
        if self.enabled and self.mode == "always":
            self.start_continuous()
        if self.enabled:
            self._start_cleanup_thread()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled"))

    @property
    def mode(self) -> str:
        return str(self._cfg.get("mode") or "off")

    def _find_ffmpeg(self, configured: str) -> str:
        candidates = []
        if configured:
            candidates.append(configured)
        env = os.environ.get("VHS_VIEWER_FFMPEG", "").strip()
        if env:
            candidates.append(env)
        found = shutil.which("ffmpeg")
        if found:
            candidates.append(found)
        sibling = self.base_dir.parent / "VHS-Streamer" / "bin" / "ffmpeg.exe"
        candidates.append(str(sibling))
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        return candidates[0] if candidates else "ffmpeg"

    # ------------------------------------------------------------------
    # RTSP
    # ------------------------------------------------------------------
    def _rtsp_for_stream(self, stream: dict) -> str:
        explicit = str(stream.get("rtsp_url") or "").strip()
        if explicit:
            return explicit
        raw = str(stream.get("url") or "").strip()
        if not raw:
            return ""
        if raw.lower().startswith("rtsp://"):
            return raw
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return ""
        host = parsed.hostname or parsed.netloc
        if not host:
            return ""
        path = parsed.path.strip("/")
        if not path:
            return ""
        credentials = ""
        if parsed.username:
            credentials = parsed.username
            if parsed.password:
                credentials += ":" + parsed.password
            credentials += "@"
        port = int(self._cfg.get("rtsp_port") or 8554)
        return f"rtsp://{credentials}{host}:{port}/{path}"

    def _camera_allowed(self, camera_id: str) -> bool:
        allowed = self._cfg.get("cameras") or []
        return not allowed or str(camera_id) in allowed

    @staticmethod
    def _camera_key(stream: dict) -> str:
        explicit = str(stream.get("id") or stream.get("camera_id") or "").strip()
        if explicit:
            return explicit
        raw = str(stream.get("url") or "").strip()
        try:
            path = urlsplit(raw).path.strip("/")
        except ValueError:
            path = ""
        key = path.rsplit("/", 1)[-1] if path else ""
        return key or str(stream.get("name") or "camera")

    # ------------------------------------------------------------------
    # Запись по событию
    # ------------------------------------------------------------------
    def on_event(self, stream: dict, detection: dict) -> None:
        if not self.enabled or self.mode != "events":
            return
        category = str(detection.get("category") or "object").lower()
        types = self._cfg.get("event_types") or []
        if category not in types:
            return
        camera_id = self._camera_key(stream)
        if not self._camera_allowed(camera_id):
            return
        key = (camera_id, category)
        now = time.time()
        cooldown = int(self._cfg.get("cooldown_sec") or 0)
        with self._lock:
            if now - self._last_event.get(key, 0.0) < cooldown:
                return
            self._last_event[key] = now

        source = self._rtsp_for_stream(stream)
        if not source:
            log.warning("archive[%s]: нет RTSP-источника для записи", camera_id)
            return
        stamp = datetime.now()
        folder = self.path / _safe_name(camera_id) / stamp.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{stamp.strftime('%H-%M-%S')}_{_safe_name(category)}.mp4"
        self._spawn_clip(source, target, camera_id, category)

    def _spawn_clip(self, source: str, target: Path, camera_id: str, category: str) -> None:
        seconds = int(self._cfg.get("clip_seconds") or 15)
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-rtsp_transport", "tcp", "-i", source,
            "-t", str(seconds), "-an", "-c:v", "copy",
            "-movflags", "+faststart", str(target),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.exception("archive[%s]: не удалось запустить ffmpeg", camera_id)
            return
        log.info("archive[%s]: запись %ss → %s", camera_id, seconds, target.name)

        def wait():
            try:
                _, err = proc.communicate(timeout=seconds + 60)
                if proc.returncode != 0:
                    log.warning("archive[%s]: ffmpeg rc=%s %s", camera_id, proc.returncode,
                                (err or b"").decode("utf-8", "replace")[-500:])
                    target.unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                proc.kill()
                target.unlink(missing_ok=True)
                log.warning("archive[%s]: таймаут записи", camera_id)
            except Exception:
                log.exception("archive[%s]: ошибка ожидания записи", camera_id)
            finally:
                self._enforce_quota()

        threading.Thread(target=wait, name=f"archive-clip-{camera_id}", daemon=True).start()

    # ------------------------------------------------------------------
    # Непрерывная запись
    # ------------------------------------------------------------------
    def start_continuous(self) -> None:
        if not self.enabled or self.mode != "always":
            return
        for stream in self._streams:
            if not stream.get("enabled", True):
                continue
            camera_id = self._camera_key(stream)
            if not self._camera_allowed(camera_id):
                continue
            if camera_id in self._processes:
                continue
            source = self._rtsp_for_stream(stream)
            if not source:
                continue
            folder = self.path / _safe_name(camera_id) / "segments"
            folder.mkdir(parents=True, exist_ok=True)
            pattern = str(folder / "%Y-%m-%d_%H-%M-%S.mp4")
            cmd = [
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-rtsp_transport", "tcp", "-i", source, "-an", "-c:v", "copy",
                "-f", "segment", "-segment_time", "60", "-reset_timestamps", "1",
                "-strftime", "1", pattern,
            ]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._processes[camera_id] = proc
                log.info("archive[%s]: непрерывная запись запущена", camera_id)
            except Exception:
                log.exception("archive[%s]: не удалось запустить непрерывную запись", camera_id)

    def stop_continuous(self) -> None:
        with self._lock:
            procs = list(self._processes.values())
            self._processes.clear()
        for proc in procs:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Квота и статистика
    # ------------------------------------------------------------------
    def _clip_files(self) -> list[Path]:
        try:
            return [p for p in self.path.rglob("*.mp4") if p.is_file()]
        except OSError:
            return []

    def _enforce_quota(self) -> None:
        quota_gb = float(self._cfg.get("quota_gb") or 0)
        if quota_gb <= 0:
            return
        limit = int(quota_gb * 1024 ** 3)
        files = []
        try:
            for path in self._clip_files():
                try:
                    files.append((path.stat().st_mtime, path.stat().st_size, path))
                except OSError:
                    continue
        except Exception:
            return
        total = sum(item[1] for item in files)
        if total <= limit:
            return
        files.sort(key=lambda item: item[0])
        for _, size, path in files:
            if total <= limit:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
        log.info("archive: квота %.1f ГБ, занято %.2f ГБ", quota_gb, total / 1024 ** 3)

    def _start_cleanup_thread(self) -> None:
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return

        def loop():
            while not self._stop.wait(60):
                self._enforce_quota()

        self._cleanup_thread = threading.Thread(target=loop, name="archive-cleanup", daemon=True)
        self._cleanup_thread.start()

    def stats(self) -> dict:
        files = []
        total = 0
        for path in self._clip_files():
            try:
                size = path.stat().st_size
                files.append({"path": str(path), "name": path.name, "size": size,
                              "mtime": path.stat().st_mtime})
                total += size
            except OSError:
                continue
        try:
            free = shutil.disk_usage(self.path).free
        except OSError:
            free = 0
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "path": str(self.path),
            "quota_gb": float(self._cfg.get("quota_gb") or 0),
            "used_bytes": total,
            "used_gb": round(total / 1024 ** 3, 3),
            "free_bytes": free,
            "free_gb": round(free / 1024 ** 3, 3),
            "clips": len(files),
            "ffmpeg": self.ffmpeg,
            "processes": len(self._processes),
        }

    def clips(self, limit: int = 300) -> list[dict]:
        items = []
        for path in self._clip_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(self.path)
            parts = rel.parts
            camera = parts[0] if len(parts) > 1 else ""
            category = ""
            if "segments" in parts:
                category = "continuous"
            else:
                stem = path.stem
                if "_" in stem:
                    category = stem.rsplit("_", 1)[-1]
            items.append({
                "path": rel.as_posix(),
                "camera": camera,
                "category": category,
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        items.sort(key=lambda item: item["mtime"], reverse=True)
        return items[:max(1, min(limit, 2000))]

    def clip_path(self, relative: str) -> Path | None:
        candidate = (self.path / relative).resolve()
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() and candidate.suffix.lower() == ".mp4" else None

    def delete_clip(self, relative: str) -> bool:
        path = self.clip_path(relative)
        if not path:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def stop(self) -> None:
        self._stop.set()
        self.stop_continuous()
