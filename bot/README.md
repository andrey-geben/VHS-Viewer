# Боты VHS-Viewer

Монорепо ботов, которые управляют реле VHS-Viewer через HTTP-эндпоинт
`/api/relay/<id>/open` (авторизация — `X-Relay-Token`).

## Структура

- `common/` — общий код (клиент реле, загрузка `.env`, логи). **Не меняйте без необходимости.**
- `vk-bot/` — **эталон**, рабочий.
- `max-bot/`, `tg-bot/`, `whats-bot/` — заготовки. См. README.md в каждой.
- `logs/` — создаётся автоматически, в `.gitignore`.

## Установка (один раз)

```powershell
cd bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements-common.txt
pip install -r vk-bot\requirements.txt