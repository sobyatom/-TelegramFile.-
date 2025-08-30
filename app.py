        import os, json, io, aiohttp, asyncio, sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from telegram import Update, Bot, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient, InputDocumentFileLocation

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "password")
TG_API_ID = int(os.getenv("TG_API_ID", 0))
TG_API_HASH = os.getenv("TG_API_HASH", "")
DB_PATH = "bundles.db"
PORT = int(os.getenv("PORT", 8080))
CHUNK_SIZE = 1900 * 1024 * 1024  # ~1.9GB

# ---------------- APP ----------------
app = FastAPI()
bot = Bot(BOT_TOKEN)
application = ApplicationBuilder().token(BOT_TOKEN).build()
tele_client = TelegramClient("session", TG_API_ID, TG_API_HASH)

# ---------------- DATABASE ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS bundles (
    id TEXT PRIMARY KEY,
    filename TEXT,
    message_ids TEXT,
    total_size INTEGER
);
"""
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    try: yield con
    finally: con.commit(); con.close()
with db() as c: c.execute(SCHEMA)

# ---------------- BOT ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a document or use /url <direct_link> to upload.\n"
        "Files >2GB will be split automatically."
    )

async def url_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /url <direct_link>")
    url = context.args[0]
    msg = await update.message.reply_text(f"⏳ Downloading URL...")
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return await msg.edit_text("❌ Failed to download URL.")
            total_size = int(resp.headers.get("Content-Length", 0))
            file_id = url.split("/")[-1].replace(" ", "_")
            message_ids = []
            downloaded = 0
            buffer = bytearray()
            chunk_index = 0
            
            async for chunk in resp.content.iter_chunked(10_485_760):
                buffer.extend(chunk)
                downloaded += len(chunk)
                if len(buffer) >= CHUNK_SIZE:
                    chunk_index += 1
                    f = InputFile(io.BytesIO(buffer), filename=f"{file_id}.part{chunk_index}")
                    sent = await bot.send_document(CHANNEL_ID, f)
                    message_ids.append(sent.message_id)
                    await msg.edit_text(f"Uploaded chunk {chunk_index}, {downloaded//(1024*1024)}MB/{total_size//(1024*1024)}MB")
                    buffer = bytearray()
            if buffer:
                chunk_index += 1
                f = InputFile(io.BytesIO(buffer), filename=f"{file_id}.part{chunk_index}")
                sent = await bot.send_document(CHANNEL_ID, f)
                message_ids.append(sent.message_id)
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO bundles (id, filename, message_ids, total_size) VALUES (?,?,?,?)",
                    (file_id, file_id, json.dumps(message_ids), total_size)
                )
            await msg.edit_text(f"✅ Upload complete! {chunk_index} chunks uploaded.")

async def forward_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc:
        msg_status = await update.message.reply_text("⏳ Forwarding document...")
        sent = await update.message.forward(chat_id=CHANNEL_ID)
        with db() as con:
            con.execute(
                "INSERT OR REPLACE INTO bundles (id, filename, message_ids, total_size) VALUES (?,?,?,?)",
                (str(doc.file_unique_id), doc.file_name, json.dumps([sent.message_id]), doc.file_size)
            )
        await msg_status.edit_text("✅ Document forwarded and indexed!")

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("url", url_upload))
application.add_handler(MessageHandler(filters.Document.ALL, forward_document))

# ---------------- WEBHOOK ----------------
@app.post(f"/webhook/{BOT_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot)
    await application.update_queue.put(update)
    return {"ok": True}

# ---------------- WEB INDEX ----------------
@app.get("/")
async def index(password: str = ""):
    if password != WEB_PASSWORD:
        return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
    html = "<h2>File Index</h2><ul>"
    with db() as con:
        for row in con.execute("SELECT id, filename FROM bundles"):
            file_id, fname = row[0], row[1]
            html += f'<li>{fname} - <a href="/download/{file_id}">Download</a></li>'
    html += "</ul>"
    return HTMLResponse(html)

# ---------------- STREAM DOWNLOAD ----------------
@app.get("/download/{bundle_id}")
async def download(bundle_id: str, range: str = Header(None)):
    with db() as con:
        row = con.execute("SELECT message_ids, filename FROM bundles WHERE id=?", (bundle_id,)).fetchone()
    if not row: raise HTTPException(404, "Bundle not found")
    msg_ids = json.loads(row[0])
    filename = row[1]

    async def stream_bytes():
        for mid in msg_ids:
            location = InputDocumentFileLocation(id=mid, access_hash=0, file_reference=b"", thumb_size="")
            data = await tele_client.download_file(location)
            yield data

    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return StreamingResponse(stream_bytes(), headers=headers)

# ---------------- STARTUP ----------------
@app.on_event("startup")
async def startup_event():
    await application.initialize()
    await application.start()
    await tele_client.start()
    print("✅ Bot webhook + Telethon client ready")

@app.on_event("shutdown")
async def shutdown_event():
    await application.stop()
    await application.shutdown()
    await tele_client.disconnect()
