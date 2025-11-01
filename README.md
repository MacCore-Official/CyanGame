# CYAN DOLLARS — Slash Command Bot (Railway-ready)

Virtual currency + minigames with slash commands:
- `/balance`, `/daily`, `/coinflip`, `/slots`, `/leaderboard`, `/redeem`
- Uses `CYAN_TOKEN` env var (not `DISCORD_TOKEN`)
- SQLite for storage

## 1) Create the Discord Bot
1. https://discord.com/developers/applications → **New Application**
2. **Bot** → *Add Bot* → copy the **token** (keep private)
3. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Read Message History`
4. Invite bot to your server with the generated link.

## 2) Deploy on Railway
1. Railway → **New Project** → **Deploy from GitHub** (or upload the files and init a repo)
2. In the service → **Variables**:
   - `CYAN_TOKEN` = your Discord bot token
   - (optional) `GUILD_ID` = your server ID for instant slash sync
3. Railway auto-detects `Procfile` and runs:
