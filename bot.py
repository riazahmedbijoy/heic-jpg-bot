"""
HEIC → JPG Telegram Bot with Turso (persistent + concurrent)
==============================================================

Features:
- Concurrent updates (multiple users in parallel)
- Persistent Turso cloud database
- Owner: unlimited
- Owner commands:
    /admin                    dashboard with clickable user list
    /admin users              list all users
    /admin USER_ID            full detail for a user
    /admin credits ID AMOUNT  add/remove paid credits
- Clickable dashboard buttons: tap a user to see full detail
- Clickable conversions: tap a conversion to view the JPG
- 5 free/day for normal users
- 50 Stars = 20 credits, 150 Stars = 30 days unlimited
"""

from __future__ import annotations

import asyncio
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
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
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
# FLASK APP (health check)
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
    """Run ALTER TABLE, ignore 'duplicate column' errors."""
    try:
        await db_execute(sql)
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "already exists" in msg:
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

    # Migrations for older databases.
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


# ===========================================================================
# FIRE-AND-FORGET HELPER
# ===========================================================================
def spawn(coro) -> None:
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        logger.warning("spawn() called outside event loop")


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
            """
            INSERT INTO users
            (user_id, first_name, username, free_date, free_used,
             registered_at, last_seen_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            [user_id, user.first_name or "", user.username or "", today, now, now],
        )
    else:
        spawn(
            db_execute(
                """
                UPDATE users
                SET first_name = ?, username = ?, last_seen_at = ?
                WHERE user_id = ?
                """,
                [user.first_name or "", user.username or "", now, user_id],
            )
        )
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


async def activate_unlimited(user_id: int) -> None:
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
    until = base + timedelta(days=UNLIMITED_DAYS)
    await db_execute(
        "UPDATE users SET unlimited_until = ? WHERE user_id = ?",
        [until.isoformat(), user_id],
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
        [
            user_id, filename, status, quota_source, file_size, created_at,
            converted_file_id, original_file_id,
        ],
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
    users = await db_fetchall("SELECT * FROM users ORDER BY last_seen_at DESC")

    return {
        "total_users": total_users, "active_today": active_today,
        "total_attempts": total_attempts, "total_success": total_success,
        "total_failed": total_failed, "active_unlimited": active_unlimited,
        "expired_unlimited": expired_unlimited,
        "total_paid_credits": total_paid_credits,
        "payment_count": payment_count, "total_stars": total_stars,
        "users": users,
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


# ===========================================================================
# ADMIN TEXT BUILDERS
# ===========================================================================
def dashboard_text(stats: dict) -> str:
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
        f"• Paid credits (all): {stats['total_paid_credits']}",
        "",
        "💳 PAYMENTS",
        f"• Successful: {stats['payment_count']}",
        f"• Total Stars: {stats['total_stars']}",
        "",
        "👤 RECENT USERS (tap a name to see details)",
        "",
        "📌 /admin users | /admin USER_ID",
        "📌 /admin credits USER_ID AMOUNT",
        "💾 DB: Turso (async persistent)",
    ]
    return "\n".join(lines)


def dashboard_keyboard(stats: dict) -> InlineKeyboardMarkup:
    """Build inline keyboard with each recent user as a clickable button."""
    rows: list[list[InlineKeyboardButton]] = []

    for row in stats["users"][:10]:
        user_id = row["user_id"]
        name = row["first_name"] or "(no name)"
        username = row["username"] or ""
        total_conv = row["total_conversions"] or 0

        # Trim long names so callback_data doesn't explode.
        label_name = name if len(name) <= 18 else name[:17] + "…"
        label = f"👤 {label_name}  ·  {total_conv} conv"

        rows.append([
            InlineKeyboardButton(label, callback_data=f"adm_user:{user_id}")
        ])

    if not rows:
        rows.append([InlineKeyboardButton("(No users yet)", callback_data="adm_noop")])

    return InlineKeyboardMarkup(rows)


async def build_user_detail(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Return (text, keyboard) for the user detail page."""
    row = await get_user_row(user_id)
    if row is None:
        return (f"🔎 No user found with ID: {user_id}", InlineKeyboardMarkup([]))

    name = row["first_name"] or "(no name)"
    username = row["username"] or "(no username)"
    free_used = row["free_used"]
    paid_credits = row["paid_credits"]
    unlimited_until = row["unlimited_until"]

    if user_id == OWNER_ID:
        plan = "👑 ADMIN / OWNER"
    elif unlimited_until:
        try:
            until = datetime.fromisoformat(unlimited_until)
            plan = (f"♾️ ACTIVE until {_dt_display(unlimited_until)}"
                    if until > now_bd()
                    else f"⏳ EXPIRED at {_dt_display(unlimited_until)}")
        except (ValueError, TypeError):
            plan = f"♾️ {unlimited_until}"
    else:
        plan = "🆓 Free plan"

    payments = await payment_summary(user_id)
    conversions = await conversion_summary(user_id)

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
            plan_name = {"plan_20": "20 Conversions",
                         "plan_unlimited_30": "30 Days Unlimited"}.get(
                pay["payload"], pay["payload"])
            lines.append(
                f"  ⭐ {plan_name} | {pay['stars']} Stars | "
                f"{_dt_display(pay['created_at'])}"
            )
    else:
        lines.append("• No payment found")

    lines.append("")
    lines.append("🗂️ RECENT CONVERSIONS (tap a row to see the image)")

    # Buttons for each recent conversion.
    buttons: list[list[InlineKeyboardButton]] = []
    for item in conversions["recent"][:15]:
        icon = "✅" if item["status"] == "success" else "❌"
        fname = item["filename"] or "(unknown)"
        if len(fname) > 26:
            fname = fname[:25] + "…"
        label = f"{icon} {fname}"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"adm_conv:{item['id']}")
        ])

    buttons.append([
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm_back")
    ])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ===========================================================================
# ADMIN COMMAND
# ===========================================================================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner only.")
        return

    args = context.args

    # ---- /admin credits USER_ID AMOUNT ---------------------------------
    if args and args[0].lower() in {"credits", "credit", "addcredits", "addcredit"}:
        if len(args) < 3:
            await update.message.reply_text(
                "❌ Usage: /admin credits USER_ID AMOUNT\n"
                "Example: /admin credits 8704880107 20\n"
                "Negative to remove: /admin credits 8704880107 -5"
            )
            return
        try:
            target_id = int(args[1])
            amount = int(args[2])
        except ValueError:
            await update.message.reply_text("❌ USER_ID and AMOUNT must be numbers.")
            return

        row = await get_user_row(target_id)
        if row is None:
            await update.message.reply_text(f"🔎 No user found with ID: {target_id}")
            return

        await add_paid_credits(target_id, amount)
        updated = await get_user_row(target_id)

        action = "added to" if amount >= 0 else "removed from"
        await update.message.reply_text(
            f"✅ *Credits updated*\n\n"
            f"🆔 User: {target_id}\n"
            f"📊 {abs(amount)} credit(s) {action} their balance.\n"
            f"⭐ New paid credits: {updated['paid_credits']}",
            parse_mode="Markdown",
        )
        return

    # ---- /admin users ---------------------------------------------------
    if args and args[0].lower() == "users":
        stats = await get_admin_stats()
        chunks = await admin_all_users_chunks(stats["users"])
        if not chunks:
            await update.message.reply_text("👥 No registered users yet.")
            return
        for chunk in chunks:
            await update.message.reply_text(chunk)
        await update.message.reply_text(f"📌 Total: {stats['total_users']}")
        return

    # ---- /admin USER_ID -------------------------------------------------
    if args:
        try:
            target_id = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Use:\n"
                "/admin                    — dashboard\n"
                "/admin users              — all users\n"
                "/admin USER_ID            — one user detail\n"
                "/admin credits ID AMOUNT  — add/remove credits"
            )
            return

        text, keyboard = await build_user_detail(target_id)
        # Split long text but only attach keyboard to last part.
        parts = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                await update.message.reply_text(part, reply_markup=keyboard)
            else:
                await update.message.reply_text(part)
        return

    # ---- /admin (dashboard) ---------------------------------------------
    stats = await get_admin_stats()
    await update.message.reply_text(
        dashboard_text(stats),
        reply_markup=dashboard_keyboard(stats),
    )


async def admin_all_users_chunks(users: list[dict]) -> list[str]:
    chunks = []
    current = ["👥 ALL REGISTERED USERS", "━━━━━━━━━━━━━━━━━━━━", ""]
    for index, row in enumerate(users, start=1):
        live = (await get_user_row(row["user_id"])) or row
        name = live["first_name"] or "(no name)"
        username = live["username"] or "(no username)"
        unlimited = live.get("unlimited_until")
        if row["user_id"] == OWNER_ID:
            plan = "👑 Owner"
        elif unlimited:
            try:
                until = datetime.fromisoformat(unlimited)
                plan = (f"♾️ Until {until.strftime('%d-%m-%Y')}"
                        if until > now_bd() else "⏳ Expired")
            except (ValueError, TypeError):
                plan = "♾️ Active"
        else:
            plan = "🆓 Free"
        conv = await conversion_summary(row["user_id"])
        item = (
            f"{index}. {name} | @{username.lstrip('@')}\n"
            f"   🆔 {row['user_id']} | 🆓 {live['free_used']}/{FREE_DAILY_LIMIT} | "
            f"⭐ {live['paid_credits']} | {plan}\n"
            f"   🔄 {conv['total']} total | ✅ {conv['successful']} | ❌ {conv['failed']}\n"
        )
        if len("\n".join(current)) + len(item) > 3600:
            chunks.append("\n".join(current))
            current = ["👥 ALL REGISTERED USERS", "━━━━━━━━━━━━━━━━━━━━", ""]
        current.append(item)
    if len(current) > 3:
        chunks.append("\n".join(current))
    return chunks


# ===========================================================================
# ADMIN CALLBACKS (clickable)
# ===========================================================================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle clicks from the admin dashboard buttons."""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != OWNER_ID:
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    data = query.data or ""

    # ---- no-op ----------------------------------------------------------
    if data == "adm_noop":
        await query.answer()
        return

    # ---- back to dashboard ---------------------------------------------
    if data == "adm_back":
        await query.answer()
        stats = await get_admin_stats()
        try:
            await query.edit_message_text(
                dashboard_text(stats),
                reply_markup=dashboard_keyboard(stats),
            )
        except Exception:
            # If edit fails (e.g. content unchanged), send a new message.
            await query.message.reply_text(
                dashboard_text(stats),
                reply_markup=dashboard_keyboard(stats),
            )
        return

    # ---- show a user detail --------------------------------------------
    if data.startswith("adm_user:"):
        await query.answer()
        try:
            user_id = int(data.split(":", 1)[1])
        except ValueError:
            return

        text, keyboard = await build_user_detail(user_id)
        parts = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
        try:
            await query.edit_message_text(
                parts[0],
                reply_markup=keyboard if len(parts) == 1 else None,
            )
        except Exception:
            await query.message.reply_text(parts[0])
        for part in parts[1:]:
            await query.message.reply_text(
                part,
                reply_markup=keyboard if part is parts[-1] else None,
            )
        return

    # ---- show a specific conversion image ------------------------------
    if data.startswith("adm_conv:"):
        try:
            conv_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid conversion.", show_alert=True)
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
                "No image stored for this entry (older records).",
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
            f"✅ Status: {conv['status']}"
        )

        # Prefer the converted JPG; fall back to original HEIC.
        try:
            if converted_id:
                await query.message.reply_document(
                    document=converted_id,
                    caption=caption,
                )
            else:
                await query.message.reply_document(
                    document=original_id,
                    caption="⚠️ Original HEIC file (JPG not stored).\n\n" + caption,
                )
        except Exception:
            logger.exception("Could not send stored file")
            await query.message.reply_text(
                "⚠️ Could not fetch the stored file. It may be too old."
            )
        return

    await query.answer()


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
        "🆓 *Free Plan*\n• 5 successful conversions daily\n\n"
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
    spawn(ensure_user(update.effective_user))
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
        "Commands:\n/start /help /plans /status"
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
        title, desc, payload, stars = ("20 HEIC → JPG Conversions",
                                       "20 additional conversions.",
                                       "plan_20", PAID_CREDIT_PRICE_STARS)
    elif query.data == "buy_unlimited":
        title, desc, payload, stars = ("30 Days Unlimited HEIC → JPG",
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
        await activate_unlimited(user_id)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n♾️ *30-day Unlimited* active.\n\n"
            + await entitlement_text(user_id),
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

    spawn(ensure_user(user))

    document = message.document
    filename = document.file_name or "image.heic"
    original_file_id = document.file_id

    if not is_supported_file(filename):
        await message.reply_text(
            "❌ *Unsupported file.* Send a `.HEIC` or `.HEIF`.",
            parse_mode="Markdown",
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"❌ File too large. Max {MAX_FILE_SIZE_MB} MB.")
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
    converted_file_id: str | None = None

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(input_file)
        await asyncio.to_thread(convert_to_jpg, input_file, output_file, JPEG_QUALITY)

        with output_file.open("rb") as jpg_file:
            sent = await message.reply_document(
                document=jpg_file,
                filename=Path(filename).with_suffix(".jpg").name,
                caption="✅ Conversion complete!",
            )
        # Capture the file_id of the sent JPG so the admin can view it later.
        if sent.document:
            converted_file_id = sent.document.file_id
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
            spawn(consume_conversion(user.id, source))
            spawn(record_conversion(
                user.id, filename, "success", source,
                document.file_size, converted_file_id, original_file_id,
            ))
        else:
            spawn(record_conversion(
                user.id, filename, "failed", source,
                document.file_size, None, original_file_id,
            ))

        safe_unlink(input_file)
        safe_unlink(output_file)
        try:
            await status_message.delete()
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error:", update, exc_info=context.error)


# ===========================================================================
# POST-INIT
# ===========================================================================
async def on_startup(application: Application) -> None:
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
    logger.info(" HEIC → JPG Bot (concurrent + admin clickable)")
    logger.info(" Owner ID: %s", OWNER_ID)
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

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("status", status_command))

    # Admin inline clicks (must be BEFORE buy_ callback handler because
    # CallbackQueryHandler with a pattern would otherwise swallow adm_*)
    application.add_handler(
        CallbackQueryHandler(admin_callback, pattern=r"^adm_")
    )

    # Purchase callbacks
    application.add_handler(
        CallbackQueryHandler(buy_plan_callback, pattern=r"^buy_(20|unlimited)$")
    )
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
    )

    # Image handlers
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
