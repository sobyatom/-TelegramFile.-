import os, json, asyncio, sqlite3, aiohttp
from fastapi import FastAPI, Request, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from telethon.tl.types import Document

# ───────────── CONFIG ─────────────
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # channel/group to forward uploads

WEB_PASSWORD = os.getenv("WEB_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")
DB_PATH = os.getenv("DB_PATH", "files.db")

# ───────────── DB ─────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name TEXT,
  file_size INTEGER,
  message_id INTEGER,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
con = sqlite3.connect(DB_PATH)
con.execute(SCHEMA)
con.commit()
con.close()

# ───────────── TELEGRAM ─────────────
client = TelegramClient("bot", TG_API_ID, TG_API_HASH)

# ───────────── FASTAPI ─────────────
app = FastAPI(title="Telegram Index Bot")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# ───────────── UTILS ─────────────
def db():
    return sqlite3.connect(DB_PATH)

async def save_file_info(name, size, mid):
    with db() as con:
        con.execute("INSERT INTO files (file_name, file_size, message_id) VALUES (?,?,?)",
                    (name, size, mid))
        con.commit()

# ───────────── WEB ROUTES ─────────────
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("auth"):
        return RedirectResponse(url="/index")
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == WEB_PASSWORD:
        request.session["auth"] = True
        return RedirectResponse(url="/index", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})

@app.get("/index", response_class=HTMLResponse)
async def index(request: Request):
    if not request.session.get("auth"):
        return RedirectResponse(url="/")
    with db() as con:
        rows = con.execute("SELECT id,file_name,file_size,message_id FROM files ORDER BY created_at DESC").fetchall()
    files = [{"id": r[0], "name": r[1], "size": r[2], "mid": r[3]} for r in rows]
    return templates.TemplateResponse("index.html", {"request": request, "files": files})

@app.get("/download/{msg_id}")
async def download(msg_id: int):
    async with client:
        msg = await client.get_messages(CHANNEL_ID, ids=msg_id)
        if not msg or not msg.document:
            raise HTTPException(404, "File not found in Telegram")
        doc: Document = msg.document

        async def file_iter():
            async for chunk in client.iter_download(doc, chunk_size=1024 * 512):
                yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{doc.attributes[0].file_name}"',
            "Content-Length": str(doc.size),
        }
        return StreamingResponse(file_iter(), headers=headers)

# ───────────── BOT HANDLERS ─────────────
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_APP = Application.builder().token(TG_BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a direct URL or upload a file, I'll forward to channel & index it.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        return
    doc = update.message.document
    # forward to channel
    fwd = await context.bot.forward_message(chat_id=CHANNEL_ID, from_chat_id=update.effective_chat.id, message_id=update.message.id)
    await save_file_info(doc.file_name, doc.file_size, fwd.message_id)
    await update.message.reply_text(f"✅ Uploaded & indexed: {doc.file_name}")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        return
    await update.message.reply_text("⏳ Downloading file from URL...")
    filename = url.split("/")[-1]
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                await update.message.reply_text("❌ Failed to fetch URL")
                return
            tmp_path = f"/tmp/{filename}"
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await resp.content.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
    # upload to channel
    async with client:
        msg = await client.send_file(CHANNEL_ID, tmp_path, caption=filename)
    await save_file_info(filename, os.path.getsize(tmp_path), msg.id)
    await update.message.reply_text(f"✅ Uploaded & indexed from URL: {filename}")
    os.remove(tmp_path)

BOT_APP.add_handler(CommandHandler("start", start))
BOT_APP.add_handler(MessageHandler(filters.Document.ALL, handle_file))
BOT_APP.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url))

# ───────────── STARTUP ─────────────
@app.on_event("startup")
async def startup():
    await client.start(bot_token=TG_BOT_TOKEN)
    asyncio.create_task(BOT_APP.run_polling())

@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()
