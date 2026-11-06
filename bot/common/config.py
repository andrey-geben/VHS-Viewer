"""Bootstrap-конфиг бота."""
import os
from pathlib import Path
from dotenv import load_dotenv


def load_bootstrap(bot_prefix=None):
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    load_dotenv(root / ".env.local", override=True)
    bot_id = os.environ.get("BOT_ID", "")
    if not bot_id and bot_prefix:
        bot_id = os.environ.get(f"{bot_prefix}_BOT_ID", "")
    return {
        "viewer_url": os.environ.get("VIEWER_API", "http://127.0.0.1:8200"),
        "config_secret": os.environ.get("BOT_CONFIG_SECRET", ""),
        "relay_secret": os.environ.get("RELAY_SECRET", ""),
        "bot_id": bot_id,
    }
