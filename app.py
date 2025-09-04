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
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB limit for direct download

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
MAX_CHUNK_SIZE = 1.9 * 1024 * 1024 * 1024  # 1.9GB
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
🤖 **Welcome to File Storage Bot!**

I can help you store files and generate direct download links.

**Available Commands:**
/upload - Upload a file from a URL
/list - List all your stored files
/help - Show this help message

**How to use:**
1. Send me a file directly, or
2. Use /upload with a URL

I'll provide you with a direct download link.
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
        safe_send_message(message.chat.id, "Please send me the URL of the file you want to upload:")

@bot.message_handler(commands=['list'])
def handle_list_command(message):
    """List all stored files"""
    if not file_metadata:
        safe_send_message(message.chat.id, "You haven't uploaded any files yet.")
        return
    
    response = "📁 **Your Stored Files:**\n\n"
    for i, (file_id, metadata) in enumerate(list(file_metadata.items())[:10]):
        size_mb = metadata['size'] / (1024 * 1024)
        response += f"• {metadata['filename']} ({size_mb:.2f} MB)\n"
        response += f"  Download: {BASE_URL}/download/{file_id}\n\n"
    
    if len(file_metadata) > 10:
        response += f"... and {len(file_metadata) - 10} more files."
    
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
    safe_send_message(message.chat.id, f"Received URL: {url}\n\nThis feature would process the URL in a full implementation.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Handle document messages"""
    try:
        safe_send_message(message.chat.id, "📥 Downloading your file...")
        
        # Get the actual file from Telegram
        file_info = safe_get_file(message.document.file_id)
        file_name = message.document.file_name or f"file_{uuid.uuid4().hex[:8]}"
        
        # Download the file content
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
        response = requests.get(file_url)
        file_content = response.content
        
        # Save file locally
        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER)
        
        file_path = os.path.join(UPLOAD_FOLDER, file_name)
        with open(file_path, 'wb') as f:
            f.write(file_content)
        
        # Store metadata with actual file content
        file_id = str(uuid.uuid4())
        file_size = len(file_content)
        
        file_metadata[file_id] = {
            'filename': file_name,
            'size': file_size,
            'content': file_content,  # Store actual content
            'upload_time': time.time()
        }
        
        success_text = f"""
✅ **File uploaded successfully!**

📁 **File:** {file_name}
📊 **Size:** {file_size / (1024 * 1024):.2f} MB
🔗 **Download URL:** {BASE_URL}/download/{file_id}

You can use this URL to download your file anytime.
        """
        safe_send_message(message.chat.id, success_text, parse_mode='Markdown')
        
    except Exception as e:
        safe_send_message(message.chat.id, f"❌ Error processing file: {str(e)}")

def is_valid_url(url):
    """Check if a URL is valid"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

async def download_file_from_url(url, file_path):
    """Download a file from a URL"""
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    with open(file_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    return True
                else:
                    return False
    except Exception as e:
        logger.error(f"Error downloading file from URL: {e}")
        return False

@app.route('/')
def home():
    return {
        'status': 'online', 
        'service': 'Telegram File Storage Bot',
        'bot_initialized': bot is not None,
        'webhook_setup': setup_webhook(),
        'endpoints': {
            'upload': '/upload',
            'upload_url': '/upload/url',
            'download': '/download/<file_id>',
            'file_info': '/files/<file_id>/info',
            'list_files': '/files',
            'health': '/health',
            'webhook': f'/webhook/{TELEGRAM_BOT_TOKEN}'
        }
    }

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload a file to storage"""
    if bot is None:
        return {"error": "Bot is not initialized. Please check your TELEGRAM_BOT_TOKEN."}, 500
        
    if 'file' not in request.files:
        return {"error": "No file provided"}, 400
    
    file = request.files['file']
    if file.filename == '':
        return {"error": "No file selected"}, 400
    
    # Create upload directory if it doesn't exist
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
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

@app.route('/upload/url', methods=['POST'])
def upload_from_url():
    """Upload a file from a URL to storage"""
    if bot is None:
        return {"error": "Bot is not initialized"}, 500
        
    data = request.get_json()
    if not data or 'url' not in data:
        return {"error": "No URL provided"}, 400
    
    url = data['url']
    if not is_valid_url(url):
        return {"error": "Invalid URL provided"}, 400
    
    # Extract filename from URL or generate one
    filename = os.path.basename(urlparse(url).path)
    if not filename:
        filename = f"downloaded_file_{uuid.uuid4().hex[:8]}"
    
    # Create upload directory if it doesn't exist
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    # Download file from URL
    temp_path = os.path.join(UPLOAD_FOLDER, filename)
    
    try:
        # Download the file
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(download_file_from_url(url, temp_path))
        
        if not success:
            return {"error": "Failed to download file from URL"}, 500
        
        # Read the downloaded file
        with open(temp_path, 'rb') as f:
            file_content = f.read()
        
        file_id = str(uuid.uuid4())
        file_size = len(file_content)
        
        # Store metadata
        file_metadata[file_id] = {
            'filename': filename,
            'size': file_size,
            'content': file_content,
            'upload_time': time.time()
        }
        
        # Clean up
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        
        return {
            "file_id": file_id,
            "filename": filename,
            "size": file_size,
            "message": "File uploaded successfully from URL",
            "download_url": f"{BASE_URL}/download/{file_id}"
        }, 200
        
    except Exception as e:
        logger.error(f"Error uploading from URL: {e}")
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        return {"error": str(e)}, 500

@app.route('/download/<file_id>', methods=['GET'])
def download_file(file_id):
    """Download a file directly from memory"""
    if file_id not in file_metadata:
        abort(404, description="File not found")
    
    metadata = file_metadata[file_id]
    filename = metadata['filename']
    
    # Return the actual file content stored in memory
    return Response(
        metadata['content'],
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(metadata['size'])
        }
    )

@app.route('/files/<file_id>/info', methods=['GET'])
def get_file_info(file_id):
    """Get information about a stored file"""
    if file_id not in file_metadata:
        abort(404, description="File not found")
    
    metadata = file_metadata[file_id]
    return {
        "file_id": file_id,
        "filename": metadata['filename'],
        "size": metadata['size'],
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
        'service': 'Telegram File Storage Bot'
    }

@app.route('/debug/bot', methods=['GET'])
def debug_bot():
    """Debug endpoint for bot status"""
    try:
        if bot:
            me = safe_get_me()
            webhook_info = bot.get_webhook_info()
            return {
                'initialized': True,
                'bot_info': {
                    'id': me.id,
                    'username': me.username,
                    'first_name': me.first_name
                },
                'webhook_info': {
                    'url': webhook_info.url,
                    'has_custom_certificate': webhook_info.has_custom_certificate,
                    'pending_update_count': webhook_info.pending_update_count
                }
            }
        else:
            return {'initialized': False, 'error': 'Bot not initialized'}
    except Exception as e:
        return {'initialized': False, 'error': str(e)}

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return {'error': 'Not found', 'message': str(error)}, 404

@app.errorhandler(500)
def internal_error(error):
    return {'error': 'Internal server error', 'message': str(error)}, 500

if __name__ == '__main__':
    # Create upload directory if it doesn't exist
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    
    logger.info(f"Starting Telegram File Storage Bot on port {PORT}")
    logger.info(f"Upload folder: {UPLOAD_FOLDER}")
    logger.info(f"Base URL: {BASE_URL}")
    logger.info(f"Koyeb Service URL: {KOYEB_SERVICE_URL}")
    
    # Setup webhook instead of polling
    webhook_success = setup_webhook()
    
    if webhook_success:
        logger.info("Webhook setup completed successfully")
    else:
        logger.error("Webhook setup failed")
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
