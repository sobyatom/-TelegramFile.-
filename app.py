import os
import math
import uuid
import logging
import time
import sqlite3
import requests
from flask import Flask, request, Response, abort
from telebot import TeleBot, types, apihelper
from functools import wraps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ===========================
# Configuration
# ===========================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
DB_FILE = os.getenv('DB_FILE', 'files.db')
PORT = int(os.getenv('PORT', 5000))
BASE_URL = os.getenv('BASE_URL', f'http://localhost:{PORT}')
KOYEB_SERVICE_URL = os.getenv('KOYEB_SERVICE_URL', BASE_URL)
MAX_CHUNK_SIZE = int(1.9 * 1024 * 1024 * 1024)  # 1.9GB

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    exit(1)

# ===========================
# Database Setup
# ===========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            filename TEXT,
            size INTEGER,
            upload_time REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            file_id TEXT,
            telegram_file_id TEXT,
            part_number INTEGER,
            size INTEGER,
            FOREIGN KEY (file_id) REFERENCES files (file_id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===========================
# Network reliability setup
# ===========================
def setup_network_reliability():
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    apihelper.requests_session = session
    apihelper.READ_TIMEOUT = 30
    apihelper.CONNECT_TIMEOUT = 10
    logger.info("Network reliability configured")

setup_network_reliability()

# ===========================
# Bot Setup
# ===========================
try:
    bot = TeleBot(TELEGRAM_BOT_TOKEN)
    logger.info("Bot instance created")
except Exception as e:
    logger.error(f"Failed to init bot: {e}")
    bot = None

# ===========================
# Retry wrapper
# ===========================
def retry_telegram_api(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Retry {attempt+1}/{max_retries} after error: {e}")
                        time.sleep(delay)
                    else:
                        logger.error(f"All retries failed: {e}")
                        raise
        return wrapper
    return decorator

@retry_telegram_api()
def safe_send_document(chat_id, document, **kwargs):
    return bot.send_document(chat_id, document, **kwargs)

@retry_telegram_api()
def safe_send_message(chat_id, text, **kwargs):
    return bot.send_message(chat_id, text, **kwargs)

@retry_telegram_api()
def safe_get_file(file_id):
    return bot.get_file(file_id)

@retry_telegram_api()
def safe_get_me():
    return bot.get_me()

# ===========================
# Helpers
# ===========================
def split_file(file_path, chunk_size=MAX_CHUNK_SIZE):
    parts = []
    with open(file_path, "rb") as f:
        index = 1
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            part_path = f"{file_path}.part{index}"
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            parts.append((index, part_path, len(chunk)))
            index += 1
    return parts

def save_file_metadata(file_id, filename, size, upload_time):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
              (file_id, filename, size, upload_time))
    conn.commit()
    conn.close()

def save_chunk_metadata(chunk_id, file_id, telegram_file_id, part_number, size):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?)",
              (chunk_id, file_id, telegram_file_id, part_number, size))
    conn.commit()
    conn.close()

def get_file_metadata(file_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM files WHERE file_id=?", (file_id,))
    file_row = c.fetchone()
    if not file_row:
        return None
    c.execute("SELECT * FROM chunks WHERE file_id=? ORDER BY part_number", (file_id,))
    chunks = c.fetchall()
    conn.close()
    return {
        "file_id": file_row[0],
        "filename": file_row[1],
        "size": file_row[2],
        "upload_time": file_row[3],
        "chunks": [{
            "chunk_id": row[0],
            "telegram_file_id": row[2],
            "part_number": row[3],
            "size": row[4]
        } for row in chunks]
    }

# ===========================
# Bot Handlers
# ===========================
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    safe_send_message(message.chat.id, "🤖 Welcome! Send me a file and I'll split + store it!")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    try:
        file_name = message.document.file_name or f"file_{uuid.uuid4().hex[:8]}"
        file_size = message.document.file_size or 0
        file_id = str(uuid.uuid4())
        upload_time = time.time()

        # Save metadata
        save_file_metadata(file_id, file_name, file_size, upload_time)

        # Download from Telegram to local
        file_info = safe_get_file(message.document.file_id)
        dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
        local_path = os.path.join(UPLOAD_FOLDER, file_name)
        with requests.get(dl_url, stream=True) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

        # Split if needed
        if file_size > MAX_CHUNK_SIZE:
            parts = split_file(local_path)
            for part_number, part_path, size in parts:
                with open(part_path, "rb") as pf:
                    sent = safe_send_document(TELEGRAM_CHAT_ID, pf, visible_file_name=os.path.basename(part_path))
                    save_chunk_metadata(str(uuid.uuid4()), file_id, sent.document.file_id, part_number, size)
        else:
            with open(local_path, "rb") as f:
                sent = safe_send_document(TELEGRAM_CHAT_ID, f, visible_file_name=file_name)
                save_chunk_metadata(str(uuid.uuid4()), file_id, sent.document.file_id, 1, file_size)

        safe_send_message(message.chat.id, f"✅ File stored!\nDownload: {BASE_URL}/download/{file_id}")

    except Exception as e:
        logger.error(f"Error: {e}")
        safe_send_message(message.chat.id, f"❌ Failed: {str(e)}")

# ===========================
# Flask Routes
# ===========================
@app.route('/download/<file_id>', methods=['GET'])
def download_file(file_id):
    metadata = get_file_metadata(file_id)
    if not metadata:
        abort(404, "File not found")

    filename = metadata['filename']

    def generate():
        try:
            for chunk in metadata['chunks']:
                file_info = safe_get_file(chunk['telegram_file_id'])
                url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
                with requests.get(url, stream=True) as r:
                    r.raise_for_status()
                    for data in r.iter_content(8192):
                        yield data
        except Exception as e:
            yield f"Error: {e}".encode()

    return Response(
        generate(),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.route('/files', methods=['GET'])
def list_files():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM files")
    rows = c.fetchall()
    conn.close()
    return {"files": [
        {"file_id": r[0], "filename": r[1], "size": r[2], "upload_time": r[3],
         "download_url": f"{BASE_URL}/download/{r[0]}"}
        for r in rows
    ]}

@app.route('/health', methods=['GET'])
def health_check():
    return {"status": "healthy", "files": len(list_files()['files'])}

# ===========================
# Run
# ===========================
if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    logger.info(f"🚀 Starting on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
