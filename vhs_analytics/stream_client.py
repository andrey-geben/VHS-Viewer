"""Клиент публичного API VHS-Streamer. Получает список потоков + RTSP-адреса."""

import logging
import requests

log = logging.getLogger(__name__)


class StreamerClient:
    def __init__(self, base_url: str, rtsp_base: str = None, timeout: int = 5):
        self.base_url = base_url.rstrip("/")
        self.rtsp_base_override = rtsp_base.rstrip("/") if rtsp_base else None
        self.timeout = timeout
        self._rtsp_host = self._extract_host(self.base_url)

    @staticmethod
    def _extract_host(url: str) -> str:
        """Из 'http://127.0.0.1:8000' -> '127.0.0.1'."""
        if "://" in url:
            url = url.split("://", 1)[1]
        host_part = url.split("/")[0]
        return host_part.split(":")[0]

    def get_streams(self) -> list[dict]:
        """
        Возвращает нормализованный список потоков:
        [{ "id", "name", "webrtc_url", "rtsp_url" }]

        Реальная структура ответа VHS-Streamer:
        {
          "count": 3,
          "rtsp_port": 8554,
          "webrtc_port": 8889,
          "server_name": "VHS-Streamer",
          "streams": [
            {
              "id": "analog_1_s1",
              "name": "Встроенная камера / analog_1_s1",
              "camera_name": "Встроенная камера",
              "path": "/analog_1_s1",
              "webrtc_urls": [ {"host": "...", "label": "...", "url": "..."}, ... ]
            }, ...
          ]
        }
        """
        url = f"{self.base_url}/api/public/streams"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"GET {url} failed: {e}")
            return []

        if not isinstance(data, dict):
            log.error(f"unexpected response type: {type(data).__name__}")
            return []

        # RTSP-порт берём из ответа сервера — это надёжнее хардкода
        rtsp_port = data.get("rtsp_port", 8554)
        raw_list = data.get("streams", [])
        if not isinstance(raw_list, list):
            log.error(f"unexpected 'streams' type: {type(raw_list).__name__}")
            return []

        streams = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            stream_id = item.get("id")
            if not stream_id:
                continue

            # Отображаемое имя: camera_name лучше, чем "camera_name / id"
            display_name = item.get("camera_name") or item.get("name") or stream_id

            # RTSP: путь берём из поля path (или собираем из id как fallback)
            path = item.get("path") or f"/{stream_id}"
            if self.rtsp_base_override:
                rtsp_url = f"{self.rtsp_base_override}{path}"
            else:
                rtsp_url = f"rtsp://{self._rtsp_host}:{rtsp_port}{path}"

            # WebRTC URL: берём первый, подходящий по host, иначе первый из списка
            webrtc_url = None
            webrtc_list = item.get("webrtc_urls") or []
            if isinstance(webrtc_list, list) and webrtc_list:
                for u in webrtc_list:
                    if isinstance(u, dict) and u.get("host") == self._rtsp_host:
                        webrtc_url = u.get("url")
                        break
                if webrtc_url is None and isinstance(webrtc_list[0], dict):
                    webrtc_url = webrtc_list[0].get("url")

            streams.append({
                "id": stream_id,
                "name": display_name,
                "webrtc_url": webrtc_url,
                "rtsp_url": rtsp_url,
            })

        log.info(f"получено потоков: {len(streams)} (rtsp_port={rtsp_port})")
        return streams