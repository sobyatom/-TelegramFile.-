import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
    BASE_URL = os.getenv('BASE_URL', 'http://localhost:5000')
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024 * 1024  # 100GB max file size
