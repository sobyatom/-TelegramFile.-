import os
import tempfile
import requests
from threading import Thread
from flask import Flask, request, render_template_string, send_file, abort
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme123")

bot = Bot(BOT_TOKEN)
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ================== BOT PART ==================
def start(update: Update, context: CallbackContext):
    update.message.reply_text("Send me a file or direct link, I will save it to index.")

def handle_file(update: Update, context: CallbackContext):
    file = update.message.document
    if file:
        bot.forward_message(chat_id=CHANNEL_ID, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
        update.message.reply_text(f"✅ Saved: {file.file_name}")

def handle_text(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    if url.startswith("http"):
        try:
            update.message.reply_text(f"⬇️ Downloading from {url} ...")
            r = requests.get(url, stream=True)
            filename = url.split("/")[-1] or "file.bin"
            tmp = tempfile.NamedTemporaryFile(delete=False)
            for chunk in r.iter_content(chunk_size=1024*1024):
                if chunk:
                    tmp.write(chunk)
            tmp.close()
            bot.send_document(chat_id=CHANNEL_ID, document=open(tmp.name, "rb"), filename=filename)
            update.message.reply_text(f"✅ Uploaded {filename} to index.")
        except Exception as e:
            update.message.reply_text(f"❌ Error: {e}")
    else:
        update.message.reply_text("Send a valid URL or file.")

def run_bot():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.document, handle_file))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    updater.start_polling()

# ================== WEB INDEX ==================
@app.route("/")
def index():
    password = request.args.get("password")
    if password != WEB_PASSWORD:
        return abort(401)

    # Fetch last 50 messages from channel
    updates = bot.get_chat(CHANNEL_ID)
    messages = bot.get_chat_history(CHANNEL_ID, limit=30) if hasattr(bot, "get_chat_history") else []
    
    file_list = []
    for msg in messages:
        if msg.document:
            file_list.append({
                "name": msg.document.file_name,
                "id": msg.document.file_id
            })

    template = """
    <h2>📂 Telegram File Index</h2>
    <ul>
    {% for f in files %}
      <li>{{ f.name }} - <a href="/download/{{ f.id }}?password={{password}}">Download</a></li>
    {% endfor %}
    </ul>
    """
    return render_template_string(template, files=file_list, password=password)

@app.route("/download/<file_id>")
def download(file_id):
    password = request.args.get("password")
    if password != WEB_PASSWORD:
        return abort(401)

    file = bot.get_file(file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False)
    file.download(custom_path=tmp.name)
    return send_file(tmp.name, as_attachment=True)

if __name__ == "__main__":
    Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
