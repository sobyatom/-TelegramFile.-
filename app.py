import os
from fastapi import FastAPI, Request, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pyrogram import Client
from db import init_db, search_files

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

app = FastAPI()
bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

templates = Jinja2Templates(directory="templates")
import os
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
@app.on_event("startup")
async def startup():
    init_db()
    await bot.start()

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()

@app.get("/")
async def index(request: Request, q: str = ""):
    results = search_files(q)
    return templates.TemplateResponse("index.html", {"request": request, "files": results, "q": q})

@app.get("/stream/{file_id}")
async def stream_file(file_id: int):
    file = await bot.get_messages(CHANNEL_ID, file_id)
    file_path = await file.download()
    def iterfile():
        with open(file_path, "rb") as f:
            yield from f
    return Response(iterfile(), media_type="application/octet-stream")
