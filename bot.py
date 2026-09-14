"""
HEIC → JPG Telegram Bot
=======================

Features:
- Owner: unlimited free conversions
- Owner /admin command with user/account statistics
- Other users: 5 free successful conversions per Bangladesh day
- 50 Telegram Stars: 20 additional conversion credits
- 150 Telegram Stars: 30 days unlimited
- SQLite database for user quota/payment records
- Flask health endpoint for Render/UptimeRobot
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
# IMPORTANT:
# This fallback token is kept only for local testing.
# Before GitHub/Render deployment, replace it with a new BotFather token
# and preferably use the BOT_TOKEN environment variable.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "REPLACE_WITH_NEW_BOT_TOKEN",
)

OWNER_ID = 2075368011

FREE_DAILY_LIMIT = 5
PAID_CREDIT_PACK = 20
PAID_CREDIT_PRICE_STARS = 50
UNLIMITED_DAYS = 30
UNLIMITED_PRICE_STARS = 150

TIMEZONE = ZoneInfo("Asia/Dhaka")

DOWNLOAD_DIR = Path("downloads")
OUTPUT_DIR = Path("converted")
DATABASE_FILE = Path("bot_data.db")

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
# DATABASE
# ===========================================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_database() -> None:
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    free_date TEXT NOT NULL,
                    free_used INTEGER NOT NULL DEFAULT 0,
                    paid_credits INTEGER NOT NULL DEFAULT 0,
                    unlimited_until TEXT
                )
                """
            )
            conn.execute(
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
            conn.commit()
        finally:
            conn.close()


def today_bd() -> str:
    return datetime.now(TIMEZONE).date().isoformat()


def now_bd() -> datetime:
    return datetime.now(TIMEZONE)


def ensure_user(user) -> sqlite3.Row:
    user_id = user.id
    today = today_bd()

    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO users
                    (user_id, first_name, username, free_date, free_used)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (
                        user_id,
                        user.first_name or "",
                        user.username or "",
                        today,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET first_name = ?, username = ?
                    WHERE user_id = ?
                    """,
                    (
                        user.first_name or "",
                        user.username or "",
                        user_id,
                    ),
                )

                if row["free_date"] != today:
                    conn.execute(
                        """
                        UPDATE users
                        SET free_date = ?, free_used = 0
                        WHERE user_id = ?
                        """,
                        (today, user_id),
                    )

            conn.commit()

            return conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()


def get_user_row(user_id: int) -> sqlite3.Row | None:
    with db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            if row and row["free_date"] != today_bd():
                conn.execute(
                    """
                    UPDATE users
                    SET free_date = ?, free_used = 0
                    WHERE user_id = ?
                    """,
                    (today_bd(), user_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()

            return row
        finally:
            conn.close()


def unlimited_active(row: sqlite3.Row | None) -> bool:
    if not row or not row["unlimited_until"]:
        return False

    try:
        until = datetime.fromisoformat(row["unlimited_until"])
        return until > now_bd()
    except ValueError:
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
    credits = row["paid_credits"]

    return (
        f"🆓 Free today: {free_left}/{FREE_DAILY_LIMIT}\n"
        f"⭐ Paid credits: {credits}"
    )


def can_convert(user_id: int) -> tuple[bool, str]:
    if user_id == OWNER_ID:
        return True, "owner"

    row = get_user_row(user_id)
    if row is None:
        return True, "free"

    if unlimited_active(row):
        return True, "unlimited"

    free_left = FREE_DAILY_LIMIT - row["free_used"]
    if free_left > 0:
        return True, "free"

    if row["paid_credits"] > 0:
        return True, "paid"

    return False, "none"


def consume_conversion(user_id: int, source: str) -> None:
    if user_id == OWNER_ID:
        return

    with db_lock:
        conn = get_db()
        try:
            if source == "free":
                conn.execute(
                    """
                    UPDATE users
                    SET free_used = free_used + 1
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
            elif source == "paid":
                conn.execute(
                    """
                    UPDATE users
                    SET paid_credits = paid_credits - 1
                    WHERE user_id = ? AND paid_credits > 0
                    """,
                    (user_id,),
                )
            conn.commit()
        finally:
            conn.close()


def add_paid_credits(user_id: int, amount: int) -> None:
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE users
                SET paid_credits = paid_credits + ?
                WHERE user_id = ?
                """,
                (amount, user_id),
            )
            conn.commit()
        finally:
            conn.close()


def activate_unlimited(user_id: int) -> None:
    current = get_user_row(user_id)
    current_until = None

    if current and current["unlimited_until"]:
        try:
            current_until = datetime.fromisoformat(current["unlimited_until"])
        except ValueError:
            current_until = None

    base = now_bd()
    if current_until and current_until > base:
        base = current_until

    until = base + timedelta(days=UNLIMITED_DAYS)

    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE users
                SET unlimited_until = ?
                WHERE user_id = ?
                """,
                (until.isoformat(), user_id),
            )
            conn.commit()
        finally:
            conn.close()


def record_payment(
    user_id: int,
    payload: str,
    stars: int,
    charge_id: str,
) -> bool:
    """
    Record a payment once.

    Returns True only when this charge_id was newly recorded.
    This prevents duplicate webhook/update processing from granting
    the same package twice.
    """
    with db_lock:
        conn = get_db()
        try:
            try:
                conn.execute(
                    """
                    INSERT INTO payments
                    (user_id, payload, stars, charge_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        payload,
                        stars,
                        charge_id,
                        now_bd().isoformat(),
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
        finally:
            conn.close()



# ===========================================================================
# ADMIN / OWNER
# ===========================================================================
def get_admin_stats() -> dict:
    """Return simple account/payment statistics for the owner."""
    with db_lock:
        conn = get_db()
        try:
            total_users = conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            today = today_bd()
            active_today = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE free_date = ? AND free_used > 0",
                (today,),
            ).fetchone()["c"]

            active_unlimited = conn.execute(
                "SELECT COUNT(*) AS c FROM users "
                "WHERE unlimited_until IS NOT NULL AND unlimited_until > ?",
                (now_bd().isoformat(),),
            ).fetchone()["c"]

            total_paid_credits = conn.execute(
                "SELECT COALESCE(SUM(paid_credits), 0) AS c FROM users"
            ).fetchone()["c"]

            payment_count = conn.execute(
                "SELECT COUNT(*) AS c FROM payments"
            ).fetchone()["c"]

            total_stars = conn.execute(
                "SELECT COALESCE(SUM(stars), 0) AS c FROM payments"
            ).fetchone()["c"]

            recent_users = conn.execute(
                "SELECT user_id, first_name, username, free_used, "
                "paid_credits, unlimited_until "
                "FROM users ORDER BY rowid DESC LIMIT 10"
            ).fetchall()

            return {
                "total_users": total_users,
                "active_today": active_today,
                "active_unlimited": active_unlimited,
                "total_paid_credits": total_paid_credits,
                "payment_count": payment_count,
                "total_stars": total_stars,
                "recent_users": recent_users,
            }
        finally:
            conn.close()


def format_admin_user(row: sqlite3.Row) -> str:
    """Format one user row without Markdown so usernames cannot break formatting."""
    user_id = row["user_id"]
    first_name = row["first_name"] or "(no name)"
    username = row["username"] or "(no username)"
    free_used = row["free_used"]
    paid_credits = row["paid_credits"]
    unlimited_until = row["unlimited_until"]

    if unlimited_until:
        try:
            until = datetime.fromisoformat(unlimited_until)
            if until > now_bd():
                plan = f"Unlimited until {until.strftime('%d-%m-%Y %I:%M %p')}"
            else:
                plan = "Free / expired unlimited"
        except ValueError:
            plan = "Free"
    else:
        plan = "Free"

    return (
        f"👤 {first_name}\n"
        f"🆔 ID: {user_id}\n"
        f"🔗 Username: @{username.lstrip('@') if username != '(no username)' else '(no username)'}\n"
        f"🆓 Used today: {free_used}/{FREE_DAILY_LIMIT}\n"
        f"⭐ Paid credits: {paid_credits}\n"
        f"📦 Plan: {plan}"
    )


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Owner-only admin dashboard. /admin or /admin USER_ID."""
    if not update.message or not update.effective_user:
        return

    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "⛔ This command is available only to the bot administrator."
        )
        return

    # /admin USER_ID -> show a specific user's stored information.
    if context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID.\n\nExample:\n/admin 123456789"
            )
            return

        row = get_user_row(target_id)
        if row is None:
            await update.message.reply_text(
                f"🔎 No stored account found for user ID: {target_id}"
            )
            return

        await update.message.reply_text(
            "🔐 USER INFORMATION\n\n" + format_admin_user(row)
        )
        return

    stats = get_admin_stats()

    lines = [
        "🔐 ADMIN DASHBOARD",
        "",
        f"👑 Owner ID: {OWNER_ID}",
        f"👥 Total registered users: {stats['total_users']}",
        f"📈 Users with conversion today: {stats['active_today']}",
        f"♾️ Active unlimited users: {stats['active_unlimited']}",
        f"⭐ Remaining paid credits (all users): {stats['total_paid_credits']}",
        f"💳 Successful payments: {stats['payment_count']}",
        f"🌟 Total Stars received: {stats['total_stars']}",
        "",
        "🕘 RECENT USERS",
    ]

    if not stats["recent_users"]:
        lines.append("No users registered yet.")
    else:
        for i, row in enumerate(stats["recent_users"], start=1):
            username = row["username"] or "(no username)"
            name = row["first_name"] or "(no name)"
            lines.append(
                f"{i}. {name} | @{username.lstrip('@') if username != '(no username)' else '(no username)'} "
                f"| ID: {row['user_id']}"
            )

    lines.extend([
        "",
        "ℹ️ To inspect a user:",
        "/admin USER_ID",
    ])

    await update.message.reply_text("\n".join(lines))


# ===========================================================================
# CUSTOM EXCEPTIONS
# ===========================================================================
class ConversionError(Exception):
    pass


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================
def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete %s", path, exc_info=True)


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⭐ 20 Conversions — 50 Stars",
                    callback_data="buy_20",
                )
            ],
            [
                InlineKeyboardButton(
                    "♾️ 30 Days Unlimited — 150 Stars",
                    callback_data="buy_unlimited",
                )
            ],
        ]
    )


def plans_text() -> str:
    return (
        "💰 *Available Plans*\\n\\n"
        "🆓 *Free Plan*\\n"
        "• 5 successful conversions every day\\n"
        "• Resets automatically each Bangladesh day\\n\\n"
        "⭐ *20 Conversions*\\n"
        "• 20 additional conversions\\n"
        "• Price: *50 Telegram Stars*\\n\\n"
        "♾️ *30 Days Unlimited*\\n"
        "• Unlimited conversions for 30 days\\n"
        "• Price: *150 Telegram Stars*\\n\\n"
        "💳 Choose a plan below to continue."
    )


def convert_to_jpg(
    input_file: Path,
    output_file: Path,
    quality: int = JPEG_QUALITY,
) -> None:
    try:
        with Image.open(input_file) as image:
            image.convert("RGB").save(
                output_file,
                format="JPEG",
                quality=quality,
                optimize=True,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.exception("Conversion failed for %s", input_file)
        raise ConversionError(str(exc)) from exc


# ===========================================================================
# COMMAND HANDLERS
# ===========================================================================
async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    ensure_user(update.effective_user)

    await update.message.reply_text(
        "👋 *Welcome to HEIC → JPG Converter!*\\n\\n"
        "📸 Convert your HEIC & HEIF images to JPG quickly and easily.\\n\\n"
        "📎 *How to convert:*\\n"
        "Send your `.HEIC` or `.HEIF` file as a *document* (📎 → File).\\n\\n"
        "✨ Fast and easy conversion\\n"
        "🆓 5 free successful conversions every day\\n"
        "🔒 Your uploaded file is deleted after conversion\\n\\n"
        "📊 Check your remaining conversions: /status\\n"
        "💰 Need more conversions? Use /plans\\n"
        "❓ Need help? Use /help\\n\\n"
        "🚀 Send your HEIC file to get started!",
        parse_mode="Markdown",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "📸 *HEIC → JPG Converter*\\n\\n"
        "*How to use:*\\n"
        "1️⃣ Tap 📎 Attachment\\n"
        "2️⃣ Choose *File* — not Photo\\n"
        "3️⃣ Select your `.HEIC` or `.HEIF` file\\n"
        "4️⃣ Send it and wait for the JPG\\n\\n"
        "*Free usage:*\\n"
        "🆓 You can make up to *5 successful conversions per day*.\\n"
        "Only successful conversions count.\\n\\n"
        "*Commands:*\\n"
        "/start – Welcome & instructions\\n"
        "/help – How to use the bot\\n"
        "/plans – View available plans\\n"
        "/status – Check your remaining conversions\\n\\n"
        "🔒 Files are automatically deleted after conversion.",
        parse_mode="Markdown",
    )


async def plans_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        plans_text(),
        parse_mode="Markdown",
        reply_markup=plans_keyboard(),
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    ensure_user(update.effective_user)

    row = ensure_user(update.effective_user)

    if update.effective_user.id == OWNER_ID:
        text = (
            "📊 *Your Conversion Status*\\n\\n"
            "♾️ Unlimited access is active.\\n"
            "👑 You can convert without a daily limit."
        )
    elif unlimited_active(row):
        until = datetime.fromisoformat(row["unlimited_until"])
        text = (
            "📊 *Your Conversion Status*\\n\\n"
            "♾️ *Unlimited plan active*\\n"
            f"⏰ Valid until: {until.strftime('%d-%m-%Y %I:%M %p')}\\n\\n"
            "You can convert without a daily limit."
        )
    else:
        free_left = max(0, FREE_DAILY_LIMIT - row["free_used"])
        credits = row["paid_credits"]
        text = (
            "📊 *Your Conversion Status*\\n\\n"
            f"🆓 Free conversions left today: *{free_left} / {FREE_DAILY_LIMIT}*\\n"
            f"⭐ Paid conversion credits: *{credits}*\\n\\n"
            "💰 Need more? Use /plans"
        )

    await update.message.reply_text(text, parse_mode="Markdown")


# ===========================================================================
# PAYMENT HANDLERS
# ===========================================================================
async def buy_plan_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    await query.answer()

    if query.data == "buy_20":
        title = "20 HEIC → JPG Conversions"
        description = "Get 20 additional HEIC → JPG conversions."
        payload = "plan_20"
        stars = PAID_CREDIT_PRICE_STARS
    elif query.data == "buy_unlimited":
        title = "30 Days Unlimited HEIC → JPG"
        description = "Unlimited HEIC → JPG conversions for 30 days."
        payload = "plan_unlimited_30"
        stars = UNLIMITED_PRICE_STARS
    else:
        return

    # Telegram Stars use XTR.
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(title, stars)],
    )


async def precheckout_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.pre_checkout_query
    if not query:
        return

    if query.invoice_payload not in {"plan_20", "plan_unlimited_30"}:
        await query.answer(
            ok=False,
            error_message="Invalid payment plan.",
        )
        return

    await query.answer(ok=True)


async def successful_payment_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    payment = update.message.successful_payment
    if not payment:
        return

    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    if payload == "plan_20":
        stars = PAID_CREDIT_PRICE_STARS
    elif payload == "plan_unlimited_30":
        stars = UNLIMITED_PRICE_STARS
    else:
        logger.warning("Unknown payment payload: %s", payload)
        return

    # Never grant the same Telegram payment twice.
    is_new = record_payment(
        user_id=user_id,
        payload=payload,
        stars=stars,
        charge_id=charge_id,
    )

    if not is_new:
        logger.warning("Duplicate payment ignored: %s", charge_id)
        return

    if payload == "plan_20":
        add_paid_credits(user_id, PAID_CREDIT_PACK)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n"
            "⭐ *20 conversion credits* have been added to your account.\n\n"
            + entitlement_text(user_id),
            parse_mode="Markdown",
        )

    elif payload == "plan_unlimited_30":
        activate_unlimited(user_id)
        await update.message.reply_text(
            "🎉 *Payment successful!*\n\n"
            "♾️ Your *30-day Unlimited plan* is now active.\n\n"
            + entitlement_text(user_id),
            parse_mode="Markdown",
        )


# ===========================================================================
# MESSAGE HANDLERS
# ===========================================================================
async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ Please send your HEIC image as a *document* "
        "(📎 → File), not as a Telegram photo.\n\n"
        "📎 Tap Attachment → File → select the HEIC/HEIF file.",
        parse_mode="Markdown",
    )


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.message
    user = update.effective_user

    if not message or not message.document or not user:
        return

    ensure_user(user)

    # -----------------------------------------------------------------------
    # Check quota BEFORE downloading/converting.
    # Owner is always allowed.
    # -----------------------------------------------------------------------
    allowed, source = can_convert(user.id)

    if not allowed:
        await message.reply_text(
            "🚫 *Your free conversions are finished for today.*\n\n"
            "You have used all 5 free conversions available today.\n\n"
            "💰 Choose a paid plan below to continue converting:",
            parse_mode="Markdown",
            reply_markup=plans_keyboard(),
        )
        return

    document = message.document
    filename = document.file_name or "image.heic"

    # -----------------------------------------------------------------------
    # Validate extension
    # -----------------------------------------------------------------------
    if not is_supported_file(filename):
        await message.reply_text(
            "❌ *Unsupported file type.*\n\n"
            "Please send a `.HEIC` or `.HEIF` file as a document.",
            parse_mode="Markdown",
        )
        return

    # -----------------------------------------------------------------------
    # Validate file size
    # -----------------------------------------------------------------------
    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"❌ *File too large.*\n"
            f"Maximum allowed size is {MAX_FILE_SIZE_MB} MB. Please choose a smaller file.",
            parse_mode="Markdown",
        )
        return

    extension = Path(filename).suffix.lower()
    unique_id = document.file_unique_id

    input_file = DOWNLOAD_DIR / f"{unique_id}{extension}"
    output_file = OUTPUT_DIR / f"{unique_id}.jpg"

    status_message = await message.reply_text(
        "🔄 HEIC detected...\n"
        "⏳ Downloading and converting..."
    )

    try:
        await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    except Exception:
        logger.debug("Could not send chat action", exc_info=True)

    conversion_succeeded = False

    try:
        # Download
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(input_file)

        # Convert
        convert_to_jpg(input_file, output_file, quality=JPEG_QUALITY)

        # Send JPG
        with output_file.open("rb") as jpg_file:
            await message.reply_document(
                document=jpg_file,
                filename=Path(filename).with_suffix(".jpg").name,
                caption="✅ HEIC → JPG conversion complete!",
            )

        conversion_succeeded = True

    except ConversionError:
        logger.warning("Conversion failed for user %s", user.id)
        await message.reply_text(
            "❌ *Conversion failed.*\n\n"
            "The file may be corrupted or use an unsupported HEIC variant. "
            "Please try another HEIC/HEIF file.",
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Unexpected error while handling document")
        await message.reply_text(
            "❌ Something went wrong while processing your file.\n"
            "Please try again later."
        )

    finally:
        # IMPORTANT: quota is consumed only after a successful conversion.
        if conversion_succeeded:
            consume_conversion(user.id, source)

        safe_unlink(input_file)
        safe_unlink(output_file)

        try:
            await status_message.delete()
        except Exception:
            logger.debug("Could not delete status message", exc_info=True)


# ===========================================================================
# ERROR HANDLER
# ===========================================================================
async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "Update %s caused error:",
        update,
        exc_info=context.error,
    )


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    init_database()

    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_WITH_NEW_BOT_TOKEN":
        raise RuntimeError(
            "BOT_TOKEN is not configured. Set BOT_TOKEN environment variable "
            "or replace the local testing token."
        )

    logger.info("=" * 60)
    logger.info(" HEIC → JPG Telegram Bot")
    logger.info(" Owner ID: %s", OWNER_ID)
    logger.info(" Free daily limit: %s", FREE_DAILY_LIMIT)
    logger.info(" 20 conversions: %s Stars", PAID_CREDIT_PRICE_STARS)
    logger.info(" 30 days unlimited: %s Stars", UNLIMITED_PRICE_STARS)
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

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("status", status_command))

    # Telegram Stars payment flow
    application.add_handler(
        CallbackQueryHandler(
            buy_plan_callback,
            pattern=r"^buy_(20|unlimited)$",
        )
    )
    application.add_handler(
        PreCheckoutQueryHandler(precheckout_callback)
    )
    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_handler,
        )
    )

    # Image/document handlers
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is running. Send /start on Telegram.")
    logger.info("Owner admin command enabled: /admin")
    logger.info("=" * 60)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
