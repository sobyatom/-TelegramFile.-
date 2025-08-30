import os
import tempfile
import aiohttp
from fastapi import FastAPI, Request, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from telethon import TelegramClient
from telethon.sessions import StringSession

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")

app = FastAPI(title="Link→Link Telegram Storage")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

tele_client = TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH)

# -------------------------
# Helpers
# -------------------------
async def upload_file_to_channel(file_path: str, filename: str):
    if CHANNEL_ID == 0:
        raise RuntimeError("CHANNEL_ID not configured")
    msg = await tele_client.send_file(CHANNEL_ID, file_path, caption=filename, force_document=True)
    return msg

async def download_url_to_temp(url: str):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    tmp.close()
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=3600) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            with open(tmp_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)
    return tmp_path

async def list_channel_files(limit=50):
    out = []
    async for msg in tele_client.iter_messages(CHANNEL_ID, limit=limit):
        if msg.document:
            out.append({
                "name": getattr(msg.document, "file_name", f"message_{msg.id}"),
                "msg_id": msg.id,
                "link": f"/download/{msg.id}"
            })
    return out

# -------------------------
# Auth + Web UI
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
    return templates.TemplateResponse("index.html", {"request": request, "files": files})

# -------------------------
# Uploads
# -------------------------
@app.post("/upload_file")
async def upload_file(request: Request, file: UploadFile):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=403)
    tmp_path = tempfile.mktemp()
    with open(tmp_path, "wb") as f:
        f.write(await file.read())
    try:
        await upload_file_to_channel(tmp_path, file.filename)
    finally:
        os.remove(tmp_path)
    return RedirectResponse("/", status_code=303)

@app.post("/upload_url")
async def upload_url(request: Request, url: str = Form(...)):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=403)
    tmp_path = await download_url_to_temp(url)
    try:
        fname = os.path.basename(url)
        await upload_file_to_channel(tmp_path, fname)
    finally:
        os.remove(tmp_path)
    return RedirectResponse("/", status_code=303)

# -------------------------
# Download / Stream
# -------------------------
@app.get("/download/{msg_id}")
async def download(msg_id: int, request: Request):
    msg = await tele_client.get_messages(CHANNEL_ID, ids=msg_id)
    if not msg or not msg.document:
        raise HTTPException(status_code=404, detail="File not found")
    range_header = request.headers.get("range")
    start = 0
    end = msg.document.size - 1
    if range_header:
        bytes_range = range_header.replace("bytes=", "").split("-")
        if bytes_range[0]:
            start = int(bytes_range[0])
        if len(bytes_range) > 1 and bytes_range[1]:
            end = int(bytes_range[1])

    async def streamer():
        async for chunk in tele_client.iter_download(msg.document, chunk_size=1024*512, offset=start, limit=end-start+1):
            yield chunk

    headers = {"Content-Disposition": f'inline; filename="{getattr(msg.document, "file_name", "file")}"'}
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{msg.document.size}"
    return StreamingResponse(streamer(), headers=headers, status_code=206 if range_header else 200)

# -------------------------
# Startup
# -------------------------
@app.on_event("startup")
async def startup_event():
    print("🚀 Starting Telethon client...")
    await tele_client.start()
    print("✅ Telethon client started")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, workers=1)
