"""
Image → JPG Telegram Bot with Turso (Queue System + Auto-Retry + Resume)
========================================================================
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import gc
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import libsql_client
import pillow_heif
import telegram
from flask import Flask
from PIL import Image, UnidentifiedImageError
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ===========================================================================
# FLASK APP
# ===========================================================================
flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    return "Bot is running", 200


def run_flask() -> None:
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


# ===========================================================================
# CONFIGURATION
# ===========================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "REPLACE_WITH_NEW_BOT_TOKEN")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

OWNER_ID = 2075368011
FREE_DAILY_LIMIT = 5
PAID_CREDIT_PACK = 20
PAID_CREDIT_PRICE_STARS = 50
UNLIMITED_DAYS = 30
UNLIMITED_PRICE_STARS = 150

TIMEZONE = ZoneInfo("Asia/Dhaka")

DOWNLOAD_DIR = Path("downloads")
OUTPUT_DIR = Path("converted")

MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
JPEG_QUALITY = 95
LOG_LEVEL = logging.INFO

SUPPORTED_EXTENSIONS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".jpe",
    ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

MAX_DIMENSION = 4096
USERS_PER_PAGE = 8
CONVS_PER_PAGE = 8

# 🚀 Concurrency & Queue Settings
MAX_WORKERS = 5
MAX_RETRIES = 3
conversion_queue = asyncio.Queue()

# ===========================================================================
# INITIALIZATION
# ===========================================================================
pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)


# ===========================================================================
# TURSO ASYNC CLIENT
# ===========================================================================
_db_client: libsql_client.Client | None = None


def _normalize_turso_url(raw_url: str) -> str:
    url = raw_url.strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    return url.rstrip("/")


def get_client() -> libsql_client.Client:
    global _db_client
    if _db_client is None:
        if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
            raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set.")
        http_url = _normalize_turso_url(TURSO_DATABASE_URL)
        _db_client = libsql_client.create_client(url=http_url, auth_token=TURSO_AUTH_TOKEN)
        logger.info("Turso async client ready: %s", http_url)
    return _db_client


async def db_execute(sql: str, params: list | None = None):
    client = get_client()
    return await client.execute(sql, params or [])


async def db_fetchone(sql: str, params: list | None = None) -> dict | None:
    result = await db_execute(sql, params)
    if not result.rows:
        return None
    return dict(zip(result.columns, result.rows[0]))


async def db_fetchall(sql: str, params: list | None = None) -> list[dict]:
    result = await db_execute(sql, params)
    return [dict(zip(result.columns, row)) for row in result.rows]


async def _safe_alter(sql: str) -> None:
    try:
        await db_execute(sql)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "already exists" in str(exc).lower():
            return
        logger.debug("ALTER skipped: %s", exc)


async def init_database() -> None:
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
            free_date TEXT NOT NULL, free_used INTEGER NOT NULL DEFAULT 0,
            paid_credits INTEGER NOT NULL DEFAULT 0, unlimited_until TEXT,
            registered_at TEXT, last_seen_at TEXT, total_conversions INTEGER NOT NULL DEFAULT 0,
            successful_conversions INTEGER NOT NULL DEFAULT 0, failed_conversions INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            payload TEXT NOT NULL, stars INTEGER NOT NULL, charge_id TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
        )
        """
    )
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            filename TEXT, status TEXT NOT NULL, quota_source TEXT, file_size INTEGER,
            created_at TEXT NOT NULL, converted_file_id TEXT, original_file_id TEXT
        )
        """
    )
    # 🚀 NEW: Pending Tasks Table for Queue & Auto-Resume
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS pending_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL, file_id TEXT NOT NULL, filename TEXT,
            file_size INTEGER, source TEXT, retry_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', created_at TEXT NOT NULL
        )
        """
    )
    await _safe_alter("ALTER TABLE conversions ADD COLUMN converted_file_id TEXT")
    await _safe_alter("ALTER TABLE conversions ADD COLUMN original_file_id TEXT")
    logger.info("Database schema ready.")


# ===========================================================================
# TIME & USER HELPERS
# ===========================================================================
def today_bd() -> str: return datetime.now(TIMEZONE).date().isoformat()
def now_bd() -> datetime: return datetime.now(TIMEZONE)

def _dt_display(value) -> str:
    if not value: return "Unknown"
    try: return datetime.fromisoformat(value).strftime("%d-%m-%Y %I:%M:%S %p")
    except (ValueError, TypeError): return str(value)

def _dt_short(value) -> str:
    if not value: return "?"
    try: return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M")
    except (ValueError, TypeError): return str(value)

def spawn(coro) -> None:
    try: asyncio.create_task(coro)
    except RuntimeError: logger.warning("spawn() outside event loop")

async def ensure_user(user) -> dict:
    user_id = user.id
    today = today_bd()
    now = now_bd().isoformat()
    row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

    if row is None:
        await db_execute("INSERT INTO users (user_id, first_name, username, free_date, free_used, registered_at, last_seen_at) VALUES (?, ?, ?, ?, 0, ?, ?)", [user_id, user.first_name or "", user.username or "", today, now, now])
    else:
        spawn(db_execute("UPDATE users SET first_name = ?, username = ?, last_seen_at = ? WHERE user_id = ?", [user.first_name or "", user.username or "", now, user_id]))
        if row["free_date"] != today:
            await db_execute("UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?", [today, user_id])
    return await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

async def get_user_row(user_id: int) -> dict | None:
    row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])
    if row and row["free_date"] != today_bd():
        await db_execute("UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?", [today_bd(), user_id])
        row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])
    return row

def unlimited_active(row: dict | None) -> bool:
    if not row or not row.get("unlimited_until"): return False
    try: return datetime.fromisoformat(row["unlimited_until"]) > now_bd()
    except (ValueError, TypeError): return False

async def can_convert(user_id: int) -> tuple[bool, str]:
    if user_id == OWNER_ID: return True, "owner"
    row = await get_user_row(user_id)
    if row is None: return True, "free"
    if unlimited_active(row): return True, "unlimited"
    if (FREE_DAILY_LIMIT - row["free_used"]) > 0: return True, "free"
    if row["paid_credits"] > 0: return True, "paid"
    return False, "none"

async def consume_conversion(user_id: int, source: str) -> None:
    if user_id == OWNER_ID: return
    if source == "free":
        await db_execute("UPDATE users SET free_used = free_used + 1 WHERE user_id = ?", [user_id])
    elif source == "paid":
        await db_execute("UPDATE users SET paid_credits = paid_credits - 1 WHERE user_id = ? AND paid_credits > 0", [user_id])

async def add_paid_credits(user_id: int, amount: int) -> None:
    await db_execute("UPDATE users SET paid_credits = paid_credits + ? WHERE user_id = ?", [amount, user_id])

async def add_unlimited_days(user_id: int, days: int = UNLIMITED_DAYS) -> None:
    current = await get_user_row(user_id)
    current_until = None
    if current and current.get("unlimited_until"):
        try: current_until = datetime.fromisoformat(current["unlimited_until"])
        except ValueError: pass
    base = now_bd()
    if current_until and current_until > base: base = current_until
    until = base + timedelta(days=days)
    await db_execute("UPDATE users SET unlimited_until = ? WHERE user_id = ?", [until.isoformat(), user_id])

async def remove_unlimited(user_id: int) -> None:
    await db_execute("UPDATE users SET unlimited_until = NULL WHERE user_id = ?", [user_id])

async def record_payment(user_id: int, payload: str, stars: int, charge_id: str) -> bool:
    try:
        await db_execute("INSERT INTO payments (user_id, payload, stars, charge_id, created_at) VALUES (?, ?, ?, ?, ?)", [user_id, payload, stars, charge_id, now_bd().isoformat()])
        return True
    except Exception: return False

async def record_conversion(user_id: int, filename: str, status: str, quota_source: str | None, file_size: int | None, converted_file_id: str | None = None, original_file_id: str | None = None) -> None:
    created_at = now_bd().isoformat()
    try:
        await db_execute(
            "INSERT INTO conversions (user_id, filename, status, quota_source, file_size, created_at, converted_file_id, original_file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [user_id, filename, status, quota_source, file_size, created_at, converted_file_id, original_file_id],
        )
        if status == "success":
            await db_execute("UPDATE users SET total_conversions = total_conversions + 1, successful_conversions = successful_conversions + 1, last_seen_at = ? WHERE user_id = ?", [created_at, user_id])
        elif status == "failed":
            await db_execute("UPDATE users SET total_conversions = total_conversions + 1, failed_conversions = failed_conversions + 1, last_seen_at = ? WHERE user_id = ?", [created_at, user_id])
    except Exception as e:
        logger.error("Failed to record conversion: %s", e)


# ===========================================================================
# 🚀 QUEUE WORKER LOGIC
# ===========================================================================
async def queue_worker(application: Application, worker_id: int):
    """Background worker that processes files from the queue."""
    logger.info(f"🚀 Worker {worker_id} started.")
    bot = application.bot

    while True:
        try:
            task_id = await conversion_queue.get()
            
            # Fetch task from DB
            task = await db_fetchone("SELECT * FROM pending_tasks WHERE id = ? AND status IN ('pending', 'processing')", [task_id])
            if not task:
                conversion_queue.task_done()
                continue

            # Mark as processing
            await db_execute("UPDATE pending_tasks SET status = 'processing' WHERE id = ?", [task_id])

            file_id = task["file_id"]
            filename = task["filename"]
            user_id = task["user_id"]
            chat_id = task["chat_id"]
            source = task["source"]
            file_size = task["file_size"]
            retry_count = task["retry_count"]

            extension = Path(filename).suffix.lower()
            unique_id = f"task_{task_id}"
            input_file = DOWNLOAD_DIR / f"{unique_id}{extension}"
            output_file = OUTPUT_DIR / f"{unique_id}.jpg"
            output_filename = Path(filename).with_suffix(".jpg").name

            success = False
            converted_file_id = None
            error_msg = ""

            try:
                # 1. Download
                tg_file = await bot.get_file(file_id)
                await tg_file.download_to_drive(input_file)

                # 2. Convert
                await asyncio.to_thread(convert_to_jpg, input_file, output_file, JPEG_QUALITY)

                # 3. Upload
                with output_file.open("rb") as jpg_file:
                    sent = await bot.send_document(
                        chat_id=chat_id,
                        document=jpg_file,
                        filename=output_filename,
                        caption=f"✅ Conversion complete!\n📄 `{output_filename}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                if sent.document:
                    converted_file_id = sent.document.file_id
                success = True

            except telegram.error.RetryAfter as e:
                # Telegram Flood Control
                logger.warning(f"Flood control hit. Sleeping for {e.retry_after} seconds.")
                await asyncio.sleep(e.retry_after)
                error_msg = "Flood limit"
            except ConversionError as e:
                error_msg = str(e)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Worker {worker_id} error: {e}")

            # 4. Handle Results
            if success:
                # Delete from pending tasks
                await db_execute("DELETE FROM pending_tasks WHERE id = ?", [task_id])
                
                # Record success in DB
                spawn(consume_conversion(user_id, source))
                spawn(record_conversion(user_id, filename, "success", source, file_size, converted_file_id, file_id))
                logger.info(f"✅ Worker {worker_id} processed: {filename}")
            else:
                retry_count += 1
                if retry_count <= MAX_RETRIES:
                    # Put back in queue (requeue)
                    await db_execute("UPDATE pending_tasks SET retry_count = ?, status = 'pending' WHERE id = ?", [retry_count, task_id])
                    await asyncio.sleep(2) # Small delay before retry
                    await conversion_queue.put(task_id)
                    logger.warning(f"⚠️ Worker {worker_id} failed {filename}. Retry {retry_count}/{MAX_RETRIES}.")
                else:
                    # Max retries reached
                    await db_execute("UPDATE pending_tasks SET status = 'failed' WHERE id = ?", [task_id])
                    spawn(record_conversion(user_id, filename, "failed", source, file_size, None, file_id))
                    logger.error(f"❌ Worker {worker_id} permanently failed: {filename}")
                    
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"❌ *Failed to convert.*\n📄 `{filename}`\nReason: `{error_msg[:100]}`",
                            parse_mode="Markdown"
                        )
                    except Exception: pass

            # 5. Cleanup
            safe_unlink(input_file)
            safe_unlink(output_file)
            gc.collect() # Force memory cleanup
            
            # Small delay to prevent API flooding
            await asyncio.sleep(1)
            conversion_queue.task_done()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Queue worker fatal error: {e}")
            await asyncio.sleep(5)


# ===========================================================================
# IMAGE CONVERSION LOGIC
# ===========================================================================
class ConversionError(Exception):
    pass

def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS

def safe_unlink(path: Path) -> None:
    try: path.unlink(missing_ok=True)
    except OSError: pass

def _decode_heic_smart(input_file: Path) -> Image.Image:
    heif = pillow_heif.open_heif(str(input_file), convert_hdr_to_8bit=True)
    w, h = heif.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        scale = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
        heif = heif.scale(scale)
    return heif.to_pillow()

def _decode_other_format(input_file: Path) -> Image.Image:
    img = Image.open(input_file)
    try: img.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
    except Exception: pass
    return img

def convert_to_jpg(input_file: Path, output_file: Path, quality: int = JPEG_QUALITY) -> None:
    suffix = input_file.suffix.lower()
    image = None
    try:
        if suffix in {".heic", ".heif"}:
            image = _decode_heic_smart(input_file)
        else:
            image = _decode_other_format(input_file)

        if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

        if image.mode in ("RGBA", "LA", "P"):
            rgb_image = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode in ("RGBA", "LA"):
                rgb_image.paste(image, mask=image.split()[-1])
            else:
                rgb_image.paste(image)
            image = rgb_image
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.save(output_file, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        logger.exception("Conversion failed for %s", input_file)
        raise ConversionError(str(exc)) from exc
    finally:
        if image is not None:
            try: image.close()
            except Exception: pass


# ===========================================================================
# MESSAGE HANDLERS
# ===========================================================================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not message.document or not user: return

    document = message.document
    filename = document.file_name or "image.heic"

    if not is_supported_file(filename):
        await message.reply_text("❌ *Unsupported file type.*", parse_mode="Markdown")
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"❌ *File too large.* Max allowed: *{MAX_FILE_SIZE_MB} MB*", parse_mode="Markdown")
        return

    # Check quota (only once)
    allowed, source = await can_convert(user.id)
    if not allowed:
        await message.reply_text("🚫 *Free conversions finished for today.*\n\n💰 Choose a plan to continue:", parse_mode="Markdown", reply_markup=plans_keyboard())
        return

    # 🚀 Add to DB and Queue
    now = now_bd().isoformat()
    await db_execute(
        """INSERT INTO pending_tasks (user_id, chat_id, file_id, filename, file_size, source, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        [user.id, message.chat_id, document.file_id, filename, document.file_size, source, now]
    )
    
    # Get the ID of the inserted row
    task_row = await db_fetchone("SELECT last_insert_rowid() as id")
    task_id = task_row["id"] if task_row else None
    
    if task_id:
        await conversion_queue.put(task_id)
        queue_size = conversion_queue.qsize()
        await message.reply_text(
            f"📥 *Added to queue.*\n📄 `{filename}`\n⏳ Position: *{queue_size}*\n\n_Processing will start automatically._",
            parse_mode="Markdown"
        )
    else:
        await message.reply_text("❌ System error. Please try again.")


# ===========================================================================
# STARTUP & MAIN
# ===========================================================================
async def on_startup(application: Application) -> None:
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception: pass

    await init_database()
    
    # 1. Reset any 'processing' tasks back to 'pending' (because bot restarted)
    await db_execute("UPDATE pending_tasks SET status = 'pending' WHERE status = 'processing'")
    
    # 2. Load all pending tasks from DB into memory queue
    pending_tasks = await db_fetchall("SELECT id FROM pending_tasks WHERE status = 'pending' ORDER BY id ASC")
    if pending_tasks:
        logger.info(f"📦 Resuming {len(pending_tasks)} pending tasks from database...")
        for row in pending_tasks:
            await conversion_queue.put(row["id"])
            
    # 3. Start 5 Background Workers
    for i in range(1, MAX_WORKERS + 1):
        application.create_task(queue_worker(application, i))


def main() -> None:
    if not BOT_TOKEN or not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError("Environment variables missing.")

    threading.Thread(target=run_flask, daemon=True).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(300)
        .write_timeout(300)
        .pool_timeout(60)
        .concurrent_updates(True)
        .post_init(on_startup)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
