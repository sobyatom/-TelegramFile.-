import os
import requests
from telebot import types
from app import bot, process_uploaded_file, file_metadata
from config import Config
import uuid

# Store user states for multi-step commands
user_states = {}

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Send welcome message and instructions"""
    welcome_text = """
🤖 **Welcome to File Storage Bot!**

I can help you store large files in Telegram and generate direct download links.

**Available Commands:**
/upload - Upload a file from a URL
/forward - Instructions for forwarding files
/list - List all your stored files
/help - Show this help message

**How to use:**
1. Send me a file directly, or
2. Use /upload with a URL, or
3. Forward a file from another chat

I'll split large files into chunks automatically and provide you with a direct download link.
    """
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['upload'])
def handle_upload_command(message):
    """Handle the upload command"""
    # Check if user provided a URL
    if len(message.text.split()) > 1:
        url = message.text.split()[1]
        handle_url_upload(message, url)
    else:
        # Ask for URL
        user_states[message.chat.id] = 'awaiting_url'
        bot.send_message(message.chat.id, "Please send me the URL of the file you want to upload:")

@bot.message_handler(commands=['forward'])
def handle_forward_command(message):
    """Instructions for forwarding files"""
    instructions = """
📤 **How to forward files:**

1. Find the file you want to store in any chat
2. Forward that file to this bot
3. I'll process it and give you a direct download link

You can forward files from:
- Your saved messages
- Any group or channel where you have access
- Any private chat
    """
    bot.reply_to(message, instructions, parse_mode='Markdown')

@bot.message_handler(commands=['list'])
def handle_list_command(message):
    """List all files uploaded by the user"""
    if not file_metadata:
        bot.send_message(message.chat.id, "You haven't uploaded any files yet.")
        return
    
    user_files = []
    for file_id, metadata in file_metadata.items():
        # In a real implementation, you would track which user uploaded which file
        user_files.append(metadata)
    
    response = "📁 **Your Stored Files:**\n\n"
    for file in user_files[:10]:  # Show first 10 files
        size_mb = file['size'] / (1024 * 1024)
        response += f"• {file['filename']} ({size_mb:.2f} MB)\n"
        response += f"  Download: /download_{file_id}\n\n"
    
    if len(user_files) > 10:
        response += f"... and {len(user_files) - 10} more files."
    
    bot.send_message(message.chat.id, response, parse_mode='Markdown')

@bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'awaiting_url')
def handle_url_response(message):
    """Handle URL response for upload"""
    url = message.text
    user_states[message.chat.id] = None  # Reset state
    
    # Validate URL
    from urllib.parse import urlparse
    if not all([urlparse(url).scheme, urlparse(url).netloc]):
        bot.send_message(message.chat.id, "That doesn't look like a valid URL. Please try again with a valid URL.")
        return
    
    handle_url_upload(message, url)

def handle_url_upload(message, url):
    """Process URL upload"""
    bot.send_message(message.chat.id, "📥 Downloading file from URL...")
    
    # In a real implementation, you would process this asynchronously
    # For simplicity, we'll just show a message
    bot.send_message(message.chat.id, f"I've received your URL: {url}\n\nThis feature would download and process the file in a full implementation.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    """Handle document messages (file uploads)"""
    try:
        # Get file information
        file_info = bot.get_file(message.document.file_id)
        file_name = message.document.file_name or f"file_{uuid.uuid4().hex}"
        
        # Download the file
        bot.send_message(message.chat.id, "📥 Downloading your file...")
        file_url = f"https://api.telegram.org/file/bot{Config.TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
        
        # Download file
        response = requests.get(file_url)
        if response.status_code != 200:
            bot.send_message(message.chat.id, "❌ Failed to download file. Please try again.")
            return
        
        # Save file temporarily
        temp_path = os.path.join(Config.UPLOAD_FOLDER, file_name)
        with open(temp_path, 'wb') as f:
            f.write(response.content)
        
        # Process the file
        bot.send_message(message.chat.id, "⚙️ Processing your file...")
        file_id, file_size, chunk_count = process_uploaded_file(temp_path, file_name)
        
        # Send success message with download link
        size_mb = file_size / (1024 * 1024)
        success_text = f"""
✅ **File successfully stored!**

📁 **File:** {file_name}
📊 **Size:** {size_mb:.2f} MB
🧩 **Chunks:** {chunk_count}

🔗 **Download URL:**
{Config.BASE_URL}/download/{file_id}

You can use this URL to download your file directly.
        """
        
        bot.send_message(message.chat.id, success_text, parse_mode='Markdown')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error processing file: {str(e)}")
        # Clean up
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)

@bot.message_handler(func=lambda message: message.text.startswith('/download_'))
def handle_download_command(message):
    """Handle download command"""
    try:
        file_id = message.text.replace('/download_', '')
        if file_id not in file_metadata:
            bot.send_message(message.chat.id, "❌ File not found. It may have been deleted or the ID is incorrect.")
            return
        
        metadata = file_metadata[file_id]
        download_url = f"{Config.BASE_URL}/download/{file_id}"
        
        response = f"""
🔗 **Download Link**

📁 **File:** {metadata['filename']}
🔗 **URL:** {download_url}

You can:
1. Click the URL to download directly
2. Use download managers for faster downloads
3. Share this link with others
        """
        
        bot.send_message(message.chat.id, response, parse_mode='Markdown')
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {str(e)}")
