"""
Image → JPG Telegram Bot with Turso (Professional, Crash-Free Edition)
===================================================================
✅ NEW: Removed FFmpeg (Pure Python for Render compatibility)
✅ NEW: Memory Optimized (MAX_DIMENSION = 1024) to prevent 512MB OOM
✅ NEW: Added 2-minute Timeout to skip infinite hangs
✅ NEW: Auto-skip crashed files on restart ("FILENAME-skip position X")
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
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
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
# FLASK APP (Render Health Check)
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
JPEG_QUALITY = 90
LOG_LEVEL = logging.INFO
SUPPORTED_EXTENSIONS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".jpe",
    ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

# 🚀 CRITICAL: Lowered to 1024 to prevent Render 512MB RAM crash (OOM)
MAX_DIMENSION = 1024 
Image.MAX_IMAGE_PIXELS = None

USERS_PER_PAGE = 8
CONVS_PER_PAGE = 8

# 🚀 Limits & Retries
MAX_RETRIES = 3            # Auto retry attempts per task (internal)
MAX_USER_RETRIES = 3       # User-facing "Try Again" clicks before only Skip remains
MAX_BATCH_SIZE = 1000
CONVERSION_TIMEOUT = 120   # 2 Minutes timeout per file

# 🚀 In-memory state
user_uploads: dict[int, list[dict]] = {}
user_batches: dict[int, dict] = {}
cancelled_users: set[int] = set()
user_locks: dict[int, asyncio.Lock] = {}
active_processing: set[int] = set()

# ===========================================================================
# INITIALIZATION
# ===========================================================================
pillow_heif.register_heif_opener()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)


# ===========================================================================
# KEYBOARD BUILDERS (Floating Menus)
# ===========================================================================
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📊 Status"), KeyboardButton("⭐ Plans")],
        [KeyboardButton("❓ Help")]
    ]
    if user_id == OWNER_ID:
        rows.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

def get_upload_keyboard(count: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(f"✅ Done - Start Conversion ({count} files)")],
            [KeyboardButton("❌ Cancel Upload")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_processing_keyboard(completed: int = 0, total: int = 0) -> ReplyKeyboardMarkup:
    btn_text = f"🔄 Progress: {completed}/{total}" if total > 0 else "🔄 Converting..."
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(btn_text)],
            [KeyboardButton("❌ Cancel Conversion")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_retry_skip_keyboard(completed: int = 0, total: int = 0) -> ReplyKeyboardMarkup:
    btn_text = f"🔄 Progress: {completed}/{total}" if total > 0 else "🔄 Converting..."
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔁 Retry Failed File"), KeyboardButton("⏭️ Skip This File")],
            [KeyboardButton(btn_text)],
            [KeyboardButton("❌ Cancel Conversion")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_skip_only_keyboard(completed: int = 0, total: int = 0) -> ReplyKeyboardMarkup:
    btn_text = f"🔄 Progress: {completed}/{total}" if total > 0 else "🔄 Converting..."
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⏭️ Skip This File")],
            [KeyboardButton(btn_text)],
            [KeyboardButton("❌ Cancel Conversion")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👥 All Users"), KeyboardButton("🔍 Search User")],
            [KeyboardButton("🏠 Back to Home")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )


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
    return _db_client

async def db_execute(sql: str, params: list | None = None):
    return await get_client().execute(sql, params or [])

async def db_fetchone(sql: str, params: list | None = None) -> dict | None:
    result = await db_execute(sql, params)
    return dict(zip(result.columns, result.rows[0])) if result.rows else None

async def db_fetchall(sql: str, params: list | None = None) -> list[dict]:
    result = await db_execute(sql, params)
    return [dict(zip(result.columns, row)) for row in result.rows]

async def init_database() -> None:
    await db_execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, free_date TEXT, free_used INTEGER DEFAULT 0, paid_credits INTEGER DEFAULT 0, unlimited_until TEXT, registered_at TEXT, last_seen_at TEXT, total_conversions INTEGER DEFAULT 0, successful_conversions INTEGER DEFAULT 0, failed_conversions INTEGER DEFAULT 0)")
    await db_execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, payload TEXT, stars INTEGER, charge_id TEXT UNIQUE, created_at TEXT)")
    await db_execute("CREATE TABLE IF NOT EXISTS conversions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, filename TEXT, status TEXT, quota_source TEXT, file_size INTEGER, created_at TEXT, converted_file_id TEXT, original_file_id TEXT)")

    await db_execute("CREATE TABLE IF NOT EXISTS pending_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, file_id TEXT, filename TEXT, file_size INTEGER, source TEXT, retry_count INTEGER DEFAULT 0, crash_count INTEGER DEFAULT 0, user_retry_count INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', created_at TEXT, position INTEGER DEFAULT 1)")

    for col_def in ["crash_count INTEGER DEFAULT 0", "user_retry_count INTEGER DEFAULT 0"]:
        try:
            await db_execute(f"ALTER TABLE pending_tasks ADD COLUMN {col_def}")
        except Exception:
            pass


# ===========================================================================
# TIME & USER HELPERS
# ===========================================================================
def today_bd() -> str: return datetime.now(TIMEZONE).date().isoformat()
def now_bd() -> datetime: return datetime.now(TIMEZONE)

def _dt_display(value) -> str:
    if not value: return "Unknown"
    try: return datetime.fromisoformat(value).strftime("%d-%m-%Y %I:%M:%S %p")
    except: return str(value)

def _dt_short(value) -> str:
    if not value: return "?"
    try: return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M")
    except: return str(value)

def spawn(coro) -> None:
    try: asyncio.create_task(coro)
    except: pass

async def ensure_user(user) -> dict:
    user_id, today, now = user.id, today_bd(), now_bd().isoformat()
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
    except: return False

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
        except: pass
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
    except: return False

async def record_conversion(user_id: int, filename: str, status: str, source: str | None, size: int | None, conv_id: str | None = None, org_id: str | None = None) -> None:
    created_at = now_bd().isoformat()
    try:
        await db_execute("INSERT INTO conversions (user_id, filename, status, quota_source, file_size, created_at, converted_file_id, original_file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [user_id, filename, status, source, size, created_at, conv_id, org_id])
        if status == "success":
            await db_execute("UPDATE users SET total_conversions = total_conversions + 1, successful_conversions = successful_conversions + 1, last_seen_at = ? WHERE user_id = ?", [created_at, user_id])
        elif status == "failed":
            await db_execute("UPDATE users SET total_conversions = total_conversions + 1, failed_conversions = failed_conversions + 1, last_seen_at = ? WHERE user_id = ?", [created_at, user_id])
    except: pass

# ===========================================================================
# ADMIN HELPERS
# ===========================================================================
async def payment_summary(user_id: int) -> dict:
    row = await db_fetchone("SELECT COUNT(*) AS count, COALESCE(SUM(stars), 0) AS stars FROM payments WHERE user_id = ?", [user_id])
    payments = await db_fetchall("SELECT payload, stars, charge_id, created_at FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", [user_id])
    return {"count": row["count"], "stars": row["stars"], "payments": payments}

async def conversion_summary(user_id: int) -> dict:
    row = await db_fetchone("SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS successful, COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed FROM conversions WHERE user_id = ?", [user_id])
    recent = await db_fetchall("SELECT id, filename, status, quota_source, file_size, created_at, converted_file_id FROM conversions WHERE user_id = ? ORDER BY created_at DESC LIMIT 20", [user_id])
    return {"total": row["total"], "successful": row["successful"], "failed": row["failed"], "recent": recent}

async def get_admin_stats() -> dict:
    now, today = now_bd().isoformat(), today_bd()
    return {
        "total_users": (await db_fetchone("SELECT COUNT(*) AS c FROM users"))["c"],
        "active_today": (await db_fetchone("SELECT COUNT(DISTINCT user_id) AS c FROM conversions WHERE status = 'success' AND substr(created_at, 1, 10) = ?", [today]))["c"],
        "total_attempts": (await db_fetchone("SELECT COUNT(*) AS c FROM conversions"))["c"],
        "total_success": (await db_fetchone("SELECT COUNT(*) AS c FROM conversions WHERE status = 'success'"))["c"],
        "total_failed": (await db_fetchone("SELECT COUNT(*) AS c FROM conversions WHERE status = 'failed'"))["c"],
        "active_unlimited": (await db_fetchone("SELECT COUNT(*) AS c FROM users WHERE user_id != ? AND unlimited_until IS NOT NULL AND unlimited_until > ?", [OWNER_ID, now]))["c"],
        "expired_unlimited": (await db_fetchone("SELECT COUNT(*) AS c FROM users WHERE user_id != ? AND unlimited_until IS NOT NULL AND unlimited_until <= ?", [OWNER_ID, now]))["c"],
        "total_paid_credits": (await db_fetchone("SELECT COALESCE(SUM(paid_credits), 0) AS c FROM users"))["c"],
        "payment_count": (await db_fetchone("SELECT COUNT(*) AS c FROM payments"))["c"],
        "total_stars": (await db_fetchone("SELECT COALESCE(SUM(stars), 0) AS c FROM payments"))["c"],
    }

async def search_users(query: str, limit: int = 30) -> list[dict]:
    q = query.strip()
    if not q: return []
    like = f"%{q}%"
    return await db_fetchall("SELECT * FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?) OR LOWER(COALESCE(username, '')) LIKE LOWER(?) ORDER BY last_seen_at DESC LIMIT ?", [like, like, like, limit])


# ===========================================================================
# SERIAL PER-USER PROCESSING
# ===========================================================================

async def _get_batch_counts(user_id: int) -> dict:
    row = await db_fetchone(
        "SELECT "
        " COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS success,"
        " COALESCE(SUM(CASE WHEN status IN ('failed','skipped','cancelled') THEN 1 ELSE 0 END),0) AS failed "
        "FROM pending_tasks WHERE user_id = ?",
        [user_id],
    )
    return {
        "total": row["total"] if row else 0,
        "success": row["success"] if row else 0,
        "failed": row["failed"] if row else 0,
    }


async def _replace_floating_keyboard(
    bot, user_id: int, chat_id: int,
    completed: int, total: int,
    kb_type: str = "processing"
):
    batch = user_batches.get(user_id)
    if not batch:
        return
    try:
        if kb_type == "retry_skip":
            kb_markup = get_retry_skip_keyboard(completed, total)
        elif kb_type == "skip_only":
            kb_markup = get_skip_only_keyboard(completed, total)
        else:
            kb_markup = get_processing_keyboard(completed, total)

        new_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"📊 {completed}/{total}",
            reply_markup=kb_markup,
        )
        old_id = batch.get("kb_msg_id")
        batch["kb_msg_id"] = new_msg.message_id
        if old_id:
            try: await bot.delete_message(chat_id=chat_id, message_id=old_id)
            except Exception: pass
    except Exception as e:
        logger.warning(f"Keyboard replace failed: {e}")


async def update_progress_display(bot, user_id: int, chat_id: int, force_kb_update: bool = False):
    batch = user_batches.get(user_id)
    if not batch: return
    counts = await _get_batch_counts(user_id)

    text = (
        f"🚀 *Converting files...*\n\n"
        f"📦 Total: *{counts['total']}*\n"
        f"✅ Success: *{counts['success']}*\n"
        f"⚠️ Failed/Skipped: *{counts['failed']}*"
    )
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=batch["status_msg_id"],
            text=text, parse_mode=ParseMode.MARKDOWN,
        )
    except telegram.error.BadRequest:
        pass
    except Exception:
        pass

    last_kb_update = batch.get("last_kb_update", -1)
    current = counts["success"] + counts["failed"]
    if force_kb_update or (current - last_kb_update >= 5):
        batch["last_kb_update"] = current
        await _replace_floating_keyboard(bot, user_id, chat_id, counts["success"], counts["total"])


async def show_retry_skip_prompt(bot, user_id: int, chat_id: int, task: dict, error_msg: str):
    batch = user_batches.get(user_id)
    counts = await _get_batch_counts(user_id)
    safe_err = str(error_msg).replace('`', '').replace('*', '').replace('_', ' ')
    safe_fn = str(task['filename']).replace('`', '')

    user_retries = task.get("user_retry_count", 0) or 0
    can_retry = user_retries < MAX_USER_RETRIES

    text = (
        f"⚠️ *Conversion failed!*\n\n"
        f"📄 File: `{safe_fn}`\n"
        f"❗ Error: `{safe_err}`\n\n"
        f"📊 Progress: *{counts['success']}/{counts['total']}*\n"
        f"🔁 Retries used: *{user_retries}/{MAX_USER_RETRIES}*\n\n"
    )

    if can_retry:
        text += "👇 *Retry the file or skip it?*"
        kb_type = "retry_skip"
        kb_markup = get_retry_skip_keyboard(counts["success"], counts["total"])
    else:
        text += (
            f"❌ *Maximum retries reached!*\n"
            f"Only *⏭️ Skip This File* is available now.\n"
            f"Tap it to continue with the next file."
        )
        kb_type = "skip_only"
        kb_markup = get_skip_only_keyboard(counts["success"], counts["total"])

    if not batch:
        try:
            status_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            kb_msg = await bot.send_message(chat_id=chat_id, text=f"📊 {counts['success']}/{counts['total']}", reply_markup=kb_markup)
            user_batches[user_id] = {
                "total": counts["total"], "status_msg_id": status_msg.message_id,
                "kb_msg_id": kb_msg.message_id, "chat_id": chat_id,
                "last_kb_update": counts["success"] + counts["failed"]
            }
        except Exception: pass
    else:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=batch["status_msg_id"], text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        await _replace_floating_keyboard(bot, user_id, chat_id, counts["success"], counts["total"], kb_type=kb_type)


async def finish_batch(bot, user_id: int, chat_id: int, cancelled: bool = False):
    batch = user_batches.pop(user_id, None)
    counts = await _get_batch_counts(user_id)

    skipped_tasks = await db_fetchall("SELECT filename, position FROM pending_tasks WHERE user_id = ? AND status = 'skipped' ORDER BY position ASC", [user_id])
    skipped_names = [f"{s['filename']}-skip position {s['position']}" for s in skipped_tasks]

    await db_execute("DELETE FROM pending_tasks WHERE user_id = ?", [user_id])
    cancelled_users.discard(user_id)

    if batch:
        try: await bot.delete_message(chat_id=chat_id, message_id=batch["status_msg_id"])
        except Exception: pass
        try: await bot.delete_message(chat_id=chat_id, message_id=batch["kb_msg_id"])
        except Exception: pass

    if cancelled:
        text = f"❌ *Conversion cancelled.*\n\n✅ Success: *{counts['success']}*"
    else:
        text = f"🎉 *Conversion finished!*\n\n📦 Total: *{counts['total']}*\n✅ Success: *{counts['success']}*"
        if counts["failed"] > 0:
            text += f"\n⚠️ Failed/Skipped: *{counts['failed']}*"

    if skipped_names:
        text += "\n\n⏭️ *Skipped files:*"
        for name in skipped_names[:10]:
            text += f"\n   • `{name}`"
        if len(skipped_names) > 10:
            text += f"\n   ... and *{len(skipped_names) - 10}* more"

    try:
        await bot.send_message(chat_id=chat_id, text=text + "\n\n👇 Use the menu below.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(user_id))
    except Exception: pass


async def process_single_task(bot, task: dict) -> dict:
    user_id = task["user_id"]
    file_id = task["file_id"]
    filename = task["filename"]
    chat_id = task["chat_id"]
    position = task.get("position", 1)

    if user_id in cancelled_users:
        return {"status": "cancelled", "error": "User cancelled"}

    ext = Path(filename).suffix.lower()
    unique_id = f"task_{task['id']}"
    input_file = DOWNLOAD_DIR / f"{unique_id}{ext}"
    output_file = OUTPUT_DIR / f"{unique_id}.jpg"
    out_name = Path(filename).with_suffix(".jpg").name

    try:
        while True:
            try:
                tg_file = await bot.get_file(file_id)
                await tg_file.download_to_drive(input_file)
                break
            except telegram.error.RetryAfter as e:
                await asyncio.sleep(e.retry_after)

        if user_id in cancelled_users:
            return {"status": "cancelled", "error": "User cancelled"}

        # 🚀 ADDED TIMEOUT: If conversion takes > 2 mins, it aborts and skips
        try:
            await asyncio.wait_for(
                asyncio.to_thread(convert_to_jpg, input_file, output_file, JPEG_QUALITY),
                timeout=CONVERSION_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout converting task {task['id']} ({filename}). Skipping.")
            return {"status": "failed", "error": "Timeout: File is too large or complex to convert."}

        if user_id in cancelled_users:
            return {"status": "cancelled", "error": "User cancelled"}

        with output_file.open("rb") as jpg_file:
            while True:
                try:
                    sent = await bot.send_document(
                        chat_id=chat_id, document=jpg_file, filename=out_name,
                        caption=f"✅ Conversion complete! Position {position}\n📄 `{out_name}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    break
                except telegram.error.RetryAfter as e:
                    await asyncio.sleep(e.retry_after)

        conv_id = sent.document.file_id if sent.document else None
        return {"status": "success", "conv_id": conv_id}

    except Exception as e:
        logger.error(f"Task {task['id']} failed: {e}")
        return {"status": "failed", "error": str(e)[:200]}
    finally:
        safe_unlink(input_file)
        safe_unlink(output_file)
        gc.collect()


async def process_user_queue(bot, user_id: int, chat_id: int):
    if user_id in active_processing:
        return
    active_processing.add(user_id)
    try:
        while True:
            if user_id in cancelled_users:
                await db_execute("UPDATE pending_tasks SET status = 'cancelled' WHERE user_id = ? AND status IN ('pending','processing','awaiting_retry')", [user_id])
                await finish_batch(bot, user_id, chat_id, cancelled=True)
                return

            task = await db_fetchone("SELECT * FROM pending_tasks WHERE user_id = ? AND status = 'pending' ORDER BY position ASC, id ASC LIMIT 1", [user_id])
            if not task:
                awaiting = await db_fetchone("SELECT * FROM pending_tasks WHERE user_id = ? AND status = 'awaiting_retry' ORDER BY position ASC, id ASC LIMIT 1", [user_id])
                if awaiting:
                    await show_retry_skip_prompt(bot, user_id, chat_id, awaiting, "Previous error")
                    return
                await finish_batch(bot, user_id, chat_id)
                return

            # 🚀 AUTO-SKIP IF SERVER CRASHED PREVIOUSLY
            crash_count = task.get("crash_count", 0) or 0
            if crash_count >= 1:
                await db_execute("UPDATE pending_tasks SET status = 'skipped' WHERE id = ?", [task["id"]])
                skip_filename = f"{task['filename']}-skip position {task['position']}"
                spawn(record_conversion(user_id, skip_filename, "failed", task["source"], task["file_size"], None, task["file_id"]))
                
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"🚨 *Server Crash Auto-Skip*\n\n"
                             f"📄 File: `{task['filename']}`\n"
                             f"📌 Position: `{task['position']}`\n\n"
                             f"⚠️ This file was too large and caused the server to crash. It has been automatically skipped.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception: pass
                
                await update_progress_display(bot, user_id, chat_id, force_kb_update=True)
                await asyncio.sleep(1)
                continue

            await db_execute("UPDATE pending_tasks SET status = 'processing' WHERE id = ?", [task["id"]])

            result = None
            attempt = task.get("retry_count", 0) or 0

            while attempt < MAX_RETRIES:
                if user_id in cancelled_users:
                    result = {"status": "cancelled", "error": "User cancelled"}
                    break
                result = await process_single_task(bot, task)
                if result["status"] in ("success", "cancelled"):
                    break
                attempt += 1
                if attempt < MAX_RETRIES:
                    logger.info(f"Auto-retry {attempt}/{MAX_RETRIES} for task {task['id']}")
                    await db_execute("UPDATE pending_tasks SET retry_count = ? WHERE id = ?", [attempt, task["id"]])
                    await asyncio.sleep(2)

            if result is None:
                result = {"status": "failed", "error": "Unknown error"}

            if result["status"] == "success":
                await db_execute("UPDATE pending_tasks SET status = 'success' WHERE id = ?", [task["id"]])
                spawn(record_conversion(user_id, task["filename"], "success", task["source"], task["file_size"], result.get("conv_id"), task["file_id"]))
                spawn(consume_conversion(user_id, task["source"]))
                await update_progress_display(bot, user_id, chat_id)
                await asyncio.sleep(0.5)
                continue

            if result["status"] == "cancelled":
                await db_execute("UPDATE pending_tasks SET status = 'cancelled' WHERE id = ?", [task["id"]])
                await finish_batch(bot, user_id, chat_id, cancelled=True)
                return

            await db_execute("UPDATE pending_tasks SET status = 'awaiting_retry' WHERE id = ?", [task["id"]])
            task = await db_fetchone("SELECT * FROM pending_tasks WHERE id = ?", [task["id"]])
            await show_retry_skip_prompt(bot, user_id, chat_id, task, result.get("error", "Failed after max retries"))
            return

    except Exception as e:
        logger.exception(f"process_user_queue crashed for user {user_id}: {e}")
        try: await finish_batch(bot, user_id, chat_id)
        except Exception: pass
    finally:
        active_processing.discard(user_id)


async def resume_user_processing(bot, user_id: int, chat_id: int):
    counts = await _get_batch_counts(user_id)
    if counts["total"] == 0: return

    try:
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"🔄 *Bot restarted — resuming...*\n\n📦 Total: *{counts['total']}*\n✅ Success: *{counts['success']}*\n⚠️ Failed/Skipped: *{counts['failed']}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        kb_msg = await bot.send_message(chat_id=chat_id, text=f"📊 {counts['success']}/{counts['total']}", reply_markup=get_processing_keyboard(counts["success"], counts["total"]))
        user_batches[user_id] = {
            "total": counts["total"], "status_msg_id": status_msg.message_id,
            "kb_msg_id": kb_msg.message_id, "chat_id": chat_id,
            "last_kb_update": counts["success"] + counts["failed"]
        }
    except Exception: return

    awaiting = await db_fetchone("SELECT * FROM pending_tasks WHERE user_id = ? AND status = 'awaiting_retry' ORDER BY position ASC, id ASC LIMIT 1", [user_id])
    if awaiting:
        await show_retry_skip_prompt(bot, user_id, chat_id, awaiting, "Previous error (resumed after restart)")
    else:
        spawn(process_user_queue(bot, user_id, chat_id))


# ===========================================================================
# IMAGE CONVERSION LOGIC (Pure Python, Highly Memory Optimized)
# ===========================================================================
class ConversionError(Exception): pass

def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS

def safe_unlink(path: Path) -> None:
    try: path.unlink(missing_ok=True)
    except: pass

def convert_to_jpg(input_file: Path, output_file: Path, quality: int = JPEG_QUALITY) -> None:
    image = None
    heif = None
    try:
        if input_file.suffix.lower() in {".heic", ".heif"}:
            heif = pillow_heif.open_heif(str(input_file), convert_hdr_to_8bit=True)
            w, h = heif.size
            
            if w > MAX_DIMENSION or h > MAX_DIMENSION:
                ratio = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
                if hasattr(heif, 'scale'):
                    heif.scale(ratio)
                else:
                    raise ConversionError("Your pillow_heif version is too old. Please ask admin to upgrade.")
                    
            image = heif.to_pillow()
            del heif
            heif = None
        else:
            image = Image.open(input_file)
            try: image.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
            except: pass

        if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

        if image.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", image.size, (255, 255, 255))
            bg.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image.close()
            image = bg
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.save(output_file, format="JPEG", quality=quality, optimize=True)
        
    except (UnidentifiedImageError, OSError) as e:
        raise ConversionError(f"Corrupt or unreadable image file: {e}")
    except Exception as e:
        raise ConversionError(f"Image processing failed: {e}")
    finally:
        if image is not None:
            image.close()
            del image
        if heif is not None:
            del heif
        gc.collect()


# ===========================================================================
# UI BUILDERS (Admin Panel)
# ===========================================================================
def dashboard_text(stats: dict) -> str:
    return (
        "🔐  *ADMIN DASHBOARD*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Registered users      :  *{stats['total_users']}*\n"
        f"📈 Active today          :  *{stats['active_today']}*\n\n"
        "🔄  *CONVERSIONS*\n"
        f"   • Total attempts   :  `{stats['total_attempts']}`\n"
        f"   • ✅ Successful     :  `{stats['total_success']}`\n"
        f"   • ❌ Failed          :  `{stats['total_failed']}`\n\n"
        "📦  *PLANS*\n"
        f"   • Active unlimited :  `{stats['active_unlimited']}`\n"
        f"   • Expired unlimited:  `{stats['expired_unlimited']}`\n"
        f"   • Paid credits sum :  `{stats['total_paid_credits']}`\n\n"
        "💳  *PAYMENTS*\n"
        f"   • Successful       :  `{stats['payment_count']}`\n"
        f"   • Total stars      :  `{stats['total_stars']}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Use the inline buttons below_"
    )

def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥  All Users", callback_data="adm_users:0")],
        [InlineKeyboardButton("🔍  Search User", callback_data="adm_search")],
        [InlineKeyboardButton("🔄  Recent Conv.", callback_data="adm_recent_conv:0"), InlineKeyboardButton("💳  Recent Pay.", callback_data="adm_recent_pay:0")],
        [InlineKeyboardButton("🔄  Refresh Stats", callback_data="adm_home")],
    ])

def users_page_text(page: int, total: int) -> str:
    return f"👥  *ALL REGISTERED USERS*\n━━━━━━━━━━━━━━━━━━━━\n\n📄  Page  *{page + 1}*  /  *{max(1, math.ceil(total / USERS_PER_PAGE))}*\n👤  Total  *{total}*  users\n\n_Tap a user to view details_"

def users_page_keyboard(users: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for row in users:
        name = (row["first_name"] or "No name")[:20]
        rows.append([InlineKeyboardButton(f"👤 {name}  ·  {row['total_conversions'] or 0} conv", callback_data=f"adm_user:{row['user_id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_users:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{max(1, math.ceil(total / USERS_PER_PAGE))}", callback_data="adm_noop"))
    if page < math.ceil(total / USERS_PER_PAGE) - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_users:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙  Back to Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)

async def user_detail_text(row: dict) -> str:
    uid = row["user_id"]
    name = row["first_name"] or "(no name)"
    username = row["username"] or "(none)"
    free_used = row["free_used"]
    paid = row["paid_credits"]
    unlim = row["unlimited_until"]

    if uid == OWNER_ID: plan = "👑  ADMIN / OWNER"
    elif unlim:
        try:
            until = datetime.fromisoformat(unlim)
            plan = f"♾️  ACTIVE until {_dt_short(unlim)}" if until > now_bd() else f"⏳  EXPIRED at {_dt_short(unlim)}"
        except (ValueError, TypeError): plan = f"♾️  {unlim}"
    else: plan = "🆓  Free plan"

    payments = await payment_summary(uid)
    conversions = await conversion_summary(uid)

    return "\n".join([
        "🔐  *USER ACCOUNT DETAILS*", "━━━━━━━━━━━━━━━━━━━━", "",
        f"👤  Name      :  *{name}*", f"🔗  Username  :  @{username.lstrip('@')}", f"🆔  User ID   :  `{uid}`", "", "────────────────────",
        "📅  *ACCOUNT*", f"   • Registered :  {_dt_display(row['registered_at'])}", f"   • Last seen  :  {_dt_display(row['last_seen_at'])}", "",
        "📊  *CURRENT BALANCE*", f"   • Free used today :  `{free_used}/{FREE_DAILY_LIMIT}`", f"   • Free remaining  :  `{max(0, FREE_DAILY_LIMIT - free_used)}`", f"   • Paid credits    :  `{paid}`", "",
        "📦  *CURRENT PLAN*", f"   • {plan}", "",
        "🔄  *CONVERSIONS*", f"   • Total       :  `{conversions['total']}`", f"   • ✅ Success  :  `{conversions['successful']}`", f"   • ❌ Failed   :  `{conversions['failed']}`", "",
        "💳  *PAYMENTS*", f"   • Successful  :  `{payments['count']}`", f"   • Stars paid  :  `{payments['stars']}`",
    ])

def user_detail_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐  Manage Credits", callback_data=f"adm_cred_menu:{user_id}")],
        [InlineKeyboardButton("♾️  Add Unlimited", callback_data=f"adm_unlim_add:{user_id}"), InlineKeyboardButton("🚫  Remove", callback_data=f"adm_unlim_rm:{user_id}")],
        [InlineKeyboardButton("🗂️  Conversions", callback_data=f"adm_uconv:{user_id}:0"), InlineKeyboardButton("💳  Payments", callback_data=f"adm_upay:{user_id}")],
        [InlineKeyboardButton("🔙  Back to Users", callback_data="adm_users:0")],
        [InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")],
    ])

def credits_menu_text(row: dict) -> str:
    return "\n".join(["⭐  *MANAGE CREDITS*", "━━━━━━━━━━━━━━━━━━━━", "", f"👤  User   :  *{row['first_name'] or '(no name)'}*", f"🆔  User ID:  `{row['user_id']}`", f"💰  Current paid credits:  *{row['paid_credits']}*", "", "👇  Tap a button to *ADD* or *REMOVE* credits"])

def credits_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 5", callback_data=f"adm_cradd:{user_id}:5"), InlineKeyboardButton("➕ 10", callback_data=f"adm_cradd:{user_id}:10"), InlineKeyboardButton("➕ 20", callback_data=f"adm_cradd:{user_id}:20")],
        [InlineKeyboardButton("➕ 50", callback_data=f"adm_cradd:{user_id}:50"), InlineKeyboardButton("➕ 100", callback_data=f"adm_cradd:{user_id}:100"), InlineKeyboardButton("➕ 200", callback_data=f"adm_cradd:{user_id}:200")],
        [InlineKeyboardButton("➖ 5", callback_data=f"adm_crrm:{user_id}:5"), InlineKeyboardButton("➖ 10", callback_data=f"adm_crrm:{user_id}:10"), InlineKeyboardButton("➖ 20", callback_data=f"adm_crrm:{user_id}:20")],
        [InlineKeyboardButton("➖ 50", callback_data=f"adm_crrm:{user_id}:50"), InlineKeyboardButton("➖ 100", callback_data=f"adm_crrm:{user_id}:100"), InlineKeyboardButton("➖ 200", callback_data=f"adm_crrm:{user_id}:200")],
        [InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")],
    ])

async def get_user_conversions_page(uid: int, page: int, per_page: int) -> tuple[list[dict], int]:
    total = (await db_fetchone("SELECT COUNT(*) AS c FROM conversions WHERE user_id = ?", [uid]))["c"]
    rows = await db_fetchall("SELECT id, filename, status, created_at FROM conversions WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?", [uid, per_page, page * per_page])
    return rows, total

async def get_recent_convs(page: int) -> tuple[list[dict], int]:
    total = (await db_fetchone("SELECT COUNT(*) AS c FROM conversions"))["c"]
    rows = await db_fetchall("SELECT id, user_id, filename, status, created_at FROM conversions ORDER BY created_at DESC LIMIT ? OFFSET ?", [CONVS_PER_PAGE, page * CONVS_PER_PAGE])
    return rows, total

async def get_recent_payments(page: int) -> tuple[list[dict], int]:
    total = (await db_fetchone("SELECT COUNT(*) AS c FROM payments"))["c"]
    rows = await db_fetchall("SELECT user_id, payload, stars, created_at FROM payments ORDER BY created_at DESC LIMIT ? OFFSET ?", [CONVS_PER_PAGE, page * CONVS_PER_PAGE])
    return rows, total

def conversions_page_text(page: int, total: int, user_name: str) -> str:
    return "\n".join(["🗂️  *CONVERSION HISTORY*", "━━━━━━━━━━━━━━━━━━━━", "", f"👤  User   :  *{user_name}*", f"📄  Page   :  *{page + 1}* / *{max(1, math.ceil(total / CONVS_PER_PAGE))}*", f"🔄  Total  :  *{total}*", "", "_Tap a conversion to view the image_"])

def conversions_page_keyboard(convs: list[dict], user_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for c in convs:
        icon = "✅" if c["status"] == "success" else "❌"
        fname = (c["filename"] or "unknown")[:26]
        rows.append([InlineKeyboardButton(f"{icon} {fname}", callback_data=f"adm_conv:{c['id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_uconv:{user_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{max(1, math.ceil(total / CONVS_PER_PAGE))}", callback_data="adm_noop"))
    if page < math.ceil(total / CONVS_PER_PAGE) - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_uconv:{user_id}:{page + 1}"))
    if len(nav) > 1: rows.append(nav)
    rows.append([InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")])
    return InlineKeyboardMarkup(rows)

async def payments_text(user_id: int) -> str:
    row = await get_user_row(user_id)
    name = (row["first_name"] or "(no name)") if row else "?"
    summary = await payment_summary(user_id)
    lines = ["💳  *PAYMENT HISTORY*", "━━━━━━━━━━━━━━━━━━━━", "", f"👤  User  :  *{name}*", f"🆔  ID    :  `{user_id}`", "", f"💰  Successful payments  :  *{summary['count']}*", f"⭐  Total Stars paid     :  *{summary['stars']}*", "", "────────────────────"]
    if summary["payments"]:
        lines.append("📋  *Recent payments*")
        for p in summary["payments"]:
            plan_name = {"plan_20": "20 Conversions", "plan_unlimited_30": "30 Days Unlimited"}.get(p["payload"], p["payload"])
            lines.append(f"   ⭐ {plan_name}  •  {p['stars']}  •  {_dt_short(p['created_at'])}")
    else: lines.append("_No payments yet_")
    return "\n".join(lines)

def payments_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")]])

def search_prompt_text() -> str:
    return "\n".join(["🔍  *SEARCH USER*", "━━━━━━━━━━━━━━━━━━━━", "", "Send me one of the following:", "   •  *Name*  (partial match)", "   •  *Username*  (without @)", "   •  *Telegram ID*", "", "Type /cancel to abort."])

def search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌  Cancel", callback_data="adm_home")]])

def search_results_text(query: str, count: int) -> str:
    return "\n".join(["🔍  *SEARCH RESULTS*", "━━━━━━━━━━━━━━━━━━━━", "", f"🔎  Query  :  `{query}`", f"📊  Found  :  *{count}*  user(s)", "", "_Tap a user to view details_"])

def search_results_keyboard(results: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in results[:20]:
        uid = r["user_id"]
        name = (r["first_name"] or "No name")[:20]
        total = r["total_conversions"] or 0
        rows.append([InlineKeyboardButton(f"👤 {name}  ·  {total} conv", callback_data=f"adm_user:{uid}")])
    rows.append([InlineKeyboardButton("🔍  New Search", callback_data="adm_search")])
    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)

def recent_convs_text(page: int, total: int) -> str:
    return "\n".join(["🔄  *RECENT CONVERSIONS*", "━━━━━━━━━━━━━━━━━━━━", "", f"📄  Page  :  *{page + 1}* / *{max(1, math.ceil(total / CONVS_PER_PAGE))}*", f"🔄  Total :  *{total}*", "", "_Tap a conversion to view the image_"])

def recent_convs_keyboard(convs: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for c in convs:
        icon = "✅" if c["status"] == "success" else "❌"
        fname = (c["filename"] or "unknown")[:22]
        uid = c["user_id"]
        rows.append([InlineKeyboardButton(f"{icon} {fname}  ·  {uid}", callback_data=f"adm_conv:{c['id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_recent_conv:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{max(1, math.ceil(total / CONVS_PER_PAGE))}", callback_data="adm_noop"))
    if page < math.ceil(total / CONVS_PER_PAGE) - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_recent_conv:{page + 1}"))
    if len(nav) > 1: rows.append(nav)
    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)

def recent_pays_text(page: int, total: int) -> str:
    return "\n".join(["💳  *RECENT PAYMENTS*", "━━━━━━━━━━━━━━━━━━━━", "", f"📄  Page  :  *{page + 1}* / *{max(1, math.ceil(total / CONVS_PER_PAGE))}*", f"💰  Total :  *{total}*"])

def recent_pays_keyboard(payments: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for p in payments:
        plan_name = {"plan_20": "20 credits", "plan_unlimited_30": "30d unlimited"}.get(p["payload"], p["payload"])
        rows.append([InlineKeyboardButton(f"⭐ {plan_name}  ·  {p['stars']}  ·  {p['user_id']}", callback_data=f"adm_user:{p['user_id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_recent_pay:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{max(1, math.ceil(total / CONVS_PER_PAGE))}", callback_data="adm_noop"))
    if page < math.ceil(total / CONVS_PER_PAGE) - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_recent_pay:{page + 1}"))
    if len(nav) > 1: rows.append(nav)
    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


# ===========================================================================
# COMMAND & MESSAGE HANDLERS
# ===========================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user: return
    spawn(ensure_user(update.effective_user))
    await update.message.reply_text(
        "👋 Welcome to Image → JPG Converter!\n\n"
        "📎 Send your images as documents (📎 → File).\n"
        "👇 Use the floating menu below.",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    await update.message.reply_text("📸 Send images as files, then tap Done.", reply_markup=get_main_keyboard(update.effective_user.id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user: return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner only.")
        return
    stats = await get_admin_stats()
    await update.message.reply_text(
        dashboard_text(stats),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=dashboard_keyboard()
    )
    await update.message.reply_text(
        "👑 *Admin Panel Activated*\nUse the floating buttons below.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admin_keyboard()
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not message.document or not user: return

    spawn(ensure_user(user))

    if user.id in active_processing or user.id in user_batches:
        await message.reply_text(
            "⏳ You already have unfinished conversions.\nPlease finish or cancel them first.",
            reply_markup=get_main_keyboard(user.id)
        )
        return

    document = message.document
    filename = document.file_name or "image.heic"

    if not is_supported_file(filename):
        await message.reply_text("❌ *Unsupported file type.*", parse_mode="Markdown")
        return

    if (document.file_size or 0) > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"❌ *File is too large.*\nMaximum size is {MAX_FILE_SIZE_MB}MB.", parse_mode="Markdown")
        return

    allowed, source = await can_convert(user.id)
    if not allowed:
        await message.reply_text("🚫 *Free conversions finished.* Use /plans", reply_markup=get_main_keyboard(user.id))
        return

    if user.id not in user_locks:
        user_locks[user.id] = asyncio.Lock()

    async with user_locks[user.id]:
        if user.id not in user_uploads:
            user_uploads[user.id] = []

        if len(user_uploads[user.id]) >= MAX_BATCH_SIZE:
            if not context.user_data.get("limit_warned"):
                context.user_data["limit_warned"] = True
                await message.reply_text(
                    f"⚠️ You have reached the maximum limit of {MAX_BATCH_SIZE} files per batch.\n"
                    "Please tap '✅ Done - Start Conversion' to process these files."
                )
            return

        user_uploads[user.id].append({
            "file_id": document.file_id,
            "filename": filename,
            "file_size": document.file_size,
            "source": source
        })
        count = len(user_uploads[user.id])

        text = f"📥 `{filename}`"

        msg = await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_upload_keyboard(count)
        )

        if "last_status_msg" in context.user_data:
            try: await context.bot.delete_message(chat_id=message.chat_id, message_id=context.user_data["last_status_msg"])
            except: pass

        context.user_data["last_status_msg"] = msg.message_id


async def handle_done_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not user: return

    user_id = user.id
    context.user_data.pop("limit_warned", None)

    if user_id in active_processing or user_id in user_batches:
        await message.reply_text(
            "⏳ Conversion already in progress. Please wait.",
            reply_markup=get_main_keyboard(user_id)
        )
        return

    if user_id not in user_uploads or not user_uploads[user_id]:
        await message.reply_text("❌ No files uploaded yet.", reply_markup=get_main_keyboard(user_id))
        return

    docs = user_uploads[user_id]
    total = len(docs)
    user_uploads[user_id] = []
    cancelled_users.discard(user_id)

    if "last_status_msg" in context.user_data:
        try: await context.bot.delete_message(chat_id=message.chat_id, message_id=context.user_data["last_status_msg"])
        except: pass
        del context.user_data["last_status_msg"]

    now = now_bd().isoformat()
    for i, doc in enumerate(docs, start=1):
        await db_execute(
            "INSERT INTO pending_tasks (user_id, chat_id, file_id, filename, file_size, source, status, created_at, position) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            [user_id, message.chat_id, doc["file_id"], doc["filename"], doc["file_size"], doc["source"], now, i]
        )

    status_msg = await message.reply_text(
        f"🚀 *Converting files...*\n\n📦 Total: *{total}*\n✅ Success: *0*\n⚠️ Failed/Skipped: *0*",
        parse_mode=ParseMode.MARKDOWN
    )
    kb_msg = await message.reply_text(
        f"📊 0/{total}",
        reply_markup=get_processing_keyboard(0, total)
    )

    user_batches[user_id] = {
        "total": total,
        "status_msg_id": status_msg.message_id,
        "kb_msg_id": kb_msg.message_id,
        "chat_id": message.chat_id,
        "last_kb_update": 0
    }

    spawn(process_user_queue(context.bot, user_id, message.chat_id))


async def handle_cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if not user: return
    context.user_data.pop("limit_warned", None)

    count = len(user_uploads.get(user.id, []))
    user_uploads[user.id] = []
    if "last_status_msg" in context.user_data:
        try: await context.bot.delete_message(chat_id=message.chat_id, message_id=context.user_data["last_status_msg"])
        except: pass
        del context.user_data["last_status_msg"]
    await message.reply_text(f"❌ Upload cancelled. Removed {count} files.", reply_markup=get_main_keyboard(user.id))


async def handle_cancel_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.callback_query.message
    user = update.effective_user
    if not user: return
    user_id = user.id
    chat_id = message.chat_id

    if user_id in cancelled_users:
        return

    cancelled_users.add(user_id)
    await db_execute(
        "UPDATE pending_tasks SET status = 'cancelled' WHERE user_id = ?",
        [user_id]
    )

    if user_id not in active_processing:
        await finish_batch(context.bot, user_id, chat_id, cancelled=True)
    else:
        if update.callback_query:
            await update.callback_query.answer("Stopping after current file...")
        else:
            await message.reply_text("⏳ Stopping after current file...")


async def handle_retry_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if not user: return
    user_id = user.id
    chat_id = message.chat_id

    if user_id in active_processing:
        return

    task = await db_fetchone(
        "SELECT * FROM pending_tasks WHERE user_id = ? AND status = 'awaiting_retry' ORDER BY position ASC, id ASC LIMIT 1",
        [user_id]
    )
    if not task:
        await message.reply_text("✅ No failed file to retry.", reply_markup=get_main_keyboard(user_id))
        return

    user_retries = task.get("user_retry_count", 0) or 0
    if user_retries >= MAX_USER_RETRIES:
        await message.reply_text(
            f"❌ *Maximum retries reached for* `{task['filename']}`.\n\n"
            f"Please tap *⏭️ Skip This File* to continue.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_skip_only_keyboard()
        )
        return

    await db_execute(
        "UPDATE pending_tasks SET status = 'pending', retry_count = 0, user_retry_count = user_retry_count + 1 WHERE id = ?",
        [task["id"]]
    )

    await _replace_floating_keyboard(
        context.bot, user_id, chat_id,
        (await _get_batch_counts(user_id))["success"],
        (await _get_batch_counts(user_id))["total"],
        kb_type="processing"
    )
    await update_progress_display(context.bot, user_id, chat_id, force_kb_update=True)
    spawn(process_user_queue(context.bot, user_id, chat_id))


async def handle_skip_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if not user: return
    user_id = user.id
    chat_id = message.chat_id

    if user_id in active_processing:
        return

    task = await db_fetchone(
        "SELECT * FROM pending_tasks WHERE user_id = ? AND status = 'awaiting_retry' ORDER BY position ASC, id ASC LIMIT 1",
        [user_id]
    )
    if not task:
        await message.reply_text("✅ No failed file to skip.", reply_markup=get_main_keyboard(user_id))
        return

    await db_execute("UPDATE pending_tasks SET status = 'skipped' WHERE id = ?", [task["id"]])

    skip_filename = f"{task['filename']}-skip position {task['position']}"
    spawn(record_conversion(
        user_id, skip_filename, "failed",
        task["source"], task["file_size"], None, task["file_id"]
    ))

    await _replace_floating_keyboard(
        context.bot, user_id, chat_id,
        (await _get_batch_counts(user_id))["success"],
        (await _get_batch_counts(user_id))["total"],
        kb_type="processing"
    )
    await update_progress_display(context.bot, user_id, chat_id, force_kb_update=True)
    spawn(process_user_queue(context.bot, user_id, chat_id))


# ===========================================================================
# ADMIN FLOATING KEYBOARD HANDLERS
# ===========================================================================
async def admin_all_users_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user: return
    users = await db_fetchall("SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ?", [USERS_PER_PAGE])
    total = await db_fetchone("SELECT COUNT(*) AS c FROM users")
    await update.message.reply_text(users_page_text(0, total["c"]), parse_mode=ParseMode.MARKDOWN, reply_markup=users_page_keyboard(users, 0, total["c"]))

async def admin_search_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    context.user_data["admin_search"] = True
    await update.message.reply_text("🔍 *SEARCH USER*\n\nSend me a Name, Username, or Telegram ID.\n\nType /cancel to abort.", parse_mode=ParseMode.MARKDOWN)

async def admin_back_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user: return
    await update.message.reply_text("🏠 Back to Home Menu.", reply_markup=get_main_keyboard(update.effective_user.id))

async def admin_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user: return
    if update.effective_user.id != OWNER_ID: return
    if not context.user_data.get("admin_search"): return

    query_text = (update.message.text or "").strip()
    context.user_data.pop("admin_search", None)

    if not query_text:
        await update.message.reply_text("❌ Empty search.", reply_markup=get_admin_keyboard())
        return

    results = await search_users(query_text)
    if not results:
        await update.message.reply_text(
            f"🔍 No users found matching: `{query_text}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_keyboard()
        )
        return

    await update.message.reply_text(
        search_results_text(query_text, len(results)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=search_results_keyboard(results)
    )


# ===========================================================================
# ADMIN INLINE CALLBACKS
# ===========================================================================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or query.from_user.id != OWNER_ID: return
    await query.answer()

    data = query.data or ""
    parts = data.split(":")

    if data == "adm_noop": return
    if data == "adm_home":
        stats = await get_admin_stats()
        await query.edit_message_text(dashboard_text(stats), parse_mode=ParseMode.MARKDOWN, reply_markup=dashboard_keyboard())
        return
    if parts[0] == "adm_users":
        page = int(parts[1]) if len(parts) > 1 else 0
        users = await db_fetchall("SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ? OFFSET ?", [USERS_PER_PAGE, page * USERS_PER_PAGE])
        total = (await db_fetchone("SELECT COUNT(*) AS c FROM users"))["c"]
        await query.edit_message_text(users_page_text(page, total), parse_mode=ParseMode.MARKDOWN, reply_markup=users_page_keyboard(users, page, total))
        return
    if parts[0] == "adm_user":
        uid = int(parts[1])
        row = await get_user_row(uid)
        if row is None:
            await query.edit_message_text("🔎 User not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Dashboard", callback_data="adm_home")]]))
            return
        text = await user_detail_text(row)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=user_detail_keyboard(uid))
        return
    if parts[0] == "adm_cred_menu":
        uid = int(parts[1])
        row = await get_user_row(uid)
        if row is None: return
        await query.edit_message_text(credits_menu_text(row), parse_mode=ParseMode.MARKDOWN, reply_markup=credits_menu_keyboard(uid))
        return
    if parts[0] in {"adm_cradd", "adm_crrm"}:
        uid, amount = int(parts[1]), int(parts[2])
        if parts[0] == "adm_crrm": amount = -abs(amount)
        await add_paid_credits(uid, amount)
        updated = await get_user_row(uid)
        if updated and updated["paid_credits"] < 0:
            await db_execute("UPDATE users SET paid_credits = 0 WHERE user_id = ?", [uid])
            updated = await get_user_row(uid)
        await query.answer(f"✅ {amount:+d} credits applied.")
        await query.edit_message_text(credits_menu_text(updated), parse_mode=ParseMode.MARKDOWN, reply_markup=credits_menu_keyboard(uid))
        return
    if parts[0] == "adm_unlim_add":
        uid = int(parts[1])
        await add_unlimited_days(uid, UNLIMITED_DAYS)
        await query.answer("✅ Unlimited added.")
        row = await get_user_row(uid)
        if row: await query.edit_message_text(await user_detail_text(row), parse_mode=ParseMode.MARKDOWN, reply_markup=user_detail_keyboard(uid))
        return
    if parts[0] == "adm_unlim_rm":
        uid = int(parts[1])
        await remove_unlimited(uid)
        await query.answer("✅ Unlimited removed.")
        row = await get_user_row(uid)
        if row: await query.edit_message_text(await user_detail_text(row), parse_mode=ParseMode.MARKDOWN, reply_markup=user_detail_keyboard(uid))
        return
    if parts[0] == "adm_uconv":
        uid, page = int(parts[1]), int(parts[2])
        row = await get_user_row(uid)
        convs, total = await get_user_conversions_page(uid, page, CONVS_PER_PAGE)
        await query.edit_message_text(conversions_page_text(page, total, row["first_name"] or "(no name)"), parse_mode=ParseMode.MARKDOWN, reply_markup=conversions_page_keyboard(convs, uid, page, total))
        return
    if parts[0] == "adm_upay":
        uid = int(parts[1])
        await query.edit_message_text(await payments_text(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=payments_keyboard(uid))
        return
    if parts[0] == "adm_conv":
        conv_id = int(parts[1])
        conv = await db_fetchone("SELECT * FROM conversions WHERE id = ?", [conv_id])
        if conv and conv.get("converted_file_id"):
            caption = f"📄 {conv['filename']}\n🕐 {_dt_display(conv['created_at'])}"
            await query.message.reply_document(document=conv["converted_file_id"], caption=caption)
        return
    if data == "adm_search":
        context.user_data["admin_search"] = True
        await query.edit_message_text(search_prompt_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=search_prompt_keyboard())
        return
    if parts[0] == "adm_recent_conv":
        page = int(parts[1])
        convs, total = await get_recent_convs(page)
        await query.edit_message_text(recent_convs_text(page, total), parse_mode=ParseMode.MARKDOWN, reply_markup=recent_convs_keyboard(convs, page, total))
        return
    if parts[0] == "adm_recent_pay":
        page = int(parts[1])
        pays, total = await get_recent_payments(page)
        await query.edit_message_text(recent_pays_text(page, total), parse_mode=ParseMode.MARKDOWN, reply_markup=recent_pays_keyboard(pays, page, total))
        return


# ===========================================================================
# STARTUP — Resume from DB state (server crash safe)
# ===========================================================================
async def on_startup(application: Application) -> None:
    await init_database()

    # Mark previously processing tasks as crashed
    await db_execute("UPDATE pending_tasks SET status = 'pending', crash_count = crash_count + 1 WHERE status = 'processing'")

    users_with_tasks = await db_fetchall(
        "SELECT user_id, chat_id FROM pending_tasks "
        "WHERE status IN ('pending','processing','awaiting_retry') "
        "GROUP BY user_id, chat_id"
    )

    logger.info(f"Resuming processing for {len(users_with_tasks)} users (server restart recovery)")

    for row in users_with_tasks:
        spawn(resume_user_processing(application.bot, row["user_id"], row["chat_id"]))


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_WITH_NEW_BOT_TOKEN": raise RuntimeError("BOT_TOKEN is not set.")

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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("cancel", handle_cancel_conversion))

    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm_"))

    application.add_handler(MessageHandler(filters.Regex(r"^👑 Admin Panel$"), admin_command))
    application.add_handler(MessageHandler(filters.Regex(r"^👥 All Users$"), admin_all_users_btn))
    application.add_handler(MessageHandler(filters.Regex(r"^🔍 Search User$"), admin_search_btn))
    application.add_handler(MessageHandler(filters.Regex(r"^🏠 Back to Home$"), admin_back_home))

    application.add_handler(MessageHandler(filters.Regex(r"^✅ Done - Start Conversion"), handle_done_keyboard))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel Upload$"), handle_cancel_upload))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel Conversion$"), handle_cancel_conversion))
    application.add_handler(MessageHandler(filters.Regex(r"^🔁 Retry Failed File$"), handle_retry_file))
    application.add_handler(MessageHandler(filters.Regex(r"^⏭️ Skip This File$"), handle_skip_file))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_search_text))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
