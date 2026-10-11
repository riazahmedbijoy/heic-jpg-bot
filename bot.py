"""
Image → JPG Telegram Bot with Turso (persistent + concurrent)
==============================================================
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
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
    ".heic", ".heif",
    ".jpg", ".jpeg", ".jpe",
    ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif",
}

# সর্বোচ্চ ডাইমেনশন - এর চেয়ে বড় হলে রিসাইজ হবে
MAX_DIMENSION = 4096
# এটি শুধু ফাইনাল সেফটি নেট - যদি সব পদ্ধতি ব্যর্থ হয়
ABSOLUTE_MAX_PIXELS = 200_000_000  # 200 Megapixels

USERS_PER_PAGE = 8
CONVS_PER_PAGE = 8

# ===========================================================================
# INITIALIZATION
# ===========================================================================
pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

conversion_lock = asyncio.Lock()
active_tasks: dict[int, asyncio.Task] = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)

# Check for pyvips availability
PYVIPS_AVAILABLE = False
try:
    import pyvips  # type: ignore
    PYVIPS_AVAILABLE = True
    logger.info("pyvips (libvips) is AVAILABLE - large image support enabled")
except ImportError:
    logger.warning("pyvips NOT available - large HEIC files may fail. Install libvips42 + pyvips for best results.")


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
        _db_client = libsql_client.create_client(
            url=http_url,
            auth_token=TURSO_AUTH_TOKEN,
        )
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
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            free_date TEXT NOT NULL,
            free_used INTEGER NOT NULL DEFAULT 0,
            paid_credits INTEGER NOT NULL DEFAULT 0,
            unlimited_until TEXT,
            registered_at TEXT,
            last_seen_at TEXT,
            total_conversions INTEGER NOT NULL DEFAULT 0,
            successful_conversions INTEGER NOT NULL DEFAULT 0,
            failed_conversions INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            stars INTEGER NOT NULL,
            charge_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT,
            status TEXT NOT NULL,
            quota_source TEXT,
            file_size INTEGER,
            created_at TEXT NOT NULL,
            converted_file_id TEXT,
            original_file_id TEXT
        )
        """
    )
    await _safe_alter("ALTER TABLE conversions ADD COLUMN converted_file_id TEXT")
    await _safe_alter("ALTER TABLE conversions ADD COLUMN original_file_id TEXT")
    logger.info("Database schema ready.")


# ===========================================================================
# TIME HELPERS
# ===========================================================================
def today_bd() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def now_bd() -> datetime:
    return datetime.now(TIMEZONE)


def _dt_display(value) -> str:
    if not value:
        return "Unknown"
    try:
        return datetime.fromisoformat(value).strftime("%d-%m-%Y %I:%M:%S %p")
    except (ValueError, TypeError):
        return str(value)


def _dt_short(value) -> str:
    if not value:
        return "?"
    try:
        return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M")
    except (ValueError, TypeError):
        return str(value)


def spawn(coro) -> None:
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        logger.warning("spawn() outside event loop")


# ===========================================================================
# USER MANAGEMENT
# ===========================================================================
async def ensure_user(user) -> dict:
    user_id = user.id
    today = today_bd()
    now = now_bd().isoformat()

    row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

    if row is None:
        await db_execute(
            """INSERT INTO users
               (user_id, first_name, username, free_date, free_used,
                registered_at, last_seen_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            [user_id, user.first_name or "", user.username or "", today, now, now],
        )
    else:
        spawn(db_execute(
            """UPDATE users SET first_name = ?, username = ?, last_seen_at = ?
               WHERE user_id = ?""",
            [user.first_name or "", user.username or "", now, user_id],
        ))
        if row["free_date"] != today:
            await db_execute(
                "UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?",
                [today, user_id],
            )

    return await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])


async def get_user_row(user_id: int) -> dict | None:
    row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])
    if row and row["free_date"] != today_bd():
        await db_execute(
            "UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?",
            [today_bd(), user_id],
        )
        row = await db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])
    return row


def unlimited_active(row: dict | None) -> bool:
    if not row or not row.get("unlimited_until"):
        return False
    try:
        return datetime.fromisoformat(row["unlimited_until"]) > now_bd()
    except (ValueError, TypeError):
        return False


async def entitlement_text(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "👑 Owner: Unlimited"
    row = await get_user_row(user_id)
    if not row:
        return "🆓 Free today: 5"
    if unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        return f"♾️ Unlimited until {until.strftime('%d-%m-%Y %I:%M %p')}"
    free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
    return f"🆓 Free today: {free_left}/{FREE_DAILY_LIMIT}\n⭐ Paid credits: {row['paid_credits']}"


async def can_convert(user_id: int) -> tuple[bool, str]:
    if user_id == OWNER_ID:
        return True, "owner"
    row = await get_user_row(user_id)
    if row is None:
        return True, "free"
    if unlimited_active(row):
        return True, "unlimited"
    if (FREE_DAILY_LIMIT - row["free_used"]) > 0:
        return True, "free"
    if row["paid_credits"] > 0:
        return True, "paid"
    return False, "none"


async def consume_conversion(user_id: int, source: str) -> None:
    if user_id == OWNER_ID:
        return
    if source == "free":
        await db_execute(
            "UPDATE users SET free_used = free_used + 1 WHERE user_id = ?",
            [user_id],
        )
    elif source == "paid":
        await db_execute(
            """UPDATE users SET paid_credits = paid_credits - 1
               WHERE user_id = ? AND paid_credits > 0""",
            [user_id],
        )


async def add_paid_credits(user_id: int, amount: int) -> None:
    await db_execute(
        "UPDATE users SET paid_credits = paid_credits + ? WHERE user_id = ?",
        [amount, user_id],
    )


async def add_unlimited_days(user_id: int, days: int = UNLIMITED_DAYS) -> None:
    current = await get_user_row(user_id)
    current_until = None
    if current and current.get("unlimited_until"):
        try:
            current_until = datetime.fromisoformat(current["unlimited_until"])
        except ValueError:
            current_until = None
    base = now_bd()
    if current_until and current_until > base:
        base = current_until
    until = base + timedelta(days=days)
    await db_execute(
        "UPDATE users SET unlimited_until = ? WHERE user_id = ?",
        [until.isoformat(), user_id],
    )


async def remove_unlimited(user_id: int) -> None:
    await db_execute(
        "UPDATE users SET unlimited_until = NULL WHERE user_id = ?",
        [user_id],
    )


async def record_payment(user_id: int, payload: str, stars: int, charge_id: str) -> bool:
    try:
        await db_execute(
            """INSERT INTO payments
               (user_id, payload, stars, charge_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [user_id, payload, stars, charge_id, now_bd().isoformat()],
        )
        return True
    except Exception:
        logger.warning("Duplicate or failed payment: %s", charge_id)
        return False


async def record_conversion(
    user_id: int,
    filename: str,
    status: str,
    quota_source: str | None,
    file_size: int | None,
    converted_file_id: str | None = None,
    original_file_id: str | None = None,
) -> None:
    created_at = now_bd().isoformat()
    await db_execute(
        """INSERT INTO conversions
           (user_id, filename, status, quota_source, file_size, created_at,
            converted_file_id, original_file_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [user_id, filename, status, quota_source, file_size, created_at,
         converted_file_id, original_file_id],
    )
    if status == "success":
        await db_execute(
            """UPDATE users
               SET total_conversions = total_conversions + 1,
                   successful_conversions = successful_conversions + 1,
                   last_seen_at = ?
               WHERE user_id = ?""",
            [created_at, user_id],
        )
    elif status == "failed":
        await db_execute(
            """UPDATE users
               SET total_conversions = total_conversions + 1,
                   failed_conversions = failed_conversions + 1,
                   last_seen_at = ?
               WHERE user_id = ?""",
            [created_at, user_id],
        )


async def search_users(query: str, limit: int = 30) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    like = f"%{q}%"
    return await db_fetchall(
        """SELECT * FROM users
           WHERE CAST(user_id AS TEXT) LIKE ?
              OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?)
              OR LOWER(COALESCE(username, '')) LIKE LOWER(?)
           ORDER BY last_seen_at DESC
           LIMIT ?""",
        [like, like, like, limit],
    )


async def get_all_users() -> list[dict]:
    return await db_fetchall("SELECT * FROM users ORDER BY last_seen_at DESC")


async def count_users() -> int:
    row = await db_fetchone("SELECT COUNT(*) AS c FROM users")
    return row["c"] if row else 0


# ===========================================================================
# ADMIN STATS
# ===========================================================================
async def get_admin_stats() -> dict:
    now = now_bd().isoformat()
    today = today_bd()

    total_users = (await db_fetchone("SELECT COUNT(*) AS c FROM users"))["c"]
    active_today = (await db_fetchone(
        """SELECT COUNT(DISTINCT user_id) AS c FROM conversions
           WHERE status = 'success' AND substr(created_at, 1, 10) = ?""",
        [today],
    ))["c"]
    total_attempts = (await db_fetchone("SELECT COUNT(*) AS c FROM conversions"))["c"]
    total_success = (await db_fetchone(
        "SELECT COUNT(*) AS c FROM conversions WHERE status = 'success'"
    ))["c"]
    total_failed = (await db_fetchone(
        "SELECT COUNT(*) AS c FROM conversions WHERE status = 'failed'"
    ))["c"]
    active_unlimited = (await db_fetchone(
        """SELECT COUNT(*) AS c FROM users
           WHERE user_id != ? AND unlimited_until IS NOT NULL
           AND unlimited_until > ?""",
        [OWNER_ID, now],
    ))["c"]
    expired_unlimited = (await db_fetchone(
        """SELECT COUNT(*) AS c FROM users
           WHERE user_id != ? AND unlimited_until IS NOT NULL
           AND unlimited_until <= ?""",
        [OWNER_ID, now],
    ))["c"]
    total_paid_credits = (await db_fetchone(
        "SELECT COALESCE(SUM(paid_credits), 0) AS c FROM users"
    ))["c"]
    payment_count = (await db_fetchone("SELECT COUNT(*) AS c FROM payments"))["c"]
    total_stars = (await db_fetchone(
        "SELECT COALESCE(SUM(stars), 0) AS c FROM payments"
    ))["c"]

    return {
        "total_users": total_users, "active_today": active_today,
        "total_attempts": total_attempts, "total_success": total_success,
        "total_failed": total_failed, "active_unlimited": active_unlimited,
        "expired_unlimited": expired_unlimited,
        "total_paid_credits": total_paid_credits,
        "payment_count": payment_count, "total_stars": total_stars,
    }


async def payment_summary(user_id: int) -> dict:
    row = await db_fetchone(
        """SELECT COUNT(*) AS count, COALESCE(SUM(stars), 0) AS stars
           FROM payments WHERE user_id = ?""",
        [user_id],
    )
    payments = await db_fetchall(
        """SELECT payload, stars, charge_id, created_at FROM payments
           WHERE user_id = ? ORDER BY created_at DESC LIMIT 10""",
        [user_id],
    )
    return {"count": row["count"], "stars": row["stars"], "payments": payments}


async def conversion_summary(user_id: int) -> dict:
    row = await db_fetchone(
        """SELECT
             COUNT(*) AS total,
             COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS successful,
             COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
           FROM conversions WHERE user_id = ?""",
        [user_id],
    )
    recent = await db_fetchall(
        """SELECT id, filename, status, quota_source, file_size, created_at,
                  converted_file_id
           FROM conversions WHERE user_id = ?
           ORDER BY created_at DESC LIMIT 20""",
        [user_id],
    )
    return {"total": row["total"], "successful": row["successful"],
            "failed": row["failed"], "recent": recent}


async def get_user_conversions_page(user_id: int, page: int, per_page: int):
    offset = page * per_page
    rows = await db_fetchall(
        """SELECT id, filename, status, quota_source, file_size, created_at,
                  converted_file_id
           FROM conversions WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        [user_id, per_page, offset],
    )
    total = await db_fetchone(
        "SELECT COUNT(*) AS c FROM conversions WHERE user_id = ?",
        [user_id],
    )
    return rows, (total["c"] if total else 0)


# ===========================================================================
# ADMIN UI — TEXT BUILDERS
# ===========================================================================
def _sep(char: str = "━", n: int = 20) -> str:
    return char * n


def dashboard_text(stats: dict) -> str:
    return "\n".join([
        "🔐  *ADMIN DASHBOARD*",
        _sep(),
        "",
        f"👥 Registered users      :  *{stats['total_users']}*",
        f"📈 Active today          :  *{stats['active_today']}*",
        "",
        "🔄  *CONVERSIONS*",
        f"   • Total attempts   :  `{stats['total_attempts']}`",
        f"   • ✅ Successful     :  `{stats['total_success']}`",
        f"   • ❌ Failed          :  `{stats['total_failed']}`",
        "",
        "📦  *PLANS*",
        f"   • Active unlimited :  `{stats['active_unlimited']}`",
        f"   • Expired unlimited:  `{stats['expired_unlimited']}`",
        f"   • Paid credits sum :  `{stats['total_paid_credits']}`",
        "",
        "💳  *PAYMENTS*",
        f"   • Successful       :  `{stats['payment_count']}`",
        f"   • Total stars      :  `{stats['total_stars']}`",
        "",
        _sep("─"),
        "_Tap a button below to explore_",
    ])


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥  All Users", callback_data="adm_users:0")],
        [InlineKeyboardButton("🔍  Search User", callback_data="adm_search")],
        [
            InlineKeyboardButton("🔄  Recent Conv.", callback_data="adm_recent_conv:0"),
            InlineKeyboardButton("💳  Recent Pay.", callback_data="adm_recent_pay:0"),
        ],
        [InlineKeyboardButton("🔄  Refresh Stats", callback_data="adm_home")],
    ])


def users_page_text(page: int, total: int) -> str:
    total_pages = max(1, math.ceil(total / USERS_PER_PAGE))
    return "\n".join([
        "👥  *ALL REGISTERED USERS*",
        _sep(),
        "",
        f"📄  Page  *{page + 1}*  /  *{total_pages}*",
        f"👤  Total  *{total}*  users",
        "",
        "_Tap a user to view details_",
    ])


def users_page_keyboard(users: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for row in users:
        uid = row["user_id"]
        name = (row["first_name"] or "No name")[:20]
        total_conv = row["total_conversions"] or 0
        label = f"👤 {name}  ·  {total_conv} conv"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm_user:{uid}")])

    total_pages = max(1, math.ceil(total / USERS_PER_PAGE))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_users:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_users:{page + 1}"))
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

    if uid == OWNER_ID:
        plan = "👑  ADMIN / OWNER"
    elif unlim:
        try:
            until = datetime.fromisoformat(unlim)
            if until > now_bd():
                plan = f"♾️  ACTIVE until {_dt_short(unlim)}"
            else:
                plan = f"⏳  EXPIRED at {_dt_short(unlim)}"
        except (ValueError, TypeError):
            plan = f"♾️  {unlim}"
    else:
        plan = "🆓  Free plan"

    payments = await payment_summary(uid)
    conversions = await conversion_summary(uid)

    return "\n".join([
        "🔐  *USER ACCOUNT DETAILS*",
        _sep(),
        "",
        f"👤  Name      :  *{name}*",
        f"🔗  Username  :  @{username.lstrip('@')}",
        f"🆔  User ID   :  `{uid}`",
        "",
        _sep("─"),
        "📅  *ACCOUNT*",
        f"   • Registered :  {_dt_display(row['registered_at'])}",
        f"   • Last seen  :  {_dt_display(row['last_seen_at'])}",
        "",
        "📊  *CURRENT BALANCE*",
        f"   • Free used today :  `{free_used}/{FREE_DAILY_LIMIT}`",
        f"   • Free remaining  :  `{max(0, FREE_DAILY_LIMIT - free_used)}`",
        f"   • Paid credits    :  `{paid}`",
        "",
        "📦  *CURRENT PLAN*",
        f"   • {plan}",
        "",
        "🔄  *CONVERSIONS*",
        f"   • Total       :  `{conversions['total']}`",
        f"   • ✅ Success  :  `{conversions['successful']}`",
        f"   • ❌ Failed   :  `{conversions['failed']}`",
        "",
        "💳  *PAYMENTS*",
        f"   • Successful  :  `{payments['count']}`",
        f"   • Stars paid  :  `{payments['stars']}`",
    ])


def user_detail_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐  Manage Credits", callback_data=f"adm_cred_menu:{user_id}")],
        [
            InlineKeyboardButton("♾️  Add Unlimited", callback_data=f"adm_unlim_add:{user_id}"),
            InlineKeyboardButton("🚫  Remove", callback_data=f"adm_unlim_rm:{user_id}"),
        ],
        [
            InlineKeyboardButton("🗂️  Conversions", callback_data=f"adm_uconv:{user_id}:0"),
            InlineKeyboardButton("💳  Payments", callback_data=f"adm_upay:{user_id}"),
        ],
        [InlineKeyboardButton("🔙  Back to Users", callback_data="adm_users:0")],
        [InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")],
    ])


def credits_menu_text(row: dict) -> str:
    return "\n".join([
        "⭐  *MANAGE CREDITS*",
        _sep(),
        "",
        f"👤  User   :  *{row['first_name'] or '(no name)'}*",
        f"🆔  User ID:  `{row['user_id']}`",
        f"💰  Current paid credits:  *{row['paid_credits']}*",
        "",
        "👇  Tap a button to *ADD* or *REMOVE* credits",
    ])


def credits_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    add_row_1 = [
        InlineKeyboardButton("➕ 5", callback_data=f"adm_cradd:{user_id}:5"),
        InlineKeyboardButton("➕ 10", callback_data=f"adm_cradd:{user_id}:10"),
        InlineKeyboardButton("➕ 20", callback_data=f"adm_cradd:{user_id}:20"),
    ]
    add_row_2 = [
        InlineKeyboardButton("➕ 50", callback_data=f"adm_cradd:{user_id}:50"),
        InlineKeyboardButton("➕ 100", callback_data=f"adm_cradd:{user_id}:100"),
        InlineKeyboardButton("➕ 200", callback_data=f"adm_cradd:{user_id}:200"),
    ]
    rm_row_1 = [
        InlineKeyboardButton("➖ 5", callback_data=f"adm_crrm:{user_id}:5"),
        InlineKeyboardButton("➖ 10", callback_data=f"adm_crrm:{user_id}:10"),
        InlineKeyboardButton("➖ 20", callback_data=f"adm_crrm:{user_id}:20"),
    ]
    rm_row_2 = [
        InlineKeyboardButton("➖ 50", callback_data=f"adm_crrm:{user_id}:50"),
        InlineKeyboardButton("➖ 100", callback_data=f"adm_crrm:{user_id}:100"),
        InlineKeyboardButton("➖ 200", callback_data=f"adm_crrm:{user_id}:200"),
    ]
    return InlineKeyboardMarkup([
        add_row_1, add_row_2, rm_row_1, rm_row_2,
        [InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")],
    ])


def conversions_page_text(page: int, total: int, user_name: str) -> str:
    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    return "\n".join([
        "🗂️  *CONVERSION HISTORY*",
        _sep(),
        "",
        f"👤  User   :  *{user_name}*",
        f"📄  Page   :  *{page + 1}* / *{total_pages}*",
        f"🔄  Total  :  *{total}*",
        "",
        "_Tap a conversion to view the image_",
    ])


def conversions_page_keyboard(convs: list[dict], user_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for c in convs:
        icon = "✅" if c["status"] == "success" else "❌"
        fname = (c["filename"] or "unknown")[:26]
        rows.append([
            InlineKeyboardButton(
                f"{icon} {fname}",
                callback_data=f"adm_conv:{c['id']}",
            )
        ])

    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_uconv:{user_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_uconv:{user_id}:{page + 1}"))
    if total_pages > 1:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")])
    return InlineKeyboardMarkup(rows)


async def payments_text(user_id: int) -> str:
    row = await get_user_row(user_id)
    name = (row["first_name"] or "(no name)") if row else "?"
    summary = await payment_summary(user_id)

    lines = [
        "💳  *PAYMENT HISTORY*",
        _sep(),
        "",
        f"👤  User  :  *{name}*",
        f"🆔  ID    :  `{user_id}`",
        "",
        f"💰  Successful payments  :  *{summary['count']}*",
        f"⭐  Total Stars paid     :  *{summary['stars']}*",
        "",
        _sep("─"),
    ]

    if summary["payments"]:
        lines.append("📋  *Recent payments*")
        for p in summary["payments"]:
            plan_name = {"plan_20": "20 Conversions",
                         "plan_unlimited_30": "30 Days Unlimited"}.get(
                p["payload"], p["payload"])
            lines.append(f"   ⭐ {plan_name}  •  {p['stars']}  •  {_dt_short(p['created_at'])}")
    else:
        lines.append("_No payments yet_")

    return "\n".join(lines)


def payments_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙  Back to User", callback_data=f"adm_user:{user_id}")],
    ])


def search_prompt_text() -> str:
    return "\n".join([
        "🔍  *SEARCH USER*",
        _sep(),
        "",
        "Send me one of the following:",
        "   •  *Name*  (partial match)",
        "   •  *Username*  (without @)",
        "   •  *Telegram ID*",
        "",
        "Type /cancel to abort.",
    ])


def search_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌  Cancel", callback_data="adm_home")],
    ])


def search_results_text(query: str, count: int) -> str:
    return "\n".join([
        "🔍  *SEARCH RESULTS*",
        _sep(),
        "",
        f"🔎  Query  :  `{query}`",
        f"📊  Found  :  *{count}*  user(s)",
        "",
        "_Tap a user to view details_",
    ])


def search_results_keyboard(results: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in results[:20]:
        uid = r["user_id"]
        name = (r["first_name"] or "No name")[:20]
        total = r["total_conversions"] or 0
        rows.append([
            InlineKeyboardButton(
                f"👤 {name}  ·  {total} conv",
                callback_data=f"adm_user:{uid}",
            )
        ])
    rows.append([InlineKeyboardButton("🔍  New Search", callback_data="adm_search")])
    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


def recent_convs_text(page: int, total: int) -> str:
    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    return "\n".join([
        "🔄  *RECENT CONVERSIONS*",
        _sep(),
        "",
        f"📄  Page  :  *{page + 1}* / *{total_pages}*",
        f"🔄  Total :  *{total}*",
        "",
        "_Tap a conversion to view the image_",
    ])


def recent_convs_keyboard(convs: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for c in convs:
        icon = "✅" if c["status"] == "success" else "❌"
        fname = (c["filename"] or "unknown")[:22]
        uid = c["user_id"]
        rows.append([
            InlineKeyboardButton(
                f"{icon} {fname}  ·  {uid}",
                callback_data=f"adm_conv:{c['id']}",
            )
        ])

    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_recent_conv:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_recent_conv:{page + 1}"))
    if total_pages > 1:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


async def get_recent_convs(page: int):
    rows = await db_fetchall(
        """SELECT id, user_id, filename, status, created_at
           FROM conversions ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        [CONVS_PER_PAGE, page * CONVS_PER_PAGE],
    )
    total = await db_fetchone("SELECT COUNT(*) AS c FROM conversions")
    return rows, (total["c"] if total else 0)


async def get_recent_payments(page: int):
    rows = await db_fetchall(
        """SELECT id, user_id, payload, stars, charge_id, created_at
           FROM payments ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        [CONVS_PER_PAGE, page * CONVS_PER_PAGE],
    )
    total = await db_fetchone("SELECT COUNT(*) AS c FROM payments")
    return rows, (total["c"] if total else 0)


def recent_pays_text(page: int, total: int) -> str:
    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    return "\n".join([
        "💳  *RECENT PAYMENTS*",
        _sep(),
        "",
        f"📄  Page  :  *{page + 1}* / *{total_pages}*",
        f"💰  Total :  *{total}*",
    ])


def recent_pays_keyboard(payments: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in payments:
        plan_name = {"plan_20": "20 credits",
                     "plan_unlimited_30": "30d unlimited"}.get(p["payload"], p["payload"])
        label = f"⭐ {plan_name}  ·  {p['stars']}  ·  {p['user_id']}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"adm_user:{p['user_id']}")
        ])

    total_pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm_recent_pay:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"adm_recent_pay:{page + 1}"))
    if total_pages > 1:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


# ===========================================================================
# ADMIN COMMAND
# ===========================================================================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    context.user_data.pop("admin_search", None)
    args = context.args or []

    if args and args[0].isdigit():
        row = await get_user_row(int(args[0]))
        if row is None:
            await update.message.reply_text(f"🔎 No user with ID {args[0]}.")
            return
        text = await user_detail_text(row)
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_detail_keyboard(row["user_id"]),
        )
        return

    if args and args[0].lower() == "users":
        total = await count_users()
        users = (await get_all_users())[:USERS_PER_PAGE]
        await update.message.reply_text(
            users_page_text(0, total),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=users_page_keyboard(users, 0, total),
        )
        return

    stats = await get_admin_stats()
    await update.message.reply_text(
        dashboard_text(stats),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=dashboard_keyboard(),
    )


# ===========================================================================
# ADMIN CALLBACK ROUTER
# ===========================================================================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != OWNER_ID:
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")

    if data == "adm_noop":
        await query.answer()
        return

    if data == "adm_home":
        context.user_data.pop("admin_search", None)
        await query.answer()
        stats = await get_admin_stats()
        await _safe_edit(query, dashboard_text(stats), dashboard_keyboard())
        return

    if parts[0] == "adm_users":
        await query.answer()
        try:
            page = int(parts[1])
        except (IndexError, ValueError):
            page = 0
        users_all = await get_all_users()
        total = len(users_all)
        start = page * USERS_PER_PAGE
        chunk = users_all[start:start + USERS_PER_PAGE]
        await _safe_edit(
            query,
            users_page_text(page, total),
            users_page_keyboard(chunk, page, total),
        )
        return

    if parts[0] == "adm_user":
        await query.answer()
        try:
            uid = int(parts[1])
        except (IndexError, ValueError):
            return
        row = await get_user_row(uid)
        if row is None:
            await _safe_edit(
                query,
                "🔎 User not found.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")]
                ]),
            )
            return
        text = await user_detail_text(row)
        await _safe_edit(query, text, user_detail_keyboard(uid))
        return

    if parts[0] == "adm_cred_menu":
        await query.answer()
        try:
            uid = int(parts[1])
        except (IndexError, ValueError):
            return
        row = await get_user_row(uid)
        if row is None:
            await query.answer("User not found.", show_alert=True)
            return
        await _safe_edit(query, credits_menu_text(row), credits_menu_keyboard(uid))
        return

    if parts[0] in {"adm_cradd", "adm_crrm"}:
        try:
            uid = int(parts[1])
            amount = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Invalid.")
            return

        row = await get_user_row(uid)
        if row is None:
            await query.answer("User not found.", show_alert=True)
            return

        if parts[0] == "adm_crrm":
            amount = -abs(amount)

        await add_paid_credits(uid, amount)
        updated = await get_user_row(uid)
        if updated and updated["paid_credits"] < 0:
            await db_execute(
                "UPDATE users SET paid_credits = 0 WHERE user_id = ?",
                [uid],
            )
            updated = await get_user_row(uid)

        await query.answer(
            f"✅ {amount:+d} credits applied. New balance: {updated['paid_credits']}"
        )
        await _safe_edit(
            query,
            credits_menu_text(updated),
            credits_menu_keyboard(uid),
        )
        return

    if parts[0] == "adm_unlim_add":
        try:
            uid = int(parts[1])
        except (IndexError, ValueError):
            return
        await add_unlimited_days(uid, UNLIMITED_DAYS)
        await query.answer(f"✅ Unlimited added (+{UNLIMITED_DAYS} days).")
        row = await get_user_row(uid)
        if row:
            await _safe_edit(
                query,
                await user_detail_text(row),
                user_detail_keyboard(uid),
            )
        return

    if parts[0] == "adm_unlim_rm":
        try:
            uid = int(parts[1])
        except (IndexError, ValueError):
            return
        await remove_unlimited(uid)
        await query.answer("✅ Unlimited removed.")
        row = await get_user_row(uid)
        if row:
            await _safe_edit(
                query,
                await user_detail_text(row),
                user_detail_keyboard(uid),
            )
        return

    if parts[0] == "adm_uconv":
        await query.answer()
        try:
            uid = int(parts[1])
            page = int(parts[2])
        except (IndexError, ValueError):
            return
        row = await get_user_row(uid)
        if row is None:
            await query.answer("User not found.", show_alert=True)
            return
        convs, total = await get_user_conversions_page(uid, page, CONVS_PER_PAGE)
        name = row["first_name"] or "(no name)"
        await _safe_edit(
            query,
            conversions_page_text(page, total, name),
            conversions_page_keyboard(convs, uid, page, total),
        )
        return

    if parts[0] == "adm_upay":
        await query.answer()
        try:
            uid = int(parts[1])
        except (IndexError, ValueError):
            return
        await _safe_edit(query, await payments_text(uid), payments_keyboard(uid))
        return

    if parts[0] == "adm_conv":
        try:
            conv_id = int(parts[1])
        except (IndexError, ValueError):
            await query.answer("Invalid.", show_alert=True)
            return
        conv = await db_fetchone(
            """SELECT id, user_id, filename, status, quota_source, file_size,
                      created_at, converted_file_id, original_file_id
               FROM conversions WHERE id = ?""",
            [conv_id],
        )
        if conv is None:
            await query.answer("Conversion not found.", show_alert=True)
            return

        converted_id = conv.get("converted_file_id")
        original_id = conv.get("original_file_id")
        if not converted_id and not original_id:
            await query.answer(
                "No image stored for this entry (older record).",
                show_alert=True,
            )
            return

        await query.answer("Sending image...")
        caption = (
            f"📄 {conv['filename'] or '(unknown)'}\n"
            f"👤 User ID: {conv['user_id']}\n"
            f"📊 Source: {conv['quota_source'] or '-'} | "
            f"Size: {conv['file_size'] or '-'} bytes\n"
            f"🕐 {_dt_display(conv['created_at'])}\n"
            f"{'✅' if conv['status'] == 'success' else '❌'} Status: {conv['status']}"
        )
        try:
            if converted_id:
                await query.message.reply_document(document=converted_id, caption=caption)
            else:
                await query.message.reply_document(
                    document=original_id,
                    caption="⚠️ Original file (JPG not stored).\n\n" + caption,
                )
        except Exception:
            logger.exception("Could not send stored file")
            await query.message.reply_text("⚠️ Could not fetch the stored file.")
        return

    if data == "adm_search":
        await query.answer()
        context.user_data["admin_search"] = True
        await _safe_edit(query, search_prompt_text(), search_prompt_keyboard())
        return

    if parts[0] == "adm_recent_conv":
        await query.answer()
        try:
            page = int(parts[1])
        except (IndexError, ValueError):
            page = 0
        convs, total = await get_recent_convs(page)
        await _safe_edit(
            query,
            recent_convs_text(page, total),
            recent_convs_keyboard(convs, page, total),
        )
        return

    if parts[0] == "adm_recent_pay":
        await query.answer()
        try:
            page = int(parts[1])
        except (IndexError, ValueError):
            page = 0
        pays, total = await get_recent_payments(page)
        await _safe_edit(
            query,
            recent_pays_text(page, total),
            recent_pays_keyboard(pays, page, total),
        )
        return

    await query.answer()


async def _safe_edit(query, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    except Exception:
        try:
            await query.message.reply_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Could not render admin view")


# ===========================================================================
# ADMIN SEARCH TEXT HANDLER
# ===========================================================================
async def admin_search_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != OWNER_ID:
        return
    if not context.user_data.get("admin_search"):
        return

    query_text = (update.message.text or "").strip()
    context.user_data.pop("admin_search", None)

    if not query_text:
        await update.message.reply_text("❌ Empty search.")
        return

    results = await search_users(query_text)

    if not results:
        await update.message.reply_text(
            f"🔍 No users found matching: `{query_text}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍  New Search", callback_data="adm_search")],
                [InlineKeyboardButton("🏠  Dashboard", callback_data="adm_home")],
            ]),
        )
        return

    await update.message.reply_text(
        search_results_text(query_text, len(results)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=search_results_keyboard(results),
    )


# ===========================================================================
# HELPERS
# ===========================================================================
class ConversionError(Exception):
    pass


class ImageTooLargeError(ConversionError):
    """Special exception for oversized images."""
    pass


def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete %s", path, exc_info=True)


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ 20 Conversions — 50 Stars", callback_data="buy_20")],
        [InlineKeyboardButton("♾️ 30 Days Unlimited — 150 Stars", callback_data="buy_unlimited")],
    ])


def plans_text() -> str:
    return (
        "💰 *Available Plans*\n\n"
        "🆓 *Free Plan*\n• 5 successful conversions daily\n\n"
        "⭐ *20 Conversions* — *50 Stars*\n\n"
        "♾️ *30 Days Unlimited* — *150 Stars*\n\n"
        "💳 Choose a plan below:"
    )


# ---------------------------------------------------------------------------
# CONVERSION — Multi-strategy (pyvips → subprocess PIL → in-process PIL)
# ---------------------------------------------------------------------------
def _convert_with_pyvips(input_file: Path, output_file: Path, quality: int) -> None:
    """
    Memory-efficient conversion using libvips.
    libvips streams the image, using only a few scanlines in memory at any time.
    This can convert 200+ MP images on 512MB RAM.
    """
    import pyvips

    # Open with sequential access - streams data instead of loading all at once
    image = pyvips.Image.new_from_file(str(input_file), access="sequential")

    # Resize if larger than MAX_DIMENSION
    if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
        scale = min(MAX_DIMENSION / image.width, MAX_DIMENSION / image.height)
        image = image.resize(scale)

    # Save as JPEG (streaming write)
    image.jpegsave(str(output_file), Q=quality)


def _convert_with_subprocess_pil(input_file: Path, output_file: Path, quality: int) -> None:
    """
    Runs PIL conversion in a subprocess with a memory limit.
    This way, if the conversion OOMs, the main bot process survives.
    """
    script = r'''
import sys
import resource

# Set memory limit to 450 MB for the subprocess
try:
    resource.setrlimit(resource.RLIMIT_AS, (450 * 1024 * 1024, 450 * 1024 * 1024))
except (ValueError, OSError):
    pass

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

input_file = sys.argv[1]
output_file = sys.argv[2]
quality = int(sys.argv[3])
max_dim = int(sys.argv[4])

try:
    with Image.open(input_file) as img:
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        if img.mode in ("RGBA", "LA", "P"):
            rgb = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode in ("RGBA", "LA"):
                rgb.paste(img, mask=img.split()[-1])
            else:
                rgb.paste(img)
            img = rgb
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.save(output_file, "JPEG", quality=quality, optimize=True)

    sys.exit(0)
except MemoryError:
    sys.stderr.write("OUT_OF_MEMORY\n")
    sys.exit(3)
except Exception as e:
    sys.stderr.write(f"ERROR:{e}\n")
    sys.exit(1)
'''

    try:
        result = subprocess.run(
            [
                sys.executable, "-c", script,
                str(input_file), str(output_file),
                str(quality), str(MAX_DIMENSION),
            ],
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("Conversion timed out. The file may be too complex.")

    if result.returncode == 0:
        return

    stderr = result.stderr.decode(errors="replace").strip()

    # SIGKILL from OOM killer
    if result.returncode == -9 or "OUT_OF_MEMORY" in stderr or "MemoryError" in stderr:
        raise ImageTooLargeError(
            "This image is too large to process on this server. "
            "Please reduce its size and try again."
        )

    raise ConversionError(stderr[:200] if stderr else "Conversion failed.")


def _convert_in_process(input_file: Path, output_file: Path, quality: int) -> None:
    """Standard in-process PIL conversion - for smaller images."""
    try:
        with Image.open(input_file) as image:
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
    except MemoryError:
        raise ImageTooLargeError(
            "This image is too large to process on this server."
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ConversionError(str(exc)) from exc


def convert_to_jpg(input_file: Path, output_file: Path, quality: int = JPEG_QUALITY) -> None:
    """
    Convert any image to JPG with maximum chance of success:
    1. If pyvips available → use it (best for large images)
    2. Else → use subprocess PIL with memory limit (bot won't crash)
    """
    if PYVIPS_AVAILABLE:
        try:
            _convert_with_pyvips(input_file, output_file, quality)
            return
        except Exception as e:
            logger.warning("pyvips conversion failed, falling back to subprocess: %s", e)

    # Fallback: subprocess PIL with memory limit
    _convert_with_subprocess_pil(input_file, output_file, quality)


# ===========================================================================
# PROGRESS ANIMATION & TASK MANAGEMENT
# ===========================================================================
async def animate_status(status_msg, user_id: int, status_info: dict):
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    try:
        while True:
            await asyncio.sleep(2)
            frame = frames[i % len(frames)]
            text = f"{frame} {status_info['step']} `{status_info['file'][:20]}...`\n⏳ Please wait..."
            try:
                await status_msg.edit_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_conv:{user_id}")]
                    ])
                )
            except Exception:
                pass
            i += 1
    except asyncio.CancelledError:
        pass


async def cancel_conversion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    user_id = query.from_user.id

    if user_id in active_tasks:
        task = active_tasks[user_id]
        if not task.done():
            task.cancel()
        await query.answer("❌ Conversion cancelled.", show_alert=True)
        try:
            await query.edit_message_text("❌ Cancelled by user.")
        except Exception:
            pass
    else:
        await query.answer("No active conversion to cancel.", show_alert=True)


async def process_conversion(message, user, document, context, source):
    filename = document.file_name or "image.heic"
    extension = Path(filename).suffix.lower()
    unique_id = document.file_unique_id
    input_file = DOWNLOAD_DIR / f"{unique_id}{extension}"
    output_file = OUTPUT_DIR / f"{unique_id}.jpg"

    status_info = {"step": "Downloading...", "file": filename}

    status_msg = await message.reply_text(
        "🔄 File detected...\n⏳ Downloading...",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_conv:{user.id}")]
        ])
    )

    animation_task = asyncio.create_task(animate_status(status_msg, user.id, status_info))

    success = False
    converted_file_id = None

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(input_file)

        status_info["step"] = "Converting..."
        await asyncio.to_thread(convert_to_jpg, input_file, output_file, JPEG_QUALITY)

        status_info["step"] = "Uploading..."
        with output_file.open("rb") as jpg_file:
            sent = await message.reply_document(
                document=jpg_file,
                filename=Path(filename).with_suffix(".jpg").name,
                caption="✅ Conversion complete!",
            )
        if sent.document:
            converted_file_id = sent.document.file_id
        success = True

        try:
            await status_msg.edit_text("✅ Conversion complete!")
        except Exception:
            pass

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text("❌ Conversion cancelled by user.")
        except Exception:
            pass
        raise
    except ImageTooLargeError as e:
        try:
            await status_msg.edit_text(
                "⚠️ *Could not convert — image too large*\n\n"
                f"❌ {str(e)}\n\n"
                "💡 *Try one of these:*\n"
                "• *iPhone:* Settings → Camera → Formats → *High Efficiency*\n"
                "• *Reduce resolution:* Use a photo editor to make it smaller\n"
                "• *Alternative:* Send as JPG or PNG instead\n\n"
                "✅ *Or:* Send a screenshot of the image — that will work!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ConversionError as e:
        try:
            await status_msg.edit_text(
                f"❌ *Conversion failed.*\nReason: `{str(e)[:200]}`\n\n"
                "Please try another image file.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except Exception:
        logger.exception("Unexpected error during conversion")
        try:
            await status_msg.edit_text(
                "❌ *Something went wrong.* Please try again later."
            )
        except Exception:
            pass
    finally:
        animation_task.cancel()
        if success:
            spawn(consume_conversion(user.id, source))
            spawn(record_conversion(
                user.id, filename, "success", source,
                document.file_size, converted_file_id, document.file_id,
            ))
        else:
            spawn(record_conversion(
                user.id, filename, "failed", source,
                document.file_size, None, document.file_id,
            ))
        safe_unlink(input_file)
        safe_unlink(output_file)


# ===========================================================================
# COMMAND HANDLERS
# ===========================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    spawn(ensure_user(update.effective_user))
    await update.message.reply_text(
        "👋 Welcome to Image → JPG Converter!\n\n"
        "📎 How to use:\n"
        "1️⃣ Tap 📎 Attachment\n"
        "2️⃣ Choose File (not Photo)\n"
        "3️⃣ Select an image (HEIC, JPG, PNG, WEBP, etc.)\n"
        "4️⃣ Receive your JPG\n\n"
        "🆓 5 free conversions every day.\n\n"
        "📊 /status — check your remaining\n"
        "⭐ /plans — paid plans\n"
        "❓ /help — help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "📸 Image → JPG Converter\n\n"
        "Send your image as a document (📎 → File).\n\n"
        "Supported: HEIC, HEIF, JPG, JPEG, PNG, WEBP, BMP, TIFF\n"
        "Max file size: 20 MB\n\n"
        "Free: 5 successful conversions/day.\n\n"
        "Commands:\n/start /help /plans /status"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    context.user_data.pop("admin_search", None)
    await update.message.reply_text("✅ Cancelled.")


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        plans_text(), parse_mode="Markdown", reply_markup=plans_keyboard()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    row = await ensure_user(update.effective_user)

    if update.effective_user.id == OWNER_ID:
        text = "📊 Status\n\n♾️ Owner: Unlimited."
    elif unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        text = f"📊 Status\n\n♾️ Unlimited active\n⏰ Until: {until.strftime('%d-%m-%Y %I:%M %p')}"
    else:
        free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
        text = (f"📊 Status\n\n🆓 Free left today: {free_left}/{FREE_DAILY_LIMIT}\n"
                f"⭐ Paid credits: {row['paid_credits']}\n\n💰 /plans")
    await update.message.reply_text(text)


# ===========================================================================
# PAYMENT HANDLERS
# ===========================================================================
async def buy_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer()

    if query.data == "buy_20":
        title, desc, payload, stars = ("20 Image → JPG Conversions",
                                       "20 additional conversions.",
                                       "plan_20", PAID_CREDIT_PRICE_STARS)
    elif query.data == "buy_unlimited":
        title, desc, payload, stars = ("30 Days Unlimited Image → JPG",
                                       "Unlimited for 30 days.",
                                       "plan_unlimited_30", UNLIMITED_PRICE_STARS)
    else:
        return

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=title, description=desc, payload=payload,
        currency="XTR", prices=[LabeledPrice(title, stars)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query:
        return
    if query.invoice_payload not in {"plan_20", "plan_unlimited_30"}:
        await query.answer(ok=False, error_message="Invalid plan.")
        return
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    payment = update.message.successful_payment
    if not payment:
        return

    await ensure_user(update.effective_user)
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    stars = (PAID_CREDIT_PRICE_STARS if payload == "plan_20"
             else UNLIMITED_PRICE_STARS if payload == "plan_unlimited_30" else 0)
    if not stars:
        return

    if not await record_payment(user_id, payload, stars, charge_id):
        logger.warning("Duplicate payment ignored: %s", charge_id)
        return

    if payload == "plan_20":
        await add_paid_credits(user_id, PAID_CREDIT_PACK)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n⭐ *20 credits* added.\n\n"
            + await entitlement_text(user_id),
            parse_mode="Markdown",
        )
    else:
        await add_unlimited_days(user_id, UNLIMITED_DAYS)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n♾️ *30-day Unlimited* active.\n\n"
            + await entitlement_text(user_id),
            parse_mode="Markdown",
        )


# ===========================================================================
# MESSAGE HANDLERS
# ===========================================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id == OWNER_ID:
        context.user_data.pop("admin_search", None)
    await update.message.reply_text(
        "ℹ️ Please send your image as a *document* (📎 → File), not as a photo.\n\n"
        "This preserves original quality so the bot can convert it properly.",
        parse_mode="Markdown",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not message.document or not user:
        return

    if user.id == OWNER_ID:
        context.user_data.pop("admin_search", None)

    spawn(ensure_user(user))

    document = message.document
    filename = document.file_name or "image.heic"

    if not is_supported_file(filename):
        supported_list = "HEIC, HEIF, JPG, JPEG, PNG, WEBP, BMP, TIFF"
        await message.reply_text(
            f"❌ *Unsupported file type.*\n\n"
            f"📎 Please send one of these formats as a document:\n`{supported_list}`",
            parse_mode="Markdown",
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"❌ *File too large.*\nMax allowed: *{MAX_FILE_SIZE_MB} MB*",
            parse_mode="Markdown",
        )
        return

    allowed, source = await can_convert(user.id)
    if not allowed:
        await message.reply_text(
            "🚫 *Free conversions finished for today.*\n\n"
            "💰 Choose a plan to continue:",
            parse_mode="Markdown",
            reply_markup=plans_keyboard(),
        )
        return

    async with conversion_lock:
        if context.user_data.get("cancel_queued"):
            context.user_data.pop("cancel_queued", None)
            return

        task = asyncio.create_task(process_conversion(message, user, document, context, source))
        active_tasks[user.id] = task

        try:
            await task
        except asyncio.CancelledError:
            logger.info("Task cancelled for user %s", user.id)
        finally:
            if user.id in active_tasks and active_tasks[user.id] == task:
                del active_tasks[user.id]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, telegram.error.Conflict):
        logger.warning("Conflict detected: Another bot instance is running. Waiting...")
        return
    logger.error("Update %s caused error:", update, exc_info=context.error)


# ===========================================================================
# POST-INIT
# ===========================================================================
async def on_startup(application: Application) -> None:
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared (safe for polling).")
    except Exception:
        logger.warning("Could not clear webhook.", exc_info=True)

    await init_database()


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_WITH_NEW_BOT_TOKEN":
        raise RuntimeError("BOT_TOKEN is not set.")
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set.")

    logger.info("=" * 60)
    logger.info(" Image → JPG Bot  ·  Full Admin Panel")
    logger.info(" Owner ID: %s", OWNER_ID)
    logger.info(" pyvips available: %s", PYVIPS_AVAILABLE)
    logger.info("=" * 60)

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask health-check server started.")

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
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("status", status_command))

    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm_"))

    application.add_handler(
        CallbackQueryHandler(buy_plan_callback, pattern=r"^buy_(20|unlimited)$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_conversion_callback, pattern=r"^cancel_conv:")
    )
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            admin_search_text,
        )
    )

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(error_handler)

    logger.info("Bot is running with concurrent updates.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
