import os, math, aiohttp
from pyrogram import Client, filters
from db import add_file

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SPLIT_SIZE = int(os.getenv("SPLIT_SIZE", 2000)) * 1024 * 1024

bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def split_and_upload(path, message):
    size = os.path.getsize(path)
    parts = math.ceil(size / SPLIT_SIZE)
    with open(path, "rb") as f:
        for i in range(parts):
            chunk = f.read(SPLIT_SIZE)
            part_name = f"{path}.part{i+1}" if parts > 1 else path
            with open(part_name, "wb") as pf:
                pf.write(chunk)
            msg = await bot.send_document(CHANNEL_ID, part_name)
            add_file(msg.id, part_name, len(chunk))
            await message.reply_text(f"Uploaded: https://{os.getenv('APP_URL')}/stream/{msg.id}")
            os.remove(part_name)

@bot.on_message(filters.command("upload") & filters.private)
async def handle_upload(_, message):
    if len(message.command) < 2:
        return await message.reply("Send: /upload <direct_url>")
    url = message.command[1]
    file_name = url.split("/")[-1]
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            with open(file_name, "wb") as f:
                f.write(await resp.read())
    await split_and_upload(file_name, message)
    os.remove(file_name)

bot.run()
