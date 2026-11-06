# Боты VHS-Viewer
## Границы проекта

Этот каталог — боты для **VHS-Viewer** (события видеоаналитики:
ALPR, детекция объектов, движение). Они уведомляют владельцев камер
и открывают реле **по событиям аналитики**.

**Домофон и звонки в квартиры — НЕ здесь.** Это отдельный проект
**VHS-Intercom** (соседний репозиторий). Боты для жильцов живут там.

Общий код (`common/`) копируется в VHS-Intercom при его создании.
Изменения в `bot/common/` нужно синхронизировать между проектами
(пока вручную; если станет больно — вынесем в общий пакет).

**Матрица ролей:**

| Проект | Триггеры | Действия |
|---|---|---|
| VHS-Streamer | камера офлайн, диск, RTSP | уведомить админа |
| VHS-Viewer | ALPR, объект, движение | уведомить + реле по ALPR |
| VHS-Intercom | звонок в квартиру | уведомить владельца + реле по команде |

K-бот — эталон. MAX/TG/WhatsApp — заготовки.

## Установка

    cd bot
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    pip install -r requirements-common.txt
    pip install -r vk-bot\requirements.txt

## Запуск

    python vk-bot\bot.py --check     # только проверка конфига
    python vk-bot\bot.py             # полный запуск

## Конфигурация

Bootstrap в корневом .env: VIEWER_API, BOT_CONFIG_SECRET, BOT_ID, RELAY_SECRET.
Всё остальное (токен мессенджера, allowed_user_ids, relay_id) — в UI /bots.
