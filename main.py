import os
import aiohttp
import shutil
import tempfile
from fastapi import FastAPI, Request, UploadFile, Form, Body
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = FastAPI(title="Telegram Filestream (Koyeb)")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Simple JSON index persisted to data/index.json
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(INDEX_PATH):
    with open(INDEX_PATH, "w") as f:
        f.write("{}")

import json
def load_index():
    with open(INDEX_PATH, "r") as f:
        return json.load(f)
def save_index(data):
    with open(INDEX_PATH + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(INDEX_PATH + ".tmp", INDEX_PATH)

# ---------------- Telegram helpers -----------------
async def get_file_path(session, file_id: str):
    async with session.get(f"{API_URL}/getFile", params={"file_id": file_id}) as resp:
        data = await resp.json()
        return data["result"]["file_path"]

async def send_document_bytes(session, data: bytes, name: str):
    form = aiohttp.FormData()
    form.add_field("chat_id", CHAT_ID)
    form.add_field("document", data, filename=name, content_type="application/octet-stream")
    async with session.post(f"{API_URL}/sendDocument", data=form) as resp:
        js = await resp.json()
        doc = js["result"].get("document") or js["result"]
        return doc["file_id"]

# ---------------- Routes -----------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    idx = load_index()
    return templates.TemplateResponse("index.html", {"request": request, "files": idx})

@app.post("/upload")
async def upload_file(file: UploadFile):
    # For production use CLI uploader for big files. This is basic browser upload.
    filename = file.filename
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE_BYTES", str(1024*1024*1024)))  # default 1GiB
    idx = load_index()
    idx.setdefault(filename, {"filename": filename, "chunks": [], "total_size": 0})
    tempdir = tempfile.mkdtemp()
    tmp_path = os.path.join(tempdir, filename)
    with open(tmp_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    total = 0
    async with aiohttp.ClientSession() as session:
        with open(tmp_path, "rb") as f:
            part = 0
            while True:
                data = f.read(CHUNK_SIZE)
                if not data:
                    break
                name = f"{filename}.part{part:06d}"
                file_id = await send_document_bytes(session, data, name)
                idx[filename]["chunks"].append({"file_id": file_id, "size": len(data)})
                total += len(data)
                part += 1
    idx[filename]["total_size"] = total
    save_index(idx)
    shutil.rmtree(tempdir)
    return RedirectResponse(url="/", status_code=303)

@app.post("/admin/register")
async def admin_register(meta: dict = Body(...)):
    # used by CLI uploader to register metadata
    if not {"filename", "total_size", "chunks"}.issubset(set(meta.keys())):
        return Response("Bad meta", status_code=400)
    idx = load_index()
    idx[meta["filename"]] = meta
    save_index(idx)
    return {"ok": True}

@app.get("/files")
async def list_files():
    return JSONResponse(load_index())

@app.get("/files/{filename}")
async def file_meta(filename: str):
    idx = load_index()
    meta = idx.get(filename)
    if not meta:
        return Response("Not found", status_code=404)
    return JSONResponse(meta)

@app.get("/stream/{filename}")
async def stream_file(filename: str, request: Request):
    idx = load_index()
    meta = idx.get(filename)
    if not meta:
        return Response("Not found", status_code=404)

    range_header = request.headers.get("range")
    if range_header:
        try:
            start_s, end_s = range_header.replace("bytes=", "").split("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else None
        except:
            start, end = 0, None
    else:
        start, end = 0, None

    async def generator():
        async with aiohttp.ClientSession() as session:
            offset = 0
            for ch in meta["chunks"]:
                file_id = ch["file_id"]
                file_path = await get_file_path(session, file_id)
                url = f"{FILE_API_URL}/{file_path}"
                async with session.get(url) as resp:
                    async for chunk in resp.content.iter_chunked(1024*1024):
                        clen = len(chunk)
                        if offset + clen < start:
                            offset += clen
                            continue
                        if end is not None and offset > end:
                            return
                        # if starting inside this chunk, slice appropriately
                        if start > offset:
                            chunk = chunk[start - offset:]
                        if end is not None and offset + clen > end:
                            chunk = chunk[:end - offset + 1]
                        yield chunk
                        offset += clen

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    if range_header:
        # Content-Length is best-effort
        # compute requested length (if end known)
        total_req = (end - start + 1) if end is not None else None
        if total_req is not None:
            headers["Content-Length"] = str(total_req)
        headers["Content-Range"] = f"bytes {start}-{end or ''}/{meta.get('total_size')}"
        return StreamingResponse(generator(), status_code=206, headers=headers)
    return StreamingResponse(generator(), headers=headers)
