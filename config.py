import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
    # Use Koyeb's provided PORT environment variable
    BASE_URL = os.getenv('BASE_URL', f"http://{os.getenv('KOYEB_APP_NAME', 'localhost')}:{os.getenv('PORT', '8080')}")
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024 * 1024  # 100GB max file size
