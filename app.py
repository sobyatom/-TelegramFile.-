import os
import asyncio
import tempfile
import traceback
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import aiohttp
import uvicorn

# -------------------------
# Config (env variables)
# -------------------------
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# -------------------------
# Telethon client
# -------------------------
tele_client = TelegramClient(StringSession(TG_SESSION_STRING) if TG_SESSION_STRING else "bot_session", TG_API_ID, TG_API_HASH)

# -------------------------
# FastAPI + templates
# -------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# -------------------------
# Telegram Bot
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

async def download_to_temp(url: str):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    tmp.close()
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=3600) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await resp.content.read(1024*1024)
                    if not chunk:
                        break
                    f.write(chunk)
    return tmp_path

async def upload_file_to_channel(file_path: str, filename: str):
    sent = await tele_client.send_file(CHANNEL_ID, file_path, caption=filename, force_document=True)
    return sent

async def list_channel_files(limit=50):
    out = []
    async for msg in tele_client.iter_messages(CHANNEL_ID, limit=limit):
        doc = msg.document or msg.video or msg.audio or None
        if doc:
            name = getattr(msg.file, "name", f"message_{msg.id}")
            size = getattr(msg.file, "size", 0)
            date = getattr(msg, "date", None)
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
    if update.message:
        await update.message.reply_text("👋 Bot active. Send a file or /upload <direct_url> to save to Telegram channel.")

async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /upload <direct_url>")
    url = context.args[0].strip()
    status_msg = await update.message.reply_text(f"⏳ Downloading {url} ...")
    try:
        tmp_path = await download_to_temp(url)
        fname = os.path.basename(tmp_path)
        await status_msg.edit_text("⬆️ Uploading to Telegram channel...")
        sent = await upload_file_to_channel(tmp_path, fname)
        os.remove(tmp_path)
        link = tme_link_for(CHANNEL_ID, sent.id)
        await status_msg.edit_text(f"✅ Uploaded: {fname}\n{link}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")

bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("upload", upload_cmd))
bot_app.add_handler(MessageHandler(filters.ALL, upload_cmd))

# -------------------------
# Web routes
# -------------------------
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
# Startup
# -------------------------
@app.on_event("startup")
async def startup_event():
    await tele_client.start()
    await bot_app.initialize()

# -------------------------
# Run app
# -------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), workers=1)
