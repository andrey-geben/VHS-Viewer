"""VK-бот (эталон). Конфиг берётся из /api/bots/<id>/config."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import load_bootstrap
from common.config_client import BotConfigError, fetch_bot_config
from common.logging_setup import setup_logging

log = setup_logging("vk-bot")


def _fetch_config_or_die():
    boot = load_bootstrap("VK")
    if not boot["bot_id"]:
        log.error("BOT_ID не задан в .env"); sys.exit(1)
    if not boot["config_secret"]:
        log.error("BOT_CONFIG_SECRET не задан в .env"); sys.exit(1)
    if not boot["relay_secret"]:
        log.error("RELAY_SECRET не задан в .env"); sys.exit(1)

    log.info("Запрашиваю конфиг: %s/api/bots/%s/config",
             boot["viewer_url"], boot["bot_id"])
    try:
        cfg = asyncio.run(fetch_bot_config(
            bot_id=boot["bot_id"],
            viewer_url=boot["viewer_url"],
            secret=boot["config_secret"]))
    except BotConfigError as e:
        log.error("Не удалось получить конфиг: %s", e); sys.exit(1)

    tok = cfg.get("token") or ""
    log.info("Конфиг получен: id=%s, type=%s, allowed_user_ids=%d, relay_id=%s, token=%s",
             cfg.get("id"), cfg.get("type"),
             len(cfg.get("allowed_user_ids") or []),
             cfg.get("relay_id"),
             ("*" * 8 + tok[-4:]) if tok else "(пусто)")
    if not tok:
        log.error("В UI /bots у бота пустой token"); sys.exit(1)
    return {**boot, **cfg}


CFG = _fetch_config_or_die()


def _is_allowed(user_id):
    return user_id in set(CFG.get("allowed_user_ids") or [])


if "--check" in sys.argv:
    log.info("Режим --check: конфиг получен, выхожу без VK SDK")
    sys.exit(0)


from vkbottle.bot import Bot, Message
from common.relay_client import open_relay, relay_status

bot = Bot(token=CFG["token"])


@bot.on.message(text=["/start", "начать", "Start"])
async def cmd_start(message: Message):
    uid = message.from_id
    if not _is_allowed(uid):
        log.warning("неавторизованный /start от uid=%s", uid)
        await message.answer(
            f"Нет прав.\nВаш VK ID: {uid}\n"
            f"Добавьте его в UI VHS-Viewer /bots.")
        return
    await message.answer(
        "Бот управления дверью.\n\n/open - открыть\n/status - состояние\n/id - VK ID")


@bot.on.message(text="/id")
async def cmd_id(message: Message):
    await message.answer(f"Ваш VK ID: {message.from_id}")


@bot.on.message(text=["/open", "открой", "Открыть"])
async def cmd_open(message: Message):
    uid = message.from_id
    if not _is_allowed(uid):
        await message.answer("Нет прав на открытие двери."); return
    log.info("/open от uid=%s, relay=%s", uid, CFG["relay_id"])
    await message.answer("Открываю...")
    ok, text = await open_relay(
        relay_id=CFG["relay_id"],
        viewer_url=CFG["viewer_url"],
        secret=CFG["relay_secret"])
    await message.answer(text)


@bot.on.message(text="/status")
async def cmd_status(message: Message):
    if not _is_allowed(message.from_id):
        await message.answer("Нет прав."); return
    ok, text = await relay_status(
        relay_id=CFG["relay_id"],
        viewer_url=CFG["viewer_url"],
        secret=CFG["relay_secret"])
    await message.answer(f"Viewer OK. Реле: {text[:200]}" if ok
                         else f"Viewer недоступен: {text}")


def main():
    log.info("=" * 60)
    log.info("VK-бот запускается")
    log.info("Viewer: %s", CFG["viewer_url"])
    log.info("Bot ID: %s", CFG["id"])
    log.info("Relay ID: %s", CFG["relay_id"])
    log.info("Разрешённых VK ID: %d", len(CFG.get("allowed_user_ids") or []))
    if not CFG.get("allowed_user_ids"):
        log.warning("allowed_user_ids пуст — бот откажет всем.")
    log.info("=" * 60)
    bot.run_forever()


if __name__ == "__main__":
    main()
