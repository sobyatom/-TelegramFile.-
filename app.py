import os, sqlite3, aiohttp, asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pyrogram import Client, filters
from pyrogram.types import Message
import uvicorn

# ENV
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")

# DB
con = sqlite3.connect("files.db", check_same_thread=False)
cur = con.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, name TEXT, file_id TEXT, size INTEGER)")
con.commit()

# Bot
bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Web
app = FastAPI()

def check_auth(req: Request):
    if req.query_params.get("password") != WEB_PASSWORD:
        raise HTTPException(401, "Unauthorized")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    check_auth(request)
    rows = cur.execute("SELECT id,name,size FROM files").fetchall()
    out = "<h2>Telegram Index</h2><ul>"
    for fid, name, size in rows:
        out += f'<li>{name} ({size//1024//1024} MB) - <a href="/download/{fid}?password={WEB_PASSWORD}">Download</a></li>'
    out += "</ul>"
    return out

@app.get("/download/{fid}")
async def download(fid: int, request: Request):
    check_auth(request)
    row = cur.execute("SELECT name,file_id,size FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    name, file_id, size = row

    range_header = request.headers.get("range")
    start, end = 0, size - 1
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{name}"'
    }

    if range_header:
        units, _, range_spec = range_header.partition("=")
        if units == "bytes":
            start_str, _, end_str = range_spec.partition("-")
            if start_str: start = int(start_str)
            if end_str: end = int(end_str)
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    async def streamer():
        async for chunk in bot.stream_media(file_id, offset=start, limit=(end-start+1), block_size=1024*1024):
            yield chunk

    return StreamingResponse(streamer(),
                             status_code=status_code,
                             media_type="application/octet-stream",
                             headers=headers)

# Bot Handlers
@bot.on_message(filters.document | filters.video)
async def save_file(c: Client, m: Message):
    file = m.document or m.video
    fwd = await m.forward(CHANNEL_ID)
    cur.execute("INSERT INTO files (name,file_id,size) VALUES (?,?,?)",
                (file.file_name, fwd.id, file.file_size))
    con.commit()
    await m.reply_text("✅ File saved and indexed!")

@bot.on_message(filters.command("upload"))
async def from_url(c: Client, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: /upload <direct_link>")
    url = m.command[1]
    fname = url.split("/")[-1] or "file.bin"
    await m.reply_text(f"⬇️ Downloading {url} ...")
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r, open(fname, "wb") as f:
            while True:
                chunk = await r.content.read(1024*1024)
                if not chunk: break
                f.write(chunk)
    sent = await m.reply_document(fname)
    fwd = await sent.forward(CHANNEL_ID)
    cur.execute("INSERT INTO files (name,file_id,size) VALUES (?,?,?)",
                (fname, fwd.id, os.path.getsize(fname)))
    con.commit()
    os.remove(fname)
    await m.reply_text("✅ File uploaded and saved!")

# Start
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(bot.start())
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
