# Personal-Telegram-
#

Store unlimited files on Telegram and stream/download them via a password-protected web index.  
Supports direct link uploads, Telegram file uploads, single merged download, and pause/resume.

---

## 🚀 Deploy to Koyeb

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&repository=github.com/sobyatom/Personal-Telegrambot&branch=main&name=Personal-Telegram-bot)

---

## ⚙️ Environment Variables

| Key         | Description                     |
|-------------|---------------------------------|
| `BOT_TOKEN` | Telegram Bot token (from @BotFather) |
| `API_ID`    | Telegram API ID (from my.telegram.org) |
| `API_HASH`  | Telegram API Hash (from my.telegram.org) |
| `CHANNEL_ID`| Private channel/group ID where files are stored |
| `WEB_PASSWORD` | Password for web index access |

---

## 📖 Usage

- Send any **file/video/document** to the bot → saved to Telegram + indexed.  
- Use `/upload <direct_link>` → bot downloads and saves file.  
- Visit `https://your-koyeb-app.koyeb.app/?password=WEB_PASSWORD` → see file list.  
- Click download → supports **pause/resume** via HTTP Range.
