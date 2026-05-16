# bump-bot

Discordのbump成功を検知し、一定時間後にリマインドを送信するBot。

## Requirements
- Python 3.12
- PostgreSQL

## Environment Variables
- `DISCORD_TOKEN` (required)
- `DATABASE_URL` (required)
- `LOG_LEVEL` (optional, default: `INFO`)

Example:
```env
DISCORD_TOKEN=your-discord-bot-token
DATABASE_URL=postgresql+asyncpg://user:password@localhost/discord_util_bot
LOG_LEVEL=INFO
```

## Install
```bash
pip install -r requirements.txt
```

## Run (Local)
```bash
python -m src.main
```

## Run (Railway)
- Build: `Dockerfile`
- Start command: `python -m src.main`
- `railway.toml` is included.
- `Procfile` is included (`worker: python -m src.main`).

## Main Files
- `src/main.py`
- `src/bot.py`
- `src/cogs/bump.py`
- `src/services/bump_service.py`
- `src/database/models.py`
- `src/database/engine.py`
