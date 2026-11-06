"""Клиент /api/bots/<id>/config."""
import asyncio
import logging
import aiohttp

log = logging.getLogger(__name__)


class BotConfigError(Exception):
    pass


async def fetch_bot_config(bot_id, viewer_url, secret,
                           timeout=5.0, max_retries=30, retry_delay=2.0):
    url = f"{viewer_url.rstrip('/')}/api/bots/{bot_id}/config"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"X-Bot-Token": secret},
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    text = await r.text()
                    if r.status == 200:
                        return await r.json()
                    if r.status == 401:
                        raise BotConfigError("401: BOT_CONFIG_SECRET не совпадает с viewer.")
                    if r.status == 403:
                        raise BotConfigError(f"403: бот '{bot_id}' выключен в UI /bots.")
                    if r.status == 404:
                        raise BotConfigError(f"404: бот '{bot_id}' не найден. Проверьте BOT_ID и id в UI.")
                    raise BotConfigError(f"HTTP {r.status}: {text[:200]}")
        except aiohttp.ClientConnectorError as e:
            last_err = f"viewer недоступен: {e}"
            log.warning("попытка %d/%d: %s", attempt, max_retries, last_err)
            await asyncio.sleep(retry_delay)
            continue
        except BotConfigError:
            raise
        except Exception as e:
            last_err = str(e)
            log.exception("неожиданная ошибка fetch_bot_config")
            await asyncio.sleep(retry_delay)
            continue
    raise BotConfigError(f"не удалось получить конфиг после {max_retries} попыток: {last_err}")
