# Personal-Telegram-


[![Deploy to Koyeb](https://www.koyeb.com/deploy/button.svg)](https://app.koyeb.com/deploy?repository=https://github.com/sobyatom/Personal-Telegram-bot)

Upload huge files to Telegram (auto-split >2GB), list them on a password-protected web index, and serve a **single merged download** with **pause/resume**.

## Features
- `/url <direct_link>` → uploads any size (splits into ~1.9GB parts)
- Send a document → forwarded to your private channel and indexed
- Web index at `/?password=WEB_PASSWORD`
- `/download/<bundle>` merges chunks on-the-fly (resume supported)
- Webhook mode: fully reliable on Koyeb free tier
- Real-time **upload/splitting status** visible in bot chat

## Environment Variables
- `BOT_TOKEN` – Telegram bot token  
- `CHANNEL_ID` – Target channel for uploads  
- `WEB_PASSWORD` – Password for web index  
- `TG_API_ID` / `TG_API_HASH` – For Telethon  
- `PORT` – Koyeb port (default 8080)
