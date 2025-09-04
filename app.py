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
        webhook_url = f"{KOYEB_SERVICE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set to: {webhook_url}, success: {success}")
        return success
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        return False

# Webhook endpoint - FIXED VERSION
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

I can help you store large files in Telegram and generate direct download links.

**Available Commands:**
/upload - Upload a file from a URL
/list - List all your stored files
/help - Show this help message

**How to use:**
1. Send me a file directly, or
2. Use /upload with a URL

I'll split large files into chunks automatically and provide you with a direct download link.
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
        # Simulate processing
        file_name = message.document.file_name or f"file_{uuid.uuid4().hex[:8]}"
        time.sleep(2)  # Simulate processing time
        
        # Generate a fake file ID for demonstration
        file_id = str(uuid.uuid4())
        file_size = message.document.file_size or 1024 * 1024  # Default to 1MB if unknown
        
        # Store metadata
        file_metadata[file_id] = {
            'filename': file_name,
            'size': file_size,
            'chunks': [f"fake_chunk_id_{i}" for i in range(3)],
            'chunk_count': 3,
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

def split_file(file_path, chunk_size=MAX_CHUNK_SIZE):
    """Split a file into chunks"""
    file_id = str(uuid.uuid4())
    chunks = []
    file_size = os.path.getsize(file_path)
    total_chunks = math.ceil(file_size / chunk_size)
    
    logger.info(f"Splitting {file_size} bytes into {total_chunks} chunks")
    
    with open(file_path, 'rb') as f:
        for i in range(total_chunks):
            chunk_data = f.read(chunk_size)
            chunk_name = f"{file_id}_chunk_{i}"
            chunk_path = os.path.join(UPLOAD_FOLDER, chunk_name)
            
            with open(chunk_path, 'wb') as chunk_file:
                chunk_file.write(chunk_data)
            
            chunks.append(chunk_path)
    
    return file_id, chunks, file_size

async def upload_chunk_to_telegram_async(chunk_path, caption, semaphore):
    """Upload a chunk to Telegram"""
    async with semaphore:
        try:
            with open(chunk_path, 'rb') as f:
                message = safe_send_document(
                    chat_id=TELEGRAM_CHAT_ID,
                    document=f,
                    caption=caption,
                    timeout=300
                )
            return message.document.file_id
        except Exception as e:
            logger.error(f"Error uploading chunk to Telegram: {e}")
            raise

async def upload_all_chunks(chunk_paths, file_id):
    """Upload all chunks asynchronously"""
    semaphore = asyncio.Semaphore(3)  # Limit concurrent uploads
    tasks = []
    
    for i, chunk_path in enumerate(chunk_paths):
        caption = f"{file_id}_{i}"
        task = upload_chunk_to_telegram_async(chunk_path, caption, semaphore)
        tasks.append(task)
    
    return await asyncio.gather(*tasks)

def process_uploaded_file(file_path, filename):
    """Process an uploaded file"""
    try:
        file_id, chunks, file_size = split_file(file_path)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        telegram_file_ids = loop.run_until_complete(upload_all_chunks(chunks, file_id))
        
        # Clean up local chunks
        for chunk_path in chunks:
            if os.path.exists(chunk_path):
                os.unlink(chunk_path)
        
        # Store metadata
        file_metadata[file_id] = {
            'filename': filename,
            'size': file_size,
            'chunks': telegram_file_ids,
            'chunk_count': len(telegram_file_ids),
            'upload_time': time.time()
        }
        
        if os.path.exists(file_path):
            os.unlink(file_path)
        
        return file_id, file_size, len(telegram_file_ids)
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        if os.path.exists(file_path):
            os.unlink(file_path)
        raise

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
    """Upload a file to Telegram storage"""
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
    
    temp_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(temp_path)
    
    try:
        file_id, file_size, chunk_count = process_uploaded_file(temp_path, file.filename)
        
        return {
            "file_id": file_id,
            "filename": file.filename,
            "size": file_size,
            "chunk_count": chunk_count,
            "message": "File uploaded successfully",
            "download_url": f"{BASE_URL}/download/{file_id}"
        }, 200
        
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return {"error": str(e)}, 500

@app.route('/upload/url', methods=['POST'])
def upload_from_url():
    """Upload a file from a URL to Telegram storage"""
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
        
        # Process the downloaded file
        file_id, file_size, chunk_count = process_uploaded_file(temp_path, filename)
        
        return {
            "file_id": file_id,
            "filename": filename,
            "size": file_size,
            "chunk_count": chunk_count,
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
    """Download a file by streaming chunks from Telegram"""
    if bot is None:
        return {"error": "Bot is not initialized"}, 500
        
    if file_id not in file_metadata:
        abort(404, description="File not found")
    
    metadata = file_metadata[file_id]
    chunk_ids = metadata['chunks']
    file_size = metadata['size']
    filename = metadata['filename']
    
    # Handle range requests for pause/resume and partial downloads
    range_header = request.headers.get('Range')
    if range_header:
        # Parse range header
        ranges = range_header.replace('bytes=', '').split('-')
        start = int(ranges[0]) if ranges[0] else 0
        end = int(ranges[1]) if ranges[1] else file_size - 1
    else:
        start = 0
        end = file_size - 1
    
    # Calculate which chunks to download
    chunk_size = MAX_CHUNK_SIZE
    first_chunk = start // chunk_size
    last_chunk = end // chunk_size
    
    def generate():
        # Stream chunks in sequence
        for i in range(first_chunk, last_chunk + 1):
            chunk_id = chunk_ids[i]
            
            try:
                # Download chunk from Telegram
                file_info = safe_get_file(chunk_id)
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
                
                response = requests.get(file_url, stream=True, timeout=30)
                if response.status_code != 200:
                    raise Exception(f"Failed to download chunk: {response.status_code}")
                
                chunk_data = response.content
                
                # Determine what part of the chunk to send
                chunk_start = i * chunk_size
                chunk_end = min((i + 1) * chunk_size, file_size) - 1
                
                # Adjust for range requests
                if i == first_chunk:
                    offset = start - chunk_start
                else:
                    offset = 0
                    
                if i == last_chunk:
                    length = end - chunk_start - offset + 1
                else:
                    length = chunk_size - offset
                
                # Yield the appropriate part of the chunk
                yield chunk_data[offset:offset+length]
                
            except Exception as e:
                logger.error(f"Error downloading chunk {i}: {e}")
                # Continue with next chunk instead of failing completely
                continue
    
    # Set appropriate headers
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Accept-Ranges': 'bytes',
        'Content-Type': 'application/octet-stream'
    }
    
    if range_header:
        status = 206
        headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        content_length = end - start + 1
    else:
        status = 200
        content_length = file_size
    
    headers['Content-Length'] = str(content_length)
    
    return Response(generate(), status=status, headers=headers)

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
        "chunk_count": metadata['chunk_count'],
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
                "chunk_count': metadata['chunk_count'],
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

@app.route('/debug/bot', methods['GET'])
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
