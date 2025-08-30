import os
import re
import math
import json
import sqlite3
import tempfile
import requests
import asyncio
import threading
from contextlib import closing
from typing import List, Tuple

from flask import Flask, request, Response, abort, render_template_string, redirect, url_for

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ─────────────────────────── ENV ───────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
CHANNEL_ID    = int(os.getenv("CHANNEL_ID", "0"))   # private channel/group id (-100...)
INDEX_PASSWORD= os.getenv("WEB_PASSWORD", "changeme")
SECRET_KEY    = os.getenv("SECRET_KEY", "supersecret")
PORT          = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN or CHANNEL_ID == 0:
    raise SystemExit("Please set BOT_TOKEN and CHANNEL_ID environment variables.")

# ─────────────────────────── DB ────────────────────────────
# bundles: one logical file (may have many TG parts)
# chunks: each telegram document (part)
con = sqlite3.connect("data.db", check_same_thread=False)
cur = con.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS bundles (
  id TEXT PRIMARY KEY,              -- slug (usually base filename)
  filename TEXT NOT NULL,           -- display filename
  total_size INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS chunks (
  bundle_id TEXT NOT NULL,
  part_index INTEGER NOT NULL,
  file_id TEXT NOT NULL,
  size INTEGER NOT NULL,
  PRIMARY KEY(bundle_id, part_index),
  FOREIGN KEY(bundle_id) REFERENCES bundles(id) ON DELETE CASCADE
);
""")
con.commit()

def upsert_bundle(bundle_id: str, filename: str):
    cur.execute("INSERT OR IGNORE INTO bundles(id, filename, total_size) VALUES(?,?,0)",
                (bundle_id, filename))
    con.commit()

def add_chunk(bundle_id: str, part_index: int, file_id: str, size: int):
    cur.execute("INSERT OR REPLACE INTO chunks(bundle_id, part_index, file_id, size) VALUES(?,?,?,?)",
                (bundle_id, part_index, file_id, size))
    cur.execute("UPDATE bundles SET total_size=(SELECT COALESCE(SUM(size),0) FROM chunks WHERE bundle_id=?) WHERE id=?",
                (bundle_id, bundle_id))
    con.commit()

def list_bundles():
    return cur.execute("SELECT id, filename, total_size FROM bundles ORDER BY created_at DESC").fetchall()

def get_bundle(bundle_id: str):
    row = cur.execute("SELECT id, filename, total_size FROM bundles WHERE id=?", (bundle_id,)).fetchone()
    if not row: return None
    chunks = cur.execute("SELECT part_index, file_id, size FROM chunks WHERE bundle_id=? ORDER BY part_index ASC",
                         (bundle_id,)).fetchall()
    return {"id": row[0], "filename": row[1], "total": row[2], "chunks": chunks}

# ────────────────────── TELEGRAM BOT ───────────────────────
bot = Bot(BOT_TOKEN)

# Helpers
def base_and_part(name: str) -> Tuple[str, int]:
    """
    Parse names like movie.mkv.part1 or movie.part001.mkv -> ('movie.mkv', 1)
    If no .partN, returns (name, 1)
    """
    m = re.search(r"(.*?)(?:\.part|\s*part)(\d+)(\..+)?$", name, re.IGNORECASE)
    if m:
        pre, num, suf = m.groups()
        base = (pre + (suf or "")).strip()
        return base, int(num)
    return name, 1

async def send_part_from_path(path: str, filename: str):
    msg = await bot.send_document(chat_id=CHANNEL_ID, document=open(path, "rb"), filename=filename)
    # return file_id and size
    doc = msg.document
    return doc.file_id, doc.file_size

# Commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a document to forward it to the index.\n"
        "Or use /url <direct_link> to upload from the web.\n"
        "Files >2GB are auto-split; the web index merges them."
    )

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /url <direct_link>")
    url = context.args[0].strip()
    filename = url.split("/")[-1].split("?")[0] or "file.bin"

    # safer per-chunk size for Koyeb ephemeral disk (≈1.9 GiB)
    CHUNK_BYTES = 1900 * 1024 * 1024

    # probe size if possible
    try:
        r_head = requests.head(url, allow_redirects=True, timeout=20)
        size = int(r_head.headers.get("Content-Length", "0"))
    except Exception:
        size = 0

    total_gb = f"{size/1e9:.2f} GB" if size else "unknown size"
    await update.message.reply_text(f"Starting upload: {filename} ({total_gb})")

    # ensure bundle row
    bundle_id, _ = base_and_part(filename)
    upsert_bundle(bundle_id, bundle_id)

    # stream download -> roll to temp part files -> send -> delete
    part_idx = 1
    written = 0
    tmp = None
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1024 * 1024):  # 1 MiB
                if not chunk:
                    continue
                if tmp is None:
                    tmp = tempfile.NamedTemporaryFile(delete=False)
                    written = 0
                tmp.write(chunk)
                written += len(chunk)
                if written >= CHUNK_BYTES:
                    tmp.close()
                    part_name = f"{bundle_id}.part{part_idx}" if size == 0 or size > CHUNK_BYTES else bundle_id
                    file_id, fsize = await send_part_from_path(tmp.name, part_name)
                    add_chunk(bundle_id, part_idx, file_id, fsize)
                    os.remove(tmp.name)
                    tmp = None
                    part_idx += 1

            # flush tail
            if tmp is not None:
                tmp.close()
                part_name = f"{bundle_id}.part{part_idx}" if part_idx > 1 or (size and size > CHUNK_BYTES) else bundle_id
                file_id, fsize = await send_part_from_path(tmp.name, part_name)
                add_chunk(bundle_id, part_idx, file_id, fsize)
                os.remove(tmp.name)
                tmp = None

        await update.message.reply_text(f"✅ Uploaded to index: {bundle_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Upload failed: {e}")
        try:
            if tmp is not None:
                tmp.close()
                os.remove(tmp.name)
        except Exception:
            pass

async def on_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = update.message.document
    if not d:
        return
    # forward to the storage channel
    msg = await bot.forward_message(chat_id=CHANNEL_ID,
                                    from_chat_id=update.message.chat_id,
                                    message_id=update.message.message_id)
    # index it (single or partN)
    name = d.file_name or "file.bin"
    base, part = base_and_part(name)
    upsert_bundle(base, base)
    add_chunk(base, part, msg.document.file_id, msg.document.file_size)
    await update.message.reply_text(f"✅ Saved to index: {name}")

# ───────────────────────── WEB (Flask) ─────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Telegram Index</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; }
    h2 { margin: 0 0 16px; }
    table { border-collapse: collapse; width: 100%; max-width: 1000px; }
    th, td { padding: 10px 12px; border-bottom: 1px solid #eee; text-align: left; }
    small { color: #666; }
    input[type=password]{ padding:8px; }
    .login{ margin:80px auto; max-width:360px; border:1px solid #eee; padding:24px; border-radius:12px; }
    .btn{ padding:8px 12px; border:1px solid #ddd; border-radius:8px; background:#fafafa; cursor:pointer; }
    .btn:hover{ background:#f0f0f0; }
  </style>
</head>
<body>
  {% if not ok %}
  <div class="login">
    <h3>Enter password</h3>
    <form method="GET">
      <input type="password" name="password" placeholder="Password"/>
      <button class="btn" type="submit">Enter</button>
    </form>
  </div>
  {% else %}
  <h2>📂 Telegram File Index</h2>
  <table>
    <thead><tr><th>File</th><th>Size</th><th>Parts</th><th>Action</th></tr></thead>
    <tbody>
      {% for b in bundles %}
        <tr>
          <td>{{ b.filename }}</td>
          <td>{{ (b.total/1024/1024/1024)|round(2) }} GB</td>
          <td>{{ b.parts }}</td>
          <td><a class="btn" href="/download/{{ b.id }}?password={{password}}">Download</a></td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  <p><small>Range-enabled: resume supported.</small></p>
  {% endif %}
</body>
</html>
"""

@app.get("/")
def home():
    pwd = request.args.get("password", "")
    if pwd != INDEX_PASSWORD:
        return render_template_string(INDEX_HTML, ok=False)

    rows = list_bundles()
    bundles = []
    for bid, fname, total in rows:
        parts = cur.execute("SELECT COUNT(*) FROM chunks WHERE bundle_id=?", (bid,)).fetchone()[0]
        bundles.append({"id": bid, "filename": fname, "total": total, "parts": parts})
    return render_template_string(INDEX_HTML, ok=True, bundles=bundles, password=pwd)

def parse_range(range_header: str, total: int) -> Tuple[int, int]:
    # returns inclusive [start, end]
    if not range_header:
        return 0, total - 1
    try:
        units, spec = range_header.split("=")
        if units.strip() != "bytes":
            return 0, total - 1
        s, e = spec.split("-")
        start = int(s) if s else 0
        end = int(e) if e else total - 1
        start = max(0, start)
        end = min(total - 1, end)
        if start > end:
            start = 0; end = total - 1
        return start, end
    except Exception:
        return 0, total - 1

@app.get("/download/<bundle_id>")
def download(bundle_id: str):
    pwd = request.args.get("password", "")
    if pwd != INDEX_PASSWORD:
        return abort(401)

    bundle = get_bundle(bundle_id)
    if not bundle or not bundle["chunks"]:
        return abort(404)
    chunks = bundle["chunks"]  # list of (part_index, file_id, size)
    sizes = [c[2] for c in chunks]
    total = sum(sizes)

    # Resolve Telegram file URLs for each chunk
    async def resolve_urls():
        out = []
        for _, file_id, _ in chunks:
            f = await bot.get_file(file_id)
            # Construct absolute URL
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
            out.append(url)
        return out

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tg_urls: List[str] = loop.run_until_complete(resolve_urls())

    # Handle Range
    start, end = parse_range(request.headers.get("Range"), total)
    length = end - start + 1
    status = 206 if start > 0 or end < (total - 1) else 200
    headers = {
        "Content-Disposition": f'attachment; filename="{bundle["filename"]}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Type": "application/octet-stream",
    }

    # Map global range over parts and stream in order
    def generate():
        pos = 0  # global cursor
        for idx, url in enumerate(tg_urls):
            part_size = sizes[idx]
            part_start = pos
            part_end = pos + part_size - 1
            pos += part_size

            if part_end < start:
                continue
            if part_start > end:
                break

            fetch_from = max(0, start - part_start)
            fetch_to = min(part_size - 1, end - part_start)

            headers_r = {"Range": f"bytes={fetch_from}-{fetch_to}"}
            with requests.get(url, headers=headers_r, stream=True) as r:
                r.raise_for_status()
                for chunk in r.iter_content(1024 * 1024):  # 1 MiB
                    if chunk:
                        yield chunk

    return Response(generate(), status=status, headers=headers)

# ────────────────────── RUN BOTH (bot + web) ───────────────────
def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("url", cmd_url))
    application.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    # Flask dev server is fine for Koyeb single process
    from waitress import serve
    try:
        # Use waitress if available (added in requirements)
        serve(app, host="0.0.0.0", port=PORT)
    except Exception:
        app.run(host="0.0.0.0", port=PORT)
