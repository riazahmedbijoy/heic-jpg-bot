"""
Image → JPG Telegram Bot with Turso (Complete Professional Edition)
===================================================================
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
JPEG_QUALITY = 95
LOG_LEVEL = logging.INFO
SUPPORTED_EXTENSIONS = {
    ".heic", ".heif", ".jpg", ".jpeg", ".jpe",
    ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

MAX_DIMENSION = 4096
USERS_PER_PAGE = 8
CONVS_PER_PAGE = 8

# 🚀 Concurrency
MAX_WORKERS = 2  
MAX_RETRIES = 3
conversion_queue = asyncio.Queue()

# 🚀 State Tracking
user_uploads: dict[int, list[dict]] = {}
user_batches: dict[int, dict] = {}
cancelled_users: set[int] = set()
user_locks: dict[int, asyncio.Lock] = {}

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

def get_processing_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
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
    await db_execute("CREATE TABLE IF NOT EXISTS pending_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, file_id TEXT, filename TEXT, file_size INTEGER, source TEXT, retry_count INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', created_at TEXT, position INTEGER DEFAULT 1)")


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
# PAYMENT & ADMIN HELPERS
# ===========================================================================
async def entitlement_text(user_id: int) -> str:
    if user_id == OWNER_ID: return "👑 Owner: Unlimited"
    row = await get_user_row(user_id)
    if not row: return "🆓 Free today: 5"
    if unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        return f"♾️ Unlimited until {until.strftime('%d-%m-%Y %I:%M %p')}"
    free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
    return f"🆓 Free today: {free_left}/{FREE_DAILY_LIMIT}\n⭐ Paid credits: {row['paid_credits']}"

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

# ===========================================================================
# 🚀 QUEUE WORKER LOGIC
# ===========================================================================
async def queue_worker(application: Application, worker_id: int):
    bot = application.bot
    while True:
        try:
            task_id = await conversion_queue.get()
            task = await db_fetchone("SELECT * FROM pending_tasks WHERE id = ? AND status IN ('pending', 'processing')", [task_id])
            if not task or task["status"] == "cancelled":
                conversion_queue.task_done()
                continue

            user_id = task["user_id"]
            if user_id in cancelled_users:
                await db_execute("DELETE FROM pending_tasks WHERE id = ?", [task_id])
                conversion_queue.task_done()
                continue

            await db_execute("UPDATE pending_tasks SET status = 'processing' WHERE id = ?", [task_id])

            file_id, filename, chat_id = task["file_id"], task["filename"], task["chat_id"]
            source, size, retry_count, position = task["source"], task["file_size"], task["retry_count"], task.get("position", 1)

            ext = Path(filename).suffix.lower()
            unique_id = f"task_{task_id}"
            input_file, output_file = DOWNLOAD_DIR / f"{unique_id}{ext}", OUTPUT_DIR / f"{unique_id}.jpg"
            out_name = Path(filename).with_suffix(".jpg").name

            success, conv_id, error_msg = False, None, ""

            try:
                if user_id in cancelled_users: raise Exception("Cancelled by user")
                tg_file = await bot.get_file(file_id)
                await tg_file.download_to_drive(input_file)

                if user_id in cancelled_users: raise Exception("Cancelled by user")
                await asyncio.to_thread(convert_to_jpg, input_file, output_file, JPEG_QUALITY)

                if user_id in cancelled_users: raise Exception("Cancelled by user")
                with output_file.open("rb") as jpg_file:
                    sent = await bot.send_document(
                        chat_id=chat_id, document=jpg_file, filename=out_name,
                        caption=f"✅ Conversion complete! Position {position}\n📄 `{out_name}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                if sent.document: conv_id = sent.document.file_id
                success = True

            except telegram.error.RetryAfter as e:
                await asyncio.sleep(e.retry_after)
                error_msg = "Flood limit"
            except Exception as e:
                error_msg = str(e)

            # Handle Results
            if success:
                await db_execute("DELETE FROM pending_tasks WHERE id = ?", [task_id])
                spawn(consume_conversion(user_id, source))
                spawn(record_conversion(user_id, filename, "success", source, size, conv_id, file_id))
                
                batch = user_batches.get(user_id)
                if batch and batch.get("active"):
                    batch["completed"] += 1
                    text = f"🔄 *Conversion in Progress...*\n\n✅ *Completed: {batch['completed']} / {batch['total']}*"
                    
                    if batch["completed"] + batch["failed"] == batch["total"]:
                        text = f"🎉 *All {batch['total']} conversions completed!*"
                        if batch["failed"] > 0: text += f"\n⚠️ *Failed: {batch['failed']}*"
                        batch["active"] = False
                        del user_batches[user_id]
                        
                        try:
                            await bot.delete_message(chat_id=batch["chat_id"], message_id=batch["status_msg_id"])
                            await bot.send_message(chat_id=batch["chat_id"], text=text + "\n\n👇 Use the menu below.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(user_id))
                        except: pass
                        
                        conversion_queue.task_done()
                        continue

                    try:
                        await bot.edit_message_text(chat_id=batch["chat_id"], message_id=batch["status_msg_id"], text=text, parse_mode=ParseMode.MARKDOWN)
                    except: pass
            else:
                if error_msg == "Cancelled by user":
                    await db_execute("DELETE FROM pending_tasks WHERE id = ?", [task_id])
                else:
                    retry_count += 1
                    if retry_count <= MAX_RETRIES:
                        await db_execute("UPDATE pending_tasks SET retry_count = ?, status = 'pending' WHERE id = ?", [retry_count, task_id])
                        await asyncio.sleep(2)
                        await conversion_queue.put(task_id)
                    else:
                        await db_execute("UPDATE pending_tasks SET status = 'failed' WHERE id = ?", [task_id])
                        spawn(record_conversion(user_id, filename, "failed", source, size, None, file_id))
                        
                        batch = user_batches.get(user_id)
                        if batch and batch.get("active"):
                            batch["failed"] += 1
                            text = f"🔄 *Conversion in Progress...*\n\n✅ *Completed: {batch['completed']} / {batch['total']}*"
                            if batch["completed"] + batch["failed"] == batch["total"]:
                                text = f"🎉 *All {batch['total']} conversions completed!*"
                                if batch["failed"] > 0: text += f"\n⚠️ *Failed: {batch['failed']}*"
                                batch["active"] = False
                                del user_batches[user_id]
                                try:
                                    await bot.delete_message(chat_id=batch["chat_id"], message_id=batch["status_msg_id"])
                                    await bot.send_message(chat_id=batch["chat_id"], text=text + "\n\n👇 Use the menu below.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(user_id))
                                except: pass
                                conversion_queue.task_done()
                                continue
                            try:
                                await bot.edit_message_text(chat_id=batch["chat_id"], message_id=batch["status_msg_id"], text=text, parse_mode=ParseMode.MARKDOWN)
                            except: pass

            safe_unlink(input_file)
            safe_unlink(output_file)
            gc.collect() 
            await asyncio.sleep(1)
            conversion_queue.task_done()

        except asyncio.CancelledError: break
        except Exception: await asyncio.sleep(5)


# ===========================================================================
# IMAGE CONVERSION LOGIC
# ===========================================================================
class ConversionError(Exception): pass

def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS

def safe_unlink(path: Path) -> None:
    try: path.unlink(missing_ok=True)
    except: pass

def convert_to_jpg(input_file: Path, output_file: Path, quality: int = JPEG_QUALITY) -> None:
    try:
        if input_file.suffix.lower() in {".heic", ".heif"}:
            heif = pillow_heif.open_heif(str(input_file), convert_hdr_to_8bit=True)
            w, h = heif.size
            if w > MAX_DIMENSION or h > MAX_DIMENSION:
                heif = heif.scale(min(MAX_DIMENSION / w, MAX_DIMENSION / h))
            image = heif.to_pillow()
        else:
            image = Image.open(input_file)
            try: image.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
            except: pass

        if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

        if image.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", image.size, (255, 255, 255))
            bg.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image = bg
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.save(output_file, format="JPEG", quality=quality, optimize=True)
    finally:
        if 'image' in locals() and image: image.close()


# ===========================================================================
# UI BUILDERS
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
    document = message.document
    filename = document.file_name or "image.heic"
    
    if not is_supported_file(filename):
        await message.reply_text("❌ *Unsupported file type.*", parse_mode="Markdown")
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
            
        user_uploads[user.id].append({
            "file_id": document.file_id,
            "filename": filename,
            "file_size": document.file_size,
            "source": source
        })
        count = len(user_uploads[user.id])

        if "last_status_msg" in context.user_data:
            try: await context.bot.delete_message(chat_id=message.chat_id, message_id=context.user_data["last_status_msg"])
            except: pass

        msg = await message.reply_text(
            f"📥 `{filename}`", 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_upload_keyboard(count)
        )
        context.user_data["last_status_msg"] = msg.message_id


# ===========================================================================
# FLOATING KEYBOARD HANDLERS
# ===========================================================================
async def handle_done_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not user: return
    
    user_id = user.id
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
    
    status_msg = await message.reply_text(
        f"🚀 *Starting conversion...*\n\n📦 Total files: *{total}*\n✅ Completed: *0 / {total}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_processing_keyboard()
    )
    
    user_batches[user_id] = {
        "total": total, "completed": 0, "failed": 0,
        "status_msg_id": status_msg.message_id, "chat_id": message.chat_id, "active": True
    }
    
    now = now_bd().isoformat()
    for i, doc in enumerate(docs, start=1):
        task_row = await db_fetchone(
            "INSERT INTO pending_tasks (user_id, chat_id, file_id, filename, file_size, source, status, created_at, position) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?) RETURNING id",
            [user_id, message.chat_id, doc["file_id"], doc["filename"], doc["file_size"], doc["source"], now, i]
        )
        if task_row:
            await conversion_queue.put(task_row["id"])

async def handle_cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if not user: return
    
    count = len(user_uploads.get(user.id, []))
    user_uploads[user.id] = []
    
    if "last_status_msg" in context.user_data:
        try: await context.bot.delete_message(chat_id=message.chat_id, message_id=context.user_data["last_status_msg"])
        except: pass
        del context.user_data["last_status_msg"]
        
    await message.reply_text(f"❌ Upload cancelled. Removed {count} files.", reply_markup=get_main_keyboard(user.id))

async def handle_cancel_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.message, update.effective_user
    if not user: return
    
    user_id = user.id
    cancelled_users.add(user_id)
    
    batch = user_batches.pop(user_id, None)
    if batch:
        try: await context.bot.edit_message_text(chat_id=batch["chat_id"], message_id=batch["status_msg_id"], text="❌ *Conversion cancelled by user.*", parse_mode=ParseMode.MARKDOWN)
        except: pass
        
    await db_execute("UPDATE pending_tasks SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", [user_id])
    await message.reply_text("✅ All processing stopped.", reply_markup=get_main_keyboard(user_id))


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
# STARTUP
# ===========================================================================
async def on_startup(application: Application) -> None:
    await init_database()
    await db_execute("UPDATE pending_tasks SET status = 'pending' WHERE status = 'processing'")
    pending_tasks = await db_fetchall("SELECT id FROM pending_tasks WHERE status = 'pending' ORDER BY id ASC")
    if pending_tasks:
        for row in pending_tasks: await conversion_queue.put(row["id"])
    
    for i in range(1, MAX_WORKERS + 1):
        asyncio.create_task(queue_worker(application, i))


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
        .post_init(on_startup) # 🚀 FIXED: Using post_init
        .build()
    )

    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    
    # Callback Handlers
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm_"))
    
    # Message Handlers (Keyboard & Text)
    application.add_handler(MessageHandler(filters.Regex(r"^👑 Admin Panel$"), admin_command))
    application.add_handler(MessageHandler(filters.Regex(r"^👥 All Users$"), admin_all_users_btn))
    application.add_handler(MessageHandler(filters.Regex(r"^🔍 Search User$"), admin_search_btn))
    application.add_handler(MessageHandler(filters.Regex(r"^🏠 Back to Home$"), admin_back_home))
    
    application.add_handler(MessageHandler(filters.Regex(r"^✅ Done - Start Conversion"), handle_done_keyboard))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel Upload$"), handle_cancel_upload))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel Conversion$"), handle_cancel_conversion))
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
