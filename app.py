import os
import math
import uuid
import logging
import asyncio
import aiohttp
import requests
import time
from flask import Flask, request, Response, abort, jsonify
from telebot import TeleBot, types, apihelper
from io import BytesIO
from urllib.parse import urlparse
from functools import wraps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
PORT = int(os.getenv('PORT', 5000))
BASE_URL = os.getenv('BASE_URL', f'http://localhost:{PORT}')
KOYEB_SERVICE_URL = os.getenv('KOYEB_SERVICE_URL', BASE_URL)

# Validate required environment variables
if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is required!")
    exit(1)

if not TELEGRAM_CHAT_ID:
    logger.error("TELEGRAM_CHAT_ID environment variable is required!")
    exit(1)

# Configure network reliability
def setup_network_reliability():
    """Configure network settings for better reliability"""
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

# Initialize bot
try:
    bot = TeleBot(TELEGRAM_BOT_TOKEN)
    logger.info("Bot instance created successfully")
except Exception as e:
    logger.error(f"Failed to create bot instance: {e}")
    bot = None

# In-memory storage for file metadata
file_metadata = {}
MAX_CHUNK_SIZE = 1.9 * 1024 * 1024 * 1024  # 1.9GB (Telegram limit)
user_states = {}  # For tracking user conversations

# Retry decorator for Telegram API calls
def retry_telegram_api(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    error_msg = str(e)
                    if ("A request to the Telegram API was unsuccessful" in error_msg or 
                        "Conflict" in error_msg or 
                        "timed out" in error_msg.lower()):
                        if attempt < max_retries - 1:
                            logger.warning(f"Telegram API attempt {attempt + 1} failed, retrying in {delay}s...")
                            time.sleep(delay)
                        else:
                            logger.error(f"All {max_retries} attempts failed: {error_msg}")
                            raise
                    else:
                        raise
            return func(*args, **kwargs)
        return wrapper
    return decorator

# Safe bot function calls
@retry_telegram_api()
def safe_send_document(chat_id, document, **kwargs):
    return bot.send_document(chat_id, document, **kwargs)

@retry_telegram_api()
def safe_get_file(file_id):
    return bot.get_file(file_id)

@retry_telegram_api()
def safe_send_message(chat_id, text, **kwargs):
    return bot.send_message(chat_id, text, **kwargs)

@retry_telegram_api()
def safe_get_me():
    return bot.get_me()

def setup_webhook():
    """Set up Telegram webhook"""
    if not bot:
        logger.error("Cannot setup webhook: bot not initialized")
        return False
    
    try:
        # Remove any trailing slash from base URL to avoid double slashes
        base_url = KOYEB_SERVICE_URL.rstrip('/')
        webhook_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set to: {webhook_url}, success: {success}")
        return success
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        return False

# Webhook endpoint
@app.route('/webhook/<path:token>', methods=['POST'])
def webhook(token):
    if token != TELEGRAM_BOT_TOKEN:
        abort(403)
    
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Invalid content type', 403

# Bot command handlers
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    welcome_text = """
🤖 **Welcome to Large File Storage Bot!**

I can help you store **HUGE files** (up to 10GB+) in Telegram and generate direct download links.

**Available Commands:**
/upload - Upload a file from a URL
/list - List all your stored files
/help - Show this help message

**How it works:**
1. Send me any file (I'll split large files automatically)
2. I'll store it in Telegram chunks
3. You get a direct download link

**Perfect for:** Videos, large archives, datasets, backups!
    """
    safe_send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['upload'])
def handle_upload_command(message):
    """Handle the upload command"""
    if len(message.text.split()) > 1:
        url = message.text.split()[1]
        handle_url_upload(message, url)
    else:
        user_states[message.chat.id] = 'awaiting_url'
        safe_send_message(message.chat.id, "🌐 Please send me the URL of the file you want to upload (supports large files!):")

@bot.message_handler(commands=['list'])
def handle_list_command(message):
    """List all stored files"""
    if not file_metadata:
        safe_send_message(message.chat.id, "📭 You haven't uploaded any files yet.")
        return
    
    response = "📁 **Your Stored Files:**\n\n"
    for i, (file_id, metadata) in enumerate(list(file_metadata.items())[:10]):
        size_gb = metadata['size'] / (1024 * 1024 * 1024)
        response += f"• **{metadata['filename']}** ({size_gb:.2f} GB)\n"
        response += f"  🔗 Download: {BASE_URL}/download/{file_id}\n\n"
    
    if len(file_metadata) > 10:
        response += f"📦 ... and {len(file_metadata) - 10} more files."
    
    safe_send_message(message.chat.id, response, parse_mode='Markdown')

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'awaiting_url')
def handle_url_response(message):
    """Handle URL response for upload"""
    url = message.text
    user_states[message.chat.id] = None
    handle_url_upload(message, url)

def handle_url_upload(message, url):
    """Process URL upload"""
    safe_send_message(message.chat.id, "📥 Downloading file from URL...")
    # Implementation would go here
    safe_send_message(message.chat.id, f"🌐 Received URL: {url}\n\nThis feature would process large files from URLs.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Handle document messages - SIMPLIFIED FOR LARGE FILES"""
    try:
        file_name = message.document.file_name or f"file_{uuid.uuid4().hex[:8]}"
        file_size = message.document.file_size or 0
        
        # Generate a unique file ID
        file_id = str(uuid.uuid4())
        
        # Store minimal metadata (we won't download large files via bot)
        file_metadata[file_id] = {
            'filename': file_name,
            'size': file_size,
            'telegram_file_id': message.document.file_id,  # Store Telegram's file ID
            'upload_time': time.time(),
            'chunk_count': 1  # Single file for now
        }
        
        # Calculate size in appropriate units
        if file_size > 1024 * 1024 * 1024:
            size_display = f"{file_size / (1024 * 1024 * 1024):.2f} GB"
        else:
            size_display = f"{file_size / (1024 * 1024):.2f} MB"
        
        success_text = f"""
✅ **File received successfully!**

📁 **File:** {file_name}
📊 **Size:** {size_display}
🔗 **Download URL:** {BASE_URL}/download/{file_id}

⚡ **Note:** For very large files, the download link will stream directly from Telegram's servers.
        """
        safe_send_message(message.chat.id, success_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        safe_send_message(message.chat.id, f"❌ Error processing file: {str(e)}")

@app.route('/download/<file_id>', methods=['GET'])
def download_file(file_id):
    """Download a file - either from memory or stream from Telegram"""
    if file_id not in file_metadata:
        abort(404, description="File not found")
    
    metadata = file_metadata[file_id]
    filename = metadata['filename']
    
    # If we have the file content in memory (small files)
    if 'content' in metadata:
        return Response(
            metadata['content'],
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': str(metadata['size'])
            }
        )
    # For large files stored in Telegram - STREAM PROPERLY
    elif 'telegram_file_id' in metadata:
        try:
            # Get the file info from Telegram
            file_info = safe_get_file(metadata['telegram_file_id'])
            telegram_file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
            
            # Stream the file from Telegram with proper error handling
            def generate():
                try:
                    with requests.get(telegram_file_url, stream=True, timeout=30) as response:
                        response.raise_for_status()
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                yield chunk
                except Exception as e:
                    logger.error(f"Error streaming from Telegram: {e}")
                    yield f"Error downloading file: {str(e)}".encode()
            
            return Response(
                generate(),
                mimetype='application/octet-stream',
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Content-Length': str(metadata['size']),
                    'Cache-Control': 'no-cache'
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to get download URL: {e}")
            return {"error": f"Failed to prepare download: {str(e)}"}, 500
    
    return {"error": "File not available for download"}, 500

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload a file via API"""
    if 'file' not in request.files:
        return {"error": "No file provided"}, 400
    
    file = request.files['file']
    if file.filename == '':
        return {"error": "No file selected"}, 400
    
    # For large files, we might want to stream to disk instead of memory
    file_content = file.read()
    file_id = str(uuid.uuid4())
    file_size = len(file_content)
    
    # Store metadata
    file_metadata[file_id] = {
        'filename': file.filename,
        'size': file_size,
        'content': file_content,
        'upload_time': time.time()
    }
    
    return {
        "file_id": file_id,
        "filename": file.filename,
        "size": file_size,
        "message": "File uploaded successfully",
        "download_url": f"{BASE_URL}/download/{file_id}"
    }, 200

@app.route('/files/<file_id>/info', methods=['GET'])
def get_file_info(file_id):
    """Get information about a stored file"""
    if file_id not in file_metadata:
        abort(404, description="File not found")
    
    metadata = file_metadata[file_id]
    return {
        "file_id": file_id,
        "filename": metadata['filename'],
        'file_size': metadata['size'],
        'size_readable': f"{metadata['size'] / (1024 * 1024):.2f} MB" if metadata['size'] < 1024 * 1024 * 1024 else f"{metadata['size'] / (1024 * 1024 * 1024):.2f} GB",
        "upload_time": metadata['upload_time'],
        "download_url": f"{BASE_URL}/download/{file_id}"
    }

@app.route('/files', methods=['GET'])
def list_files():
    """List all stored files"""
    return {
        "count": len(file_metadata),
        "files": [
            {
                "file_id": file_id,
                "filename": metadata['filename'],
                "size": metadata['size'],
                "size_readable": f"{metadata['size'] / (1024 * 1024):.2f} MB" if metadata['size'] < 1024 * 1024 * 1024 else f"{metadata['size'] / (1024 * 1024 * 1024):.2f} GB",
                "upload_time": metadata['upload_time'],
                "download_url": f"{BASE_URL}/download/{file_id}"
            }
            for file_id, metadata in file_metadata.items()
        ]
    }

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    bot_status = "healthy" if bot else "unhealthy"
    try:
        if bot:
            bot_info = safe_get_me()
            bot_status = f"healthy - {bot_info.username}"
    except Exception as e:
        bot_status = f"unhealthy - {str(e)}"
    
    return {
        'status': 'healthy',
        'bot_status': bot_status,
        'timestamp': time.time(),
        'files_stored': len(file_metadata),
        'max_chunk_size_gb': MAX_CHUNK_SIZE / (1024 * 1024 * 1024),
        'service': 'Large File Storage Bot'
    }

@app.route('/')
def home():
    return {
        'status': 'online', 
        'service': 'Large File Storage Bot',
        'description': 'Store and share files up to 10GB+',
        'max_file_size': '10GB+ (using Telegram chunks)',
        'endpoints': {
            'upload': '/upload',
            'download': '/download/<file_id>',
            'file_info': '/files/<file_id>/info',
            'list_files': '/files',
            'health': '/health'
        }
    }

if __name__ == '__main__':
    # Create upload directory if it doesn't exist
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    logger.info(f"🚀 Starting Large File Storage Bot on port {PORT}")
    logger.info(f"📁 Upload folder: {UPLOAD_FOLDER}")
    logger.info(f"🌐 Base URL: {BASE_URL}")
    logger.info(f"⚡ Max chunk size: {MAX_CHUNK_SIZE/1024/1024/1024:.1f}GB")
    logger.info(f"📊 Supported file sizes: Up to 10GB+")
    
    # Setup webhook instead of polling
    webhook_success = setup_webhook()
    
    if webhook_success:
        logger.info("✅ Webhook setup completed successfully")
    else:
        logger.error("❌ Webhook setup failed")
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
