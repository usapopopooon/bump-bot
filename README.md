# bump-bot

[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](#)

Discordのbump成功を検知し、一定時間後にリマインドを送信するBot。

## Requirements
- Python 3.12
- PostgreSQL

## Environment Variables
- `DISCORD_TOKEN` (required)
- `DATABASE_URL` (optional for Docker Compose, default: internal PostgreSQL)
- `LOG_LEVEL` (optional, default: `INFO`)
- `DB_POOL_SIZE` (optional, default: `1`)
- `DB_MAX_OVERFLOW` (optional, default: `0`)

Example:
```env
DISCORD_TOKEN=your-discord-bot-token
POSTGRES_DB=bump_bot
POSTGRES_USER=bump_bot
POSTGRES_PASSWORD=bump_bot_password
DATABASE_URL=postgresql+asyncpg://bump_bot:bump_bot_password@db:5432/bump_bot
LOG_LEVEL=INFO
DB_POOL_SIZE=1
DB_MAX_OVERFLOW=0
```

## Run (Coolify)
- Use `docker-compose.yml`
- Set `DISCORD_TOKEN` in Coolify environment variables
- The compose file includes PostgreSQL as `db`
- Recommended for a single-server deployment:
  - Bot memory limit: `128 MB`
  - Bot memory reservation: `64 MB`
  - PostgreSQL memory limit: `256 MB`
  - PostgreSQL memory reservation: `128 MB`
  - `DB_POOL_SIZE=1`
  - `DB_MAX_OVERFLOW=0`
  - If the bot restarts with OOM, raise the bot limit to `192 MB`

## Run (Docker Compose)
```bash
docker compose up --build
```

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
