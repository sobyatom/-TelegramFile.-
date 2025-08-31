import os
import tempfile
from fastapi import FastAPI, Request, Response, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pyrogram import Client
import aiohttp
from db import init_db, save_file, search_files

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

app = FastAPI()
bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

templates = Jinja2Templates(directory="templates")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

CHUNK_SIZE = 2 * 1024 * 1024 * 1024  # 2GB per Telegram file

# ------------------ File helpers ------------------

async def download_to_temp(url):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    tmp.close()
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)
    return tmp_path

def split_file(path):
    parts = []
    with open(path, "rb") as f:
        i = 0
        while chunk := f.read(CHUNK_SIZE):
            part_path = f"{path}.part{i}"
            with open(part_path, "wb") as p:
                p.write(chunk)
            parts.append(part_path)
            i += 1
    return parts

async def upload_chunks(parts):
    msg_ids = []
    for part in parts:
        msg = await bot.send_document(CHANNEL_ID, part)
        msg_ids.append(msg.message_id)
    return msg_ids

# ------------------ Routes ------------------

@app.on_event("startup")
async def startup():
    init_db()
    await bot.start()

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()

@app.get("/")
async def index(request: Request, q: str = ""):
    files = search_files(q)
    return templates.TemplateResponse("index.html", {"request": request, "files": files, "q": q})

@app.post("/upload")
async def upload_url(url: str = Form(...)):
    tmp_path = await download_to_temp(url)
    parts = split_file(tmp_path)
    msg_ids = await upload_chunks(parts)
    save_file(os.path.basename(url), msg_ids)
    # Cleanup temp files
    for p in parts:
        os.remove(p)
    os.remove(tmp_path)
    return {"status": "success", "file": os.path.basename(url)}

@app.get("/stream/{file_id}")
async def stream_file(file_id: int):
    files = search_files()
    file = next((f for f in files if f["id"] == file_id), None)
    if not file:
        return Response("File not found", status_code=404)
    async def streamer():
        for msg_id in file["msg_ids"]:
            msg = await bot.get_messages(CHANNEL_ID, msg_id)
            async for chunk in bot.iter_download(msg.document, chunk_size=1024*512):
                yield chunk
    return Response(streamer(), media_type="application/octet-stream")
