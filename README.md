# bump-bot

[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](#)

Discordのbump成功を検知し、一定時間後にリマインドを送信するBot。

## Requirements
- Python 3.12
- PostgreSQL

## Environment Variables
- `DISCORD_TOKEN` (required)
- `DATABASE_URL` (required)
- `LOG_LEVEL` (optional, default: `INFO`)
- `DB_POOL_SIZE` (optional, default: `1`)
- `DB_MAX_OVERFLOW` (optional, default: `1`)

Example:
```env
DISCORD_TOKEN=your-discord-bot-token
DATABASE_URL=postgresql+asyncpg://user:password@localhost/discord_util_bot
LOG_LEVEL=INFO
DB_POOL_SIZE=1
DB_MAX_OVERFLOW=1
```

## Run (Coolify)
- Build: `Dockerfile`
- Start command: `python -m src.main`
- Recommended for a single-server deployment:
  - Memory limit: `256 MB`
  - Memory reservation: `128 MB`
  - `DB_POOL_SIZE=1`
  - `DB_MAX_OVERFLOW=1`

## Install
```bash
pip install -r requirements.txt
```

## Run (Local)
```bash
python -m src.main
```

## Main Files
- `src/main.py`
- `src/bot.py`
- `src/cogs/bump.py`
- `src/services/bump_service.py`
- `src/database/models.py`
- `src/database/engine.py`
