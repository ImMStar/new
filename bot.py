import os
import re
import asyncio
import logging
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("terabox-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Endpoint of a Terabox link-resolver service you trust or host yourself.
# It must accept ?url=<terabox link> and return JSON containing a direct
# download link (see extract_direct_link() below to adapt to its format).
RESOLVER_API = os.environ.get("RESOLVER_API", "")
PORT = int(os.environ.get("PORT", "10000"))

# Telegram Bot API upload limit for bots using the cloud API
MAX_UPLOAD = 50 * 1024 * 1024

TERABOX_RE = re.compile(
    r"https?://(?:www\.)?(?:terabox|teraboxapp|1024terabox|4funbox|mirrobox|nephobox|"
    r"momerybox|tibibox|terasharelink|teraboxlink)\.[a-z]+/(?:s|wap/share/filelist)[^\s]*",
    re.I,
)


# ---------- tiny health server so Render sees an open port ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), Health).serve_forever()


# ---------- terabox resolving ----------
def extract_direct_link(data: dict):
    """Adapt this to whatever your resolver API returns."""
    for key in ("download_url", "direct_link", "dlink", "url"):
        if isinstance(data.get(key), str):
            return data[key], data.get("file_name") or data.get("filename") or "video.mp4"
    # some APIs return a list of files
    files = data.get("list") or data.get("files") or []
    if files and isinstance(files[0], dict):
        f = files[0]
        link = f.get("download_url") or f.get("dlink") or f.get("url")
        if link:
            return link, f.get("file_name") or f.get("filename") or "video.mp4"
    return None, None


async def resolve(session: aiohttp.ClientSession, link: str):
    if not RESOLVER_API:
        raise RuntimeError("RESOLVER_API is not configured")
    async with session.get(RESOLVER_API, params={"url": link}, timeout=60) as r:
        r.raise_for_status()
        data = await r.json(content_type=None)
    return extract_direct_link(data)


async def download(session, url, dest):
    size = 0
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=900)) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                size += len(chunk)
                f.write(chunk)
    return size


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a Terabox share link and I'll try to fetch the video."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = TERABOX_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Please send a valid Terabox share link.")
        return

    status = await update.message.reply_text("Resolving link...")
    try:
        async with aiohttp.ClientSession() as session:
            direct, name = await resolve(session, m.group(0))
            if not direct:
                await status.edit_text("Couldn't get a download link for this file.")
                return

            await status.edit_text("Downloading...")
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, re.sub(r"[^\w.\- ]", "_", name))
                size = await download(session, direct, path)

                if size > MAX_UPLOAD:
                    await status.edit_text(
                        f"File is {size/1048576:.0f} MB, over Telegram's 50 MB bot limit.\n"
                        f"Direct link:\n{direct}"
                    )
                    return

                await status.edit_text("Uploading...")
                with open(path, "rb") as f:
                    await update.message.reply_video(
                        video=f, caption=name, supports_streaming=True,
                        read_timeout=300, write_timeout=300,
                    )
                await status.delete()
    except Exception as e:
        log.exception("failed")
        await status.edit_text(f"Failed: {e}")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()


if __name__ == "__main__":
    main()
