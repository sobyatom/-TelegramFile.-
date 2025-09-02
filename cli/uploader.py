# CLI uploader that uploads parts and registers metadata with server
import os, sys, argparse, aiohttp, asyncio

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

async def send_part(session, data, filename):
    form = aiohttp.FormData()
    form.add_field("chat_id", CHAT_ID)
    form.add_field("document", data, filename=filename, content_type="application/octet-stream")
    async with session.post(f"{BOT_API}/sendDocument", data=form) as r:
        js = await r.json()
        doc = js["result"].get("document") or js["result"]
        return doc["file_id"], len(data)

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--chunk-size", type=int, default=1024*1024*1024)
    p.add_argument("--index-url", default="http://127.0.0.1:8080")
    args = p.parse_args()

    size = os.path.getsize(args.file)
    file_ids = []
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(args.file, "rb") as f:
            idx = 0
            while True:
                buf = f.read(args.chunk_size)
                if not buf:
                    break
                part_name = f"{os.path.basename(args.name)}.part{idx:06d}"
                fid, sent = await send_part(session, buf, part_name)
                file_ids.append({"file_id": fid, "size": sent})
                idx += 1
                print(f"uploaded part {idx}, {sent} bytes")
    meta = {"filename": args.name, "total_size": size, "chunk_size": args.chunk_size, "chunks": file_ids}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{args.index_url}/admin/register", json=meta) as r:
            print("registered:", r.status)
            print(await r.text())

if __name__ == "__main__":
    asyncio.run(main())
