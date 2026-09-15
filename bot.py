"""
HEIC → JPG Telegram Bot with Turso (persistent cloud database via HTTP)
========================================================================

Features:
- Cloud database on Turso (SQLite-compatible) — survives every deploy
- Uses HTTP transport (avoids WebSocket issues on Render)
- Owner: unlimited conversions
- Owner /admin dashboard, /admin users, /admin USER_ID
- Other users: 5 free successful conversions per Bangladesh day
- 50 Telegram Stars: 20 additional conversion credits
- 150 Telegram Stars: 30 days unlimited
- Flask health endpoint for Render/UptimeRobot
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import libsql_client
import pillow_heif
from flask import Flask
from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ChatAction
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
    flask_app.run(host="0.0.0.0", port=port)


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
SUPPORTED_EXTENSIONS = {".heic", ".heif"}

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

db_lock = threading.Lock()


# ===========================================================================
# TURSO DATABASE LAYER  (HTTP TRANSPORT — no WebSocket)
# ===========================================================================
_db_client: libsql_client.Client | None = None


def _normalize_turso_url(raw_url: str) -> str:
    """
    Convert a libsql:// URL to an https:// URL so that libsql_client
    uses HTTP transport instead of WebSocket.

    Render's networking blocks/breaks the WebSocket handshake used by
    the default libsql:// scheme, so we force HTTP(S) here.
    """
    url = raw_url.strip()

    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    elif url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]

    # Strip a trailing slash for consistency.
    url = url.rstrip("/")

    return url


def get_client() -> libsql_client.Client:
    """Return a lazily-created Turso client using HTTP transport."""
    global _db_client

    if _db_client is None:
        if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
            raise RuntimeError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set."
            )

        http_url = _normalize_turso_url(TURSO_DATABASE_URL)

        _db_client = libsql_client.create_client_sync(
            url=http_url,
            auth_token=TURSO_AUTH_TOKEN,
        )

        logger.info("Connected to Turso via HTTP: %s", http_url)

    return _db_client


def db_execute(sql: str, params: list | None = None):
    """Execute a statement and return the raw result."""
    client = get_client()
    return client.execute(sql, params or [])


def db_fetchone(sql: str, params: list | None = None) -> dict | None:
    """Return the first row as a dict, or None."""
    result = db_execute(sql, params)
    if not result.rows:
        return None
    return dict(zip(result.columns, result.rows[0]))


def db_fetchall(sql: str, params: list | None = None) -> list[dict]:
    """Return all rows as a list of dicts."""
    result = db_execute(sql, params)
    return [dict(zip(result.columns, row)) for row in result.rows]


def init_database() -> None:
    """Create tables if they don't exist."""
    with db_lock:
        db_execute(
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

        db_execute(
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

        db_execute(
            """
            CREATE TABLE IF NOT EXISTS conversions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT,
                status TEXT NOT NULL,
                quota_source TEXT,
                file_size INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
    logger.info("Database schema is ready.")


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


# ===========================================================================
# USER MANAGEMENT
# ===========================================================================
def ensure_user(user) -> dict:
    user_id = user.id
    today = today_bd()
    now = now_bd().isoformat()

    with db_lock:
        row = db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

        if row is None:
            db_execute(
                """
                INSERT INTO users
                (user_id, first_name, username, free_date, free_used,
                 registered_at, last_seen_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                [
                    user_id,
                    user.first_name or "",
                    user.username or "",
                    today,
                    now,
                    now,
                ],
            )
        else:
            db_execute(
                """
                UPDATE users
                SET first_name = ?, username = ?, last_seen_at = ?
                WHERE user_id = ?
                """,
                [
                    user.first_name or "",
                    user.username or "",
                    now,
                    user_id,
                ],
            )

            if row["free_date"] != today:
                db_execute(
                    "UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?",
                    [today, user_id],
                )

        return db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])


def get_user_row(user_id: int) -> dict | None:
    with db_lock:
        row = db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

        if row and row["free_date"] != today_bd():
            db_execute(
                "UPDATE users SET free_date = ?, free_used = 0 WHERE user_id = ?",
                [today_bd(), user_id],
            )
            row = db_fetchone("SELECT * FROM users WHERE user_id = ?", [user_id])

        return row


def unlimited_active(row: dict | None) -> bool:
    if not row or not row.get("unlimited_until"):
        return False
    try:
        return datetime.fromisoformat(row["unlimited_until"]) > now_bd()
    except (ValueError, TypeError):
        return False


def entitlement_text(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "👑 Owner: Unlimited"

    row = get_user_row(user_id)
    if not row:
        return "🆓 Free today: 5"

    if unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        return (
            "♾️ Unlimited active\n"
            f"⏰ Until: {until.strftime('%d-%m-%Y %I:%M %p')}"
        )

    free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
    return (
        f"🆓 Free today: {free_left}/{FREE_DAILY_LIMIT}\n"
        f"⭐ Paid credits: {row['paid_credits']}"
    )


def can_convert(user_id: int) -> tuple[bool, str]:
    if user_id == OWNER_ID:
        return True, "owner"

    row = get_user_row(user_id)
    if row is None:
        return True, "free"
    if unlimited_active(row):
        return True, "unlimited"
    if (FREE_DAILY_LIMIT - row["free_used"]) > 0:
        return True, "free"
    if row["paid_credits"] > 0:
        return True, "paid"
    return False, "none"


def consume_conversion(user_id: int, source: str) -> None:
    if user_id == OWNER_ID:
        return
    with db_lock:
        if source == "free":
            db_execute(
                "UPDATE users SET free_used = free_used + 1 WHERE user_id = ?",
                [user_id],
            )
        elif source == "paid":
            db_execute(
                """UPDATE users SET paid_credits = paid_credits - 1
                   WHERE user_id = ? AND paid_credits > 0""",
                [user_id],
            )


def add_paid_credits(user_id: int, amount: int) -> None:
    with db_lock:
        db_execute(
            "UPDATE users SET paid_credits = paid_credits + ? WHERE user_id = ?",
            [amount, user_id],
        )


def activate_unlimited(user_id: int) -> None:
    current = get_user_row(user_id)
    current_until = None
    if current and current.get("unlimited_until"):
        try:
            current_until = datetime.fromisoformat(current["unlimited_until"])
        except ValueError:
            current_until = None

    base = now_bd()
    if current_until and current_until > base:
        base = current_until
    until = base + timedelta(days=UNLIMITED_DAYS)

    with db_lock:
        db_execute(
            "UPDATE users SET unlimited_until = ? WHERE user_id = ?",
            [until.isoformat(), user_id],
        )


def record_payment(user_id: int, payload: str, stars: int, charge_id: str) -> bool:
    with db_lock:
        try:
            db_execute(
                """INSERT INTO payments
                   (user_id, payload, stars, charge_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [user_id, payload, stars, charge_id, now_bd().isoformat()],
            )
            return True
        except Exception:
            logger.warning("Duplicate or failed payment insert: %s", charge_id)
            return False


def record_conversion(
    user_id: int,
    filename: str,
    status: str,
    quota_source: str | None,
    file_size: int | None,
) -> None:
    created_at = now_bd().isoformat()
    with db_lock:
        db_execute(
            """INSERT INTO conversions
               (user_id, filename, status, quota_source, file_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [user_id, filename, status, quota_source, file_size, created_at],
        )
        if status == "success":
            db_execute(
                """UPDATE users
                   SET total_conversions = total_conversions + 1,
                       successful_conversions = successful_conversions + 1,
                       last_seen_at = ?
                   WHERE user_id = ?""",
                [created_at, user_id],
            )
        elif status == "failed":
            db_execute(
                """UPDATE users
                   SET total_conversions = total_conversions + 1,
                       failed_conversions = failed_conversions + 1,
                       last_seen_at = ?
                   WHERE user_id = ?""",
                [created_at, user_id],
            )


# ===========================================================================
# ADMIN STATS
# ===========================================================================
def get_admin_stats() -> dict:
    now = now_bd().isoformat()
    today = today_bd()
    with db_lock:
        total_users = db_fetchone("SELECT COUNT(*) AS c FROM users")["c"]
        active_today = db_fetchone(
            """SELECT COUNT(DISTINCT user_id) AS c FROM conversions
               WHERE status = 'success' AND substr(created_at, 1, 10) = ?""",
            [today],
        )["c"]
        total_attempts = db_fetchone("SELECT COUNT(*) AS c FROM conversions")["c"]
        total_success = db_fetchone(
            "SELECT COUNT(*) AS c FROM conversions WHERE status = 'success'"
        )["c"]
        total_failed = db_fetchone(
            "SELECT COUNT(*) AS c FROM conversions WHERE status = 'failed'"
        )["c"]
        active_unlimited = db_fetchone(
            """SELECT COUNT(*) AS c FROM users
               WHERE user_id != ? AND unlimited_until IS NOT NULL
               AND unlimited_until > ?""",
            [OWNER_ID, now],
        )["c"]
        expired_unlimited = db_fetchone(
            """SELECT COUNT(*) AS c FROM users
               WHERE user_id != ? AND unlimited_until IS NOT NULL
               AND unlimited_until <= ?""",
            [OWNER_ID, now],
        )["c"]
        total_paid_credits = db_fetchone(
            "SELECT COALESCE(SUM(paid_credits), 0) AS c FROM users"
        )["c"]
        payment_count = db_fetchone("SELECT COUNT(*) AS c FROM payments")["c"]
        total_stars = db_fetchone(
            "SELECT COALESCE(SUM(stars), 0) AS c FROM payments"
        )["c"]
        users = db_fetchall(
            "SELECT * FROM users ORDER BY last_seen_at DESC"
        )
        return {
            "total_users": total_users,
            "active_today": active_today,
            "total_attempts": total_attempts,
            "total_success": total_success,
            "total_failed": total_failed,
            "active_unlimited": active_unlimited,
            "expired_unlimited": expired_unlimited,
            "total_paid_credits": total_paid_credits,
            "payment_count": payment_count,
            "total_stars": total_stars,
            "users": users,
        }


def payment_summary(user_id: int) -> dict:
    with db_lock:
        row = db_fetchone(
            """SELECT COUNT(*) AS count, COALESCE(SUM(stars), 0) AS stars
               FROM payments WHERE user_id = ?""",
            [user_id],
        )
        payments = db_fetchall(
            """SELECT payload, stars, charge_id, created_at FROM payments
               WHERE user_id = ? ORDER BY created_at DESC LIMIT 10""",
            [user_id],
        )
        return {"count": row["count"], "stars": row["stars"], "payments": payments}


def conversion_summary(user_id: int) -> dict:
    with db_lock:
        row = db_fetchone(
            """SELECT
                 COUNT(*) AS total,
                 COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS successful,
                 COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed
               FROM conversions WHERE user_id = ?""",
            [user_id],
        )
        recent = db_fetchall(
            """SELECT filename, status, quota_source, file_size, created_at
               FROM conversions WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 20""",
            [user_id],
        )
        return {
            "total": row["total"],
            "successful": row["successful"],
            "failed": row["failed"],
            "recent": recent,
        }


def admin_user_text(row: dict) -> str:
    user_id = row["user_id"]
    name = row["first_name"] or "(no name)"
    username = row["username"] or "(no username)"

    current = get_user_row(user_id) or row
    free_used = current["free_used"]
    paid_credits = current["paid_credits"]
    unlimited_until = current["unlimited_until"]

    if user_id == OWNER_ID:
        plan = "👑 ADMIN / OWNER"
    elif unlimited_until:
        try:
            until = datetime.fromisoformat(unlimited_until)
            plan = (
                f"♾️ ACTIVE until {_dt_display(unlimited_until)}"
                if until > now_bd()
                else f"⏳ EXPIRED at {_dt_display(unlimited_until)}"
            )
        except (ValueError, TypeError):
            plan = f"♾️ {unlimited_until}"
    else:
        plan = "🆓 Free plan"

    payments = payment_summary(user_id)
    conversions = conversion_summary(user_id)

    lines = [
        "🔐 USER ACCOUNT DETAILS",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👤 Name: {name}",
        f"🔗 Username: @{username.lstrip('@')}",
        f"🆔 Telegram ID: {user_id}",
        "",
        "📅 ACCOUNT",
        f"• Registered: {_dt_display(row['registered_at'])}",
        f"• Last seen: {_dt_display(row['last_seen_at'])}",
        "",
        "📊 CURRENT BALANCE",
        f"• Free used today: {free_used}/{FREE_DAILY_LIMIT}",
        f"• Free remaining: {max(0, FREE_DAILY_LIMIT - free_used)}",
        f"• Paid credits remaining: {paid_credits}",
        "",
        "📦 CURRENT PLAN",
        f"• {plan}",
        "",
        "🔄 CONVERSION HISTORY",
        f"• Total attempts: {conversions['total']}",
        f"• Successful: {conversions['successful']}",
        f"• Failed: {conversions['failed']}",
        "",
        "💳 PAYMENT HISTORY",
        f"• Successful payments: {payments['count']}",
        f"• Total Stars paid: {payments['stars']}",
    ]

    if payments["payments"]:
        lines.append("• Recent payments:")
        for pay in payments["payments"]:
            plan_name = {
                "plan_20": "20 Conversions",
                "plan_unlimited_30": "30 Days Unlimited",
            }.get(pay["payload"], pay["payload"])
            lines.append(
                f"  ⭐ {plan_name} | {pay['stars']} Stars | "
                f"{_dt_display(pay['created_at'])}"
            )
    else:
        lines.append("• No payment found")

    if conversions["recent"]:
        lines.extend(["", "🗂️ RECENT CONVERSIONS"])
        for item in conversions["recent"]:
            icon = "✅" if item["status"] == "success" else "❌"
            lines.append(
                f"{icon} {item['filename'] or '(unknown)'}\n"
                f"   Source: {item['quota_source'] or '-'} | "
                f"Size: {item['file_size'] or '-'} bytes\n"
                f"   Time: {_dt_display(item['created_at'])}"
            )

    return "\n".join(lines)


def admin_all_users_chunks(users: list[dict]) -> list[str]:
    chunks = []
    current = ["👥 ALL REGISTERED USERS", "━━━━━━━━━━━━━━━━━━━━", ""]

    for index, row in enumerate(users, start=1):
        live = get_user_row(row["user_id"]) or row
        name = live["first_name"] or "(no name)"
        username = live["username"] or "(no username)"

        unlimited = live.get("unlimited_until")
        if row["user_id"] == OWNER_ID:
            plan = "👑 Owner"
        elif unlimited:
            try:
                until = datetime.fromisoformat(unlimited)
                plan = (
                    f"♾️ Until {until.strftime('%d-%m-%Y')}"
                    if until > now_bd()
                    else "⏳ Expired"
                )
            except (ValueError, TypeError):
                plan = "♾️ Active"
        else:
            plan = "🆓 Free"

        conv = conversion_summary(row["user_id"])
        item = (
            f"{index}. {name} | @{username.lstrip('@')}\n"
            f"   🆔 {row['user_id']} | 🆓 {live['free_used']}/{FREE_DAILY_LIMIT} | "
            f"⭐ {live['paid_credits']} | {plan}\n"
            f"   🔄 {conv['total']} total | ✅ {conv['successful']} | "
            f"❌ {conv['failed']}\n"
        )

        if len("\n".join(current)) + len(item) > 3600:
            chunks.append("\n".join(current))
            current = ["👥 ALL REGISTERED USERS", "━━━━━━━━━━━━━━━━━━━━", ""]
        current.append(item)

    if len(current) > 3:
        chunks.append("\n".join(current))
    return chunks


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "⛔ This command is available only to the bot administrator."
        )
        return

    args = context.args

    if args and args[0].lower() == "users":
        stats = get_admin_stats()
        chunks = admin_all_users_chunks(stats["users"])
        if not chunks:
            await update.message.reply_text("👥 No registered users yet.")
            return
        for chunk in chunks:
            await update.message.reply_text(chunk)
        await update.message.reply_text(
            f"📌 Total: {stats['total_users']}\n"
            "Use /admin USER_ID for details."
        )
        return

    if args:
        try:
            target_id = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Use: /admin | /admin users | /admin USER_ID"
            )
            return
        row = get_user_row(target_id)
        if row is None:
            await update.message.reply_text(f"🔎 Not found: {target_id}")
            return
        text = admin_user_text(row)
        for i in range(0, len(text), 3800):
            await update.message.reply_text(text[i:i + 3800])
        return

    stats = get_admin_stats()
    lines = [
        "🔐 ADMIN DASHBOARD",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👥 Total registered users: {stats['total_users']}",
        f"📈 Users active today: {stats['active_today']}",
        "",
        "🔄 CONVERSIONS",
        f"• Total attempts: {stats['total_attempts']}",
        f"• Successful: {stats['total_success']}",
        f"• Failed: {stats['total_failed']}",
        "",
        "📦 PLANS",
        f"• Active unlimited: {stats['active_unlimited']}",
        f"• Expired unlimited: {stats['expired_unlimited']}",
        f"• Paid credits (all users): {stats['total_paid_credits']}",
        "",
        "💳 PAYMENTS",
        f"• Successful: {stats['payment_count']}",
        f"• Total Stars: {stats['total_stars']}",
        "",
        "👤 RECENT USERS",
    ]

    for i, row in enumerate(stats["users"][:10], start=1):
        live = get_user_row(row["user_id"]) or row
        name = live["first_name"] or "(no name)"
        username = live["username"] or "(no username)"
        conv = conversion_summary(row["user_id"])
        lines.append(f"{i}. {name} | @{username.lstrip('@')}")
        lines.append(
            f"   ID: {row['user_id']} | Free: {live['free_used']}/{FREE_DAILY_LIMIT} | "
            f"Paid: {live['paid_credits']} | Total: {conv['total']} | ✅ {conv['successful']}"
        )

    lines.extend([
        "",
        "📌 /admin users — all users",
        "📌 /admin USER_ID — one user details",
        "",
        "💾 Database: Turso Cloud (persistent, HTTP)",
    ])

    await update.message.reply_text("\n".join(lines))


# ===========================================================================
# HELPERS
# ===========================================================================
class ConversionError(Exception):
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
        "🆓 *Free Plan*\n"
        "• 5 successful conversions daily\n\n"
        "⭐ *20 Conversions* — *50 Stars*\n\n"
        "♾️ *30 Days Unlimited* — *150 Stars*\n\n"
        "💳 Choose a plan below:"
    )


def convert_to_jpg(input_file: Path, output_file: Path, quality: int = JPEG_QUALITY) -> None:
    try:
        with Image.open(input_file) as image:
            image.convert("RGB").save(
                output_file, format="JPEG", quality=quality, optimize=True,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.exception("Conversion failed for %s", input_file)
        raise ConversionError(str(exc)) from exc


# ===========================================================================
# COMMAND HANDLERS
# ===========================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "👋 Welcome to HEIC → JPG Converter!\n\n"
        "📎 How to use:\n"
        "1️⃣ Tap 📎 Attachment\n"
        "2️⃣ Choose File (not Photo)\n"
        "3️⃣ Select .HEIC or .HEIF file\n"
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
        "📸 HEIC → JPG Converter\n\n"
        "Send your HEIC image as a document (📎 → File).\n\n"
        "Free: 5 successful conversions/day.\n\n"
        "Commands:\n"
        "/start /help /plans /status"
    )


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        plans_text(), parse_mode="Markdown", reply_markup=plans_keyboard()
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    row = ensure_user(update.effective_user)

    if update.effective_user.id == OWNER_ID:
        text = "📊 Status\n\n♾️ Owner: Unlimited."
    elif unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        text = (
            "📊 Status\n\n♾️ Unlimited active\n"
            f"⏰ Until: {until.strftime('%d-%m-%Y %I:%M %p')}"
        )
    else:
        free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
        text = (
            "📊 Status\n\n"
            f"🆓 Free left today: {free_left}/{FREE_DAILY_LIMIT}\n"
            f"⭐ Paid credits: {row['paid_credits']}\n\n"
            "💰 /plans"
        )
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
        title, desc, payload, stars = (
            "20 HEIC → JPG Conversions",
            "20 additional conversions.",
            "plan_20",
            PAID_CREDIT_PRICE_STARS,
        )
    elif query.data == "buy_unlimited":
        title, desc, payload, stars = (
            "30 Days Unlimited HEIC → JPG",
            "Unlimited for 30 days.",
            "plan_unlimited_30",
            UNLIMITED_PRICE_STARS,
        )
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

    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    stars = (
        PAID_CREDIT_PRICE_STARS if payload == "plan_20"
        else UNLIMITED_PRICE_STARS if payload == "plan_unlimited_30"
        else 0
    )
    if not stars:
        return

    if not record_payment(user_id, payload, stars, charge_id):
        logger.warning("Duplicate payment ignored: %s", charge_id)
        return

    if payload == "plan_20":
        add_paid_credits(user_id, PAID_CREDIT_PACK)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n⭐ *20 credits* added.\n\n"
            + entitlement_text(user_id),
            parse_mode="Markdown",
        )
    else:
        activate_unlimited(user_id)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n♾️ *30-day Unlimited* active.\n\n"
            + entitlement_text(user_id),
            parse_mode="Markdown",
        )


# ===========================================================================
# MESSAGE HANDLERS
# ===========================================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "ℹ️ Send your HEIC as a *document* (📎 → File), not as a photo.",
        parse_mode="Markdown",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not message.document or not user:
        return

    ensure_user(user)
    document = message.document
    filename = document.file_name or "image.heic"

    allowed, source = can_convert(user.id)
    if not allowed:
        await message.reply_text(
            "🚫 *Free conversions finished for today.*\n\n"
            "💰 Choose a plan to continue:",
            parse_mode="Markdown",
            reply_markup=plans_keyboard(),
        )
        return

    if not is_supported_file(filename):
        await message.reply_text(
            "❌ *Unsupported file.* Send a `.HEIC` or `.HEIF`.",
            parse_mode="Markdown",
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"❌ File too large. Max {MAX_FILE_SIZE_MB} MB."
        )
        return

    extension = Path(filename).suffix.lower()
    unique_id = document.file_unique_id
    input_file = DOWNLOAD_DIR / f"{unique_id}{extension}"
    output_file = OUTPUT_DIR / f"{unique_id}.jpg"

    status_message = await message.reply_text(
        "🔄 HEIC detected...\n⏳ Downloading and converting..."
    )
    try:
        await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    except Exception:
        pass

    success = False
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(input_file)
        convert_to_jpg(input_file, output_file, quality=JPEG_QUALITY)
        with output_file.open("rb") as jpg_file:
            await message.reply_document(
                document=jpg_file,
                filename=Path(filename).with_suffix(".jpg").name,
                caption="✅ Conversion complete!",
            )
        success = True
    except ConversionError:
        await message.reply_text(
            "❌ *Conversion failed.* Try another HEIC/HEIF file.",
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Unexpected error")
        await message.reply_text("❌ Something went wrong. Try again later.")
    finally:
        if success:
            consume_conversion(user.id, source)
            record_conversion(user.id, filename, "success", source, document.file_size)
        else:
            record_conversion(user.id, filename, "failed", source, document.file_size)
        safe_unlink(input_file)
        safe_unlink(output_file)
        try:
            await status_message.delete()
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error:", update, exc_info=context.error)


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_WITH_NEW_BOT_TOKEN":
        raise RuntimeError("BOT_TOKEN is not set.")
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set."
        )

    init_database()

    logger.info("=" * 60)
    logger.info(" HEIC → JPG Bot (Turso persistent storage via HTTP)")
    logger.info(" Owner ID: %s", OWNER_ID)
    logger.info(" Turso URL: %s", _normalize_turso_url(TURSO_DATABASE_URL))
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
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(buy_plan_callback, pattern=r"^buy_(20|unlimited)$"))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(error_handler)

    logger.info("Bot is running.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
