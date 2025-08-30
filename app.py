import os
import asyncio
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.error import RetryAfter
import uvicorn

# =========================
# Config
# =========================
TG_API_ID = int(os.getenv("TG_API_ID", 0))
TG_API_HASH = os.getenv("TG_API_HASH")
SESSION_STRING = os.getenv("TG_SESSION_STRING")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")

# =========================
# Telethon Client
# =========================
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), TG_API_ID, TG_API_HASH)
else:
    client = TelegramClient("bot", TG_API_ID, TG_API_HASH)

# =========================
# FastAPI Setup
# =========================
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="supersecret")
templates = Jinja2Templates(directory="templates")

# =========================
# Bot Commands
# =========================
async def start(update: Update, context):
    await update.message.reply_text("👋 Bot is alive! Send me a file or link to upload.")

async def help_cmd(update: Update, context):
    await update.message.reply_text("ℹ️ Commands:\n/start - check bot\n/help - help info")

# =========================
# Telegram Bot Application
# =========================
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", help_cmd))

# =========================
# Startup & Shutdown
# =========================
@app.on_event("startup")
async def startup():
    await client.start()
    if WEBHOOK_URL:
        # Retry webhook up to 5 times
        for attempt in range(5):
            try:
                await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}")
                print("Webhook set successfully")
                break
            except RetryAfter as e:
                print(f"Flood control, retrying in {e.retry_after} seconds...")
                await asyncio.sleep(e.retry_after)
        else:
            print("Failed to set webhook after multiple attempts")
    else:
        await bot_app.initialize()
        await bot_app.start()
        print("Polling started")

@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()
    if not WEBHOOK_URL:
        await bot_app.stop()
    print("Shutdown complete")

# =========================
# Webhook for Telegram
# =========================
@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != BOT_TOKEN:
        return PlainTextResponse("Unauthorized", status_code=401)
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return PlainTextResponse("OK")

# =========================
# Web Interface
# =========================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if request.session.get("logged_in"):
        files = ["example1.mp4", "example2.zip"]  # TODO: fetch real Telegram files
        return templates.TemplateResponse("index.html", {"request": request, "files": files})
    return RedirectResponse("/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == WEB_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return PlainTextResponse("❌ Wrong password", status_code=401)

# =========================
# Main
# =========================
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), workers=1)
