# Personal-Telegram-
#
# Telegram Index Bot (Koyeb One-Click)

Upload huge files to Telegram (auto-split >2GB), list them on a password-protected web index, and serve a **single merged download** with **HTTP Range** (pause/resume).

## Features
- `/url <direct_link>` → uploads any size (splits into ~1.9GB parts)
- Send a document to the bot → forwarded to your private channel and indexed
- Web index at `/?password=WEB_PASSWORD`
- `/download/<bundle>` merges parts on-the-fly (resume supported)

## Deploy on Koyeb
1. Fork this repo to your GitHub.
2. Create a Koyeb service from this repo.
3. Set env vars:
   - `BOT_TOKEN` (from @BotFather)
   - `CHANNEL_ID` (private channel where the bot is admin; starts with `-100`)
   - `WEB_PASSWORD` (index password)
   - `SECRET_KEY` (random string)
4. Open `https://<your-app>.koyeb.app/?password=WEB_PASSWORD`

## Usage
- In Telegram, start your bot:
  - `/url https://example.com/bigfile.iso`
  - Send a document → it’s forwarded & indexed.
- Visit the index and click **Download**. Resumable & single merged stream.

## Notes
- No API_ID/API_HASH needed (pure Bot API).
- Koyeb storage isn’t used for full files; each part is written to a temp file then uploaded and removed.
- Chunk size is ~1.9GB to fit Koyeb free tier ephemeral disk.
