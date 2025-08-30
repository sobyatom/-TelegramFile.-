import os
import asyncio
import aiohttp
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
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
    await update.message.reply_text("👋 Bot is alive!\nSend me a file or direct link to upload to Telegram.")

async def help_cmd(update: Update, context):
    await update.message.reply_text("ℹ️ Commands:\n/start - check bot\n/help - help info\nJust send me a file or link.")

# =========================
# File & Link Handling
# =========================
async def handle_message(update: Update, context):
    if update.message.document or update.message.video or update.message.audio:
        # Forward uploaded file to channel
        msg = await update.message.forward(CHANNEL_ID)
        await update.message.reply_text(f"✅ File uploaded to channel.\nLink: https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}")
    elif update.message.text and update.message.text.startswith("http"):
        # Download from direct link then upload
        url = update.message.text.strip()
        await update.message.reply_text("⏳ Downloading your file, please wait...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        fname = url.split("/")[-1] or "file.bin"
                        data = await resp.read()
                        sent = await context.bot.send_document(
                            chat_id=CHANNEL_ID,
                            document=data,
                            filename=fname
                        )
                        await update.message.reply_text(f"✅ Uploaded from link.\nLink: https://t.me/c/{str(CHANNEL_ID)[4:]}/{sent.id}")
                    else:
                        await update.message.reply_text("❌ Failed to download file.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error: {e}")

# =========================
# Telegram Bot Application
# =========================
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", help_cmd))
bot_app.add_handler(MessageHandler(filters.ALL, handle_message))

# =========================
# Startup & Shutdown
# =========================
@app.on_event("startup")
async def startup():
    await client.start()
    if WEBHOOK_URL:
        for attempt in range(5):
            try:
                await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}")
                print("Webhook set successfully")
                break
            except RetryAfter as e:
                print(f"Flood control, retrying in {e.retry_after} seconds...")
                await asyncio.sleep(e.retry_after)
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
        files = []
        async for msg in client.iter_messages(CHANNEL_ID, limit=20):
            if msg.document or msg.video or msg.audio:
                fname = msg.file.name if msg.file else "unnamed"
                link = f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}"
                files.append({"name": fname, "link": link})
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
