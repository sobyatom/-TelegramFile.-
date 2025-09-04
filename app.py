import os
import math
import uuid
import logging
import asyncio
import aiohttp
import requests
from flask import Flask, request, Response, abort, jsonify
from telebot import TeleBot, types
from io import BytesIO
from urllib.parse import urlparse
from config import Config

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

# Initialize bot
bot = TeleBot(app.config['TELEGRAM_BOT_TOKEN'])

# In-memory storage for file metadata (in production, use a database)
file_metadata = {}

# Maximum chunk size for Telegram (slightly under 2GB to be safe)
MAX_CHUNK_SIZE = 1.9 * 1024 * 1024 * 1024  # 1.9GB

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
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    with open(file_path, 'wb') as f:
                        while True:
                            chunk = await response.content.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                    return True
                else:
                    return False
    except Exception as e:
        logger.error(f"Error downloading file from URL: {e}")
        return False

def split_file(file_path, chunk_size=MAX_CHUNK_SIZE):
    """Split a file into chunks optimized for Telegram"""
    file_id = str(uuid.uuid4())
    chunks = []
    file_size = os.path.getsize(file_path)
    total_chunks = math.ceil(file_size / chunk_size)
    
    logger.info(f"Splitting {file_size} bytes into {total_chunks} chunks")
    
    with open(file_path, 'rb') as f:
        for i in range(total_chunks):
            # Read chunk
            chunk_data = f.read(chunk_size)
            chunk_name = f"{file_id}_chunk_{i}"
            chunk_path = os.path.join(app.config['UPLOAD_FOLDER'], chunk_name)
            
            # Save chunk locally
            with open(chunk_path, 'wb') as chunk_file:
                chunk_file.write(chunk_data)
            
            chunks.append(chunk_path)
    
    return file_id, chunks, file_size

async def upload_chunk_to_telegram_async(chunk_path, caption, semaphore):
    """Upload a chunk to Telegram asynchronously with semaphore for rate limiting"""
    async with semaphore:
        try:
            with open(chunk_path, 'rb') as f:
                message = bot.send_document(
                    chat_id=app.config['TELEGRAM_CHAT_ID'],
                    document=f,
                    caption=caption,
                    timeout=300  # 5 minute timeout
                )
            return message.document.file_id
        except Exception as e:
            logger.error(f"Error uploading chunk to Telegram: {e}")
            raise

async def upload_all_chunks(chunk_paths, file_id):
    """Upload all chunks asynchronously with rate limiting"""
    # Limit concurrent uploads to avoid hitting Telegram rate limits
    semaphore = asyncio.Semaphore(3)  # 3 concurrent uploads
    
    tasks = []
    for i, chunk_path in enumerate(chunk_paths):
        caption = f"{file_id}_{i}"
        task = upload_chunk_to_telegram_async(chunk_path, caption, semaphore)
        tasks.append(task)
    
    return await asyncio.gather(*tasks)

def process_uploaded_file(file_path, filename):
    """Process an uploaded file (split and upload to Telegram)"""
    try:
        # Split file into chunks
        file_id, chunks, file_size = split_file(file_path)
        
        # Upload each chunk to Telegram asynchronously
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
            'chunk_size': MAX_CHUNK_SIZE
        }
        
        # Clean up original file
        if os.path.exists(file_path):
            os.unlink(file_path)
        
        return file_id, file_size, len(telegram_file_ids)
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        # Clean up any remaining files
        if os.path.exists(file_path):
            os.unlink(file_path)
        raise

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload a file to Telegram storage with automatic splitting"""
    if 'file' not in request.files:
        return {"error": "No file provided"}, 400
    
    file = request.files['file']
    if file.filename == '':
        return {"error": "No file selected"}, 400
    
    # Save file temporarily
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(temp_path)
    
    try:
        file_id, file_size, chunk_count = process_uploaded_file(temp_path, file.filename)
        
        return {
            "file_id": file_id,
            "filename": file.filename,
            "size": file_size,
            "chunk_count": chunk_count,
            "message": "File uploaded successfully with automatic chunking",
            "download_url": f"{request.host_url}download/{file_id}"
        }, 200
        
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return {"error": str(e)}, 500

@app.route('/upload/url', methods=['POST'])
def upload_from_url():
    """Upload a file from a URL to Telegram storage"""
    data = request.get_json()
    if not data or 'url' not in data:
        return {"error": "No URL provided"}, 400
    
    url = data['url']
    if not is_valid_url(url):
        return {"error": "Invalid URL provided"}, 400
    
    # Extract filename from URL or generate one
    filename = os.path.basename(urlparse(url).path)
    if not filename:
        filename = f"downloaded_file_{uuid.uuid4().hex}"
    
    # Download file from URL
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
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
            "download_url": f"{request.host_url}download/{file_id}"
        }, 200
        
    except Exception as e:
        logger.error(f"Error uploading from URL: {e}")
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        return {"error": str(e)}, 500

@app.route('/download/<file_id>', methods=['GET'])
def download_file(file_id):
    """Download a file by streaming chunks from Telegram"""
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
            
            # Download chunk from Telegram
            file_info = bot.get_file(chunk_id)
            file_url = f"https://api.telegram.org/file/bot{app.config['TELEGRAM_BOT_TOKEN']}/{file_info.file_path}"
            
            response = requests.get(file_url, stream=True)
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
    
    # Set appropriate headers
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Accept-Ranges': 'bytes'
    }
    
    if range_header:
        status = 206
        headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        content_length = end - start + 1
    else:
        status = 200
        content_length = file_size
    
    headers['Content-Length'] = str(content_length)
    
    return Response(generate(), status=status, headers=headers, mimetype='application/octet-stream')

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
        "download_url": f"{request.host_url}download/{file_id}"
    }

@app.route('/files', methods=['GET'])
def list_files():
    """List all stored files"""
    return {
        "files": [
            {
                "file_id": file_id,
                "filename": metadata['filename'],
                "size": metadata['size'],
                "chunk_count": metadata['chunk_count'],
                "download_url": f"{request.host_url}download/{file_id}"
            }
            for file_id, metadata in file_metadata.items()
        ]
    }

if __name__ == '__main__':
    # Create upload directory if it doesn't exist
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    
    # Start the bot in a separate thread
    from threading import Thread
    Thread(target=bot.polling, kwargs={"none_stop": True}).start()
    
    app.run(host='0.0.0.0', port=5000, debug=True)
