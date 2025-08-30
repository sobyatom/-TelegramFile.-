# app.py
import os
import io
import asyncio
import tempfile
import traceback
from datetime import datetime

import aiohttp
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from telethon import TelegramClient, types
from telethon.sessions import StringSession

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import RetryAfter

import uvicorn

# -------------------------
# Config (from env)
# -------------------------
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW else None

WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")
if os.getenv("WEBHOOK_URL"):
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
else:
    hostname = os.getenv("KOYEB_APP_HOSTNAME")
    WEBHOOK_URL = f"https://{hostname}" if hostname else ""

# -------------------------
# Telethon client
# -------------------------
if TG_SESSION_STRING:
    tele_client = TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH)
else:
    tele_client = TelegramClient("bot_session", TG_API_ID, TG_API_HASH)

# -------------------------
# FastAPI + templates
# -------------------------
app = FastAPI(title="Telegram Link→Link Indexer")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# -------------------------
# python-telegram-bot
# -------------------------
bot_app = Application.builder().token(BOT_TOKEN).build()

# -------------------------
# Helpers
# -------------------------
def tme_link_for(channel_id: int, msg_id: int) -> str:
    s = str(channel_id)
    if s.startswith("-100"):
        short = s[4:]
        return f"https://t.me/c/{short}/{msg_id}"
    return f"https://t.me/{channel_id}/{msg_id}"

async def download_to_temp(url: str, progress_cb=None, chunk_size=1024*1024):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    tmp.close()
    total = None
    downloaded = 0
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=3600) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length") or 0) or None
            async for chunk in resp.content.iter_chunked(chunk_size):
                if not chunk:
                    break
                with open(tmp_path, "ab") as f:
                    f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    await progress_cb(downloaded, total)
    return tmp_path, total

async def upload_file_to_channel(file_path: str, filename: str, caption: str = None, progress_cb=None):
    if CHANNEL_ID is None:
        raise RuntimeError("CHANNEL_ID not configured")
    sent = await tele_client.send_file(CHANNEL_ID, file_path, caption=caption, force_document=True)
    return sent

async def list_channel_files(limit=50):
    out = []
    async for msg in tele_client.iter_messages(CHANNEL_ID, limit=limit):
        if msg is None:
            continue
        doc = msg.document or msg.video or msg.audio or None
        if doc:
            name = None
            try:
                if getattr(msg, "file", None):
                    name = msg.file.name
                elif getattr(msg, "document", None):
                    name = getattr(msg, "file", None) and msg.file.name
                if not name:
                    name = msg.file.name if getattr(msg, "file", None) else f"message_{msg.id}"
            except Exception:
                name = f"message_{msg.id}"
            size = msg.file.size if getattr(msg, "file", None) else (getattr(msg, "document", None) and msg.document.size) or 0
            date = msg.date if hasattr(msg, "date") else None
            out.append({
                "name": name,
                "size": size,
                "date": date,
                "msg_id": msg.id,
                "link": tme_link_for(CHANNEL_ID, msg.id)
            })
    return out

# -------------------------
# Bot handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📩 /start received from", update.effective_user.id if update.effective_user else "unknown")
    if update.message:
        await update.message.reply_text("👋 Bot active. Send a file or /upload <direct_url> to save to Telegram channel.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📩 /help received")
    if update.message:
        await update.message.reply_text("Commands:\n/upload <url> - download and upload to channel\nSend file/document to forward to channel.")

async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📩 /upload called")
    if not context.args:
        return await update.message.reply_text("Usage: /upload <direct_url>")
    url = context.args[0].strip()
    status_msg = await update.message.reply_text(f"⏳ Downloading {url} ...")
    try:
        async def progress_cb(downloaded, total):
            if total:
                pct = int(downloaded * 100 / total)
                await status_msg.edit_text(f"⏳ Downloading {pct}% ({downloaded//(1024*1024)} MiB)")
            else:
                await status_msg.edit_text(f"⏳ Downloaded {downloaded//(1024*1024)} MiB")

        tmp_path, total = await download_to_temp(url, progress_cb=progress_cb)
        fname = os.path.basename(tmp_path) if tmp_path else url.split("/")[-1]
        await status_msg.edit_text("⬆️ Uploading to Telegram channel...")
        sent = await upload_file_to_channel(tmp_path, filename=fname, caption=fname)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        link = tme_link_for(CHANNEL_ID, sent.id)
        await status_msg.edit_text(f"✅ Uploaded: {fname}\n{link}")
    except Exception as e:
        tb = traceback.format_exc()
        print("Error in upload_cmd:", e, tb)
        await status_msg.edit_text(f"❌ Error: {e}")

async def on_message_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message and update.message.document:
            await handle_incoming_document(update, context)
            return
        if update.message and update.message.video:
            await handle_incoming_media(update, context, media_attr="video")
            return
        if update.message and update.message.audio:
            await handle_incoming_media(update, context, media_attr="audio")
            return
        if update.message and update.message.text and update.message.text.startswith("http"):
            context.args = [update.message.text.strip()]
            await upload_cmd(update, context)
            return
    except Exception as e:
        print("Error in on_message_all:", e, traceback.format_exc())

async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_attr="video"):
    status = await update.message.reply_text("⏳ Forwarding to channel...")
    try:
        fwd = await update.message.forward(chat_id=CHANNEL_ID)
        link = tme_link_for(CHANNEL_ID, fwd.message_id)
        await status.edit_text(f"✅ Forwarded: {link}")
    except Exception as e:
        print("Error forwarding media:", e, traceback.format_exc())
        await status.edit_text(f"❌ Forward failed: {e}")

async def handle_incoming_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⏳ Forwarding document to channel...")
    try:
        fwd = await update.message.forward(chat_id=CHANNEL_ID)
        link = tme_link_for(CHANNEL_ID, fwd.message_id)
        await status.edit_text(f"✅ Document forwarded: {link}")
    except Exception as e:
        print("Error forwarding document:", e, traceback.format_exc())
        await status.edit_text(f"❌ Forward failed: {e}")

bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("help", help_cmd))
bot_app.add_handler(CommandHandler("upload", upload_cmd))
bot_app.add_handler(MessageHandler(filters.ALL, on_message_all))

# -------------------------
# FastAPI routes
# -------------------------
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    print("📥 Incoming update (webhook):", data)
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return PlainTextResponse("OK")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == WEB_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Wrong password"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse("/login")
    files = await list_channel_files(limit=50)
    for f in files:
        f["date_iso"] = f["date"].isoformat() if f["date"] else ""
    return templates.TemplateResponse("index.html", {"request": request, "files": files})

@app.get("/download/telegram/{msg_id}")
async def download_via_telegram(msg_id: int):
    msg = await tele_client.get_messages(CHANNEL_ID, ids=msg_id)
    if not msg or not msg.document:
        return PlainTextResponse("File not found", status_code=404)

    async def streamer():
        async for chunk in tele_client.iter_download(msg.document, chunk_size=1024*512):
            yield chunk

    headers = {"Content-Disposition": f'inline; filename="{getattr(msg.document, "file_name", "file")}"'}
    return StreamingResponse(streamer(), headers=headers)

# -------------------------
# Startup & shutdown events
# -------------------------
@app.on_event("startup")
async def startup_event():
    print("🚀 Starting Telethon client and initializing webhook/polling...")
    await tele_client.start()
    await bot_app.initialize()

    if WEBHOOK_URL:
        wh = f"{WEBHOOK_URL}/webhook"
        print("🧹 Deleting any old webhook...")
        try:
            await bot_app.bot.delete_webhook()
            print("✅ Old webhook deleted")
        except Exception as e:
            print("⚠️ Failed to delete old webhook:", e)

        print("🔗 Setting new webhook
