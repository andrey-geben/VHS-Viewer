"""Отправка уведомлений в Telegram."""

import logging

import requests

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: int = 10):
        self.api = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> bool:
        try:
            r = requests.post(
                self.api,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=self.timeout,
            )
            if not r.ok:
                log.error(f"telegram error: {r.status_code} {r.text[:200]}")
                return False
            return True
        except Exception:
            log.exception("telegram send failed")
            return False
