# bump-bot

`discord-util-bot` の bump 機能を単体化した Bot です。

## 機能
- DISBOARD と ディス速報 の bump 成功検知
- 2時間後のリマインダー送信
- `/bump setup` などの設定コマンド
- 通知ロール切り替え

## ローカル実行
1. `cp .env.example .env`
2. `.env` の `DISCORD_TOKEN` と `DATABASE_URL` を設定
3. `pip install -r requirements.txt`
4. `python -m src.main`

## Railway
- `railway.toml` は `dockerfile` ビルダーを使用
- 起動コマンドは `python -m src.main`（`Procfile` の worker と同じ）
- Railway 側で最低限以下を設定:
  - `DISCORD_TOKEN`
  - `DATABASE_URL` (PostgreSQL)
  - `LOG_LEVEL` (任意, 例: `INFO`)

## 注意
- DB テーブルは起動時に自動作成されます。
- 実装本体は `src/cogs/bump.py` と `src/services/bump_service.py` です。
