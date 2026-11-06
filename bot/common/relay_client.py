"""Клиент эндпоинта реле VHS-Viewer."""
import logging
import aiohttp

log = logging.getLogger(__name__)


async def open_relay(relay_id, viewer_url, secret, timeout=5.0):
    url = f"{viewer_url.rstrip('/')}/api/relay/{relay_id}/open"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers={"X-Relay-Token": secret},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                text = await r.text()
                log.info("viewer -> HTTP %s %s", r.status, text[:200])
                if r.status == 200:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {}
                    if data.get("ok"):
                        return True, "Дверь открыта."
                    return False, f"Реле: {data.get('error', text[:100])}"
                return False, f"Viewer HTTP {r.status}: {text[:100]}"
    except aiohttp.ClientConnectorError as e:
        return False, f"Viewer недоступен: {e}"
    except Exception as e:
        log.exception("open_relay failed")
        return False, f"Ошибка: {e}"


async def relay_status(relay_id, viewer_url, secret, timeout=5.0):
    url = f"{viewer_url.rstrip('/')}/api/relay/{relay_id}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"X-Relay-Token": secret},
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    return True, str(await r.json())
                return False, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)
