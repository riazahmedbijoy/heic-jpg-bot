"""
HEIC → JPG Telegram Bot
========================

A Telegram bot that converts HEIC/HEIF images (commonly produced by
iPhones) into universally supported JPG files.

Deployment-ready for Render.com (uses Flask for health check endpoint).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pillow_heif
from flask import Flask
from PIL import Image, UnidentifiedImageError
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===========================================================================
# FLASK APP (for Render health check + UptimeRobot pinging)
# ===========================================================================
flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    """Health-check endpoint. UptimeRobot pings this to keep the bot awake."""
    return "Bot is running", 200


def run_flask() -> None:
    """Run the Flask server in a background thread."""
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# ===========================================================================
# CONFIGURATION
# ===========================================================================
# Default token (used locally). On Render, the BOT_TOKEN environment
# variable will automatically override this value.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "8884896400:AAEE65VVZGGE_EN4H6nCr8sa_u4jeaB9q98",
)

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
# CUSTOM EXCEPTIONS
# ===========================================================================
class ConversionError(Exception):
    """Raised when an image cannot be converted to JPG."""


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================
def is_supported_file(filename: str) -> bool:
    """Return True if the filename has a supported HEIC/HEIF extension."""
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def safe_unlink(path: Path) -> None:
    """Delete a file if it exists, ignoring any filesystem errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete %s", path, exc_info=True)


def convert_to_jpg(
    input_file: Path,
    output_file: Path,
    quality: int = JPEG_QUALITY,
) -> None:
    """
    Convert a HEIC/HEIF image to JPG.

    Args:
        input_file: Source HEIC/HEIF path.
        output_file: Destination JPG path.
        quality: JPEG quality (1–100).

    Raises:
        ConversionError: If the image cannot be opened or saved.
    """
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
    """Handle the /start command."""
    if not update.message:
        return

    await update.message.reply_text(
        "👋 *Welcome to HEIC → JPG Converter!*\n\n"
        "📸 Send a `.HEIC` or `.HEIF` file as a *document*.\n\n"
        "⚡ Fast conversion\n"
        "🆓 Free to use\n"
        "🔒 Files are deleted right after conversion\n\n"
        "Use /help for more information.",
        parse_mode="Markdown",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle the /help command."""
    if not update.message:
        return

    await update.message.reply_text(
        "📸 *HEIC → JPG Converter*\n\n"
        "*How to use:*\n"
        "1. Tap the 📎 attachment icon\n"
        "2. Choose *File* (not Photo)\n"
        "3. Select your `.HEIC` or `.HEIF` file\n"
        "4. Wait for the bot to return a JPG\n\n"
        "*Commands:*\n"
        "/start – Welcome message\n"
        "/help  – This help message",
        parse_mode="Markdown",
    )


# ===========================================================================
# MESSAGE HANDLERS
# ===========================================================================
async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reject regular Telegram photos and guide the user to send as a file."""
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ Please send the original HEIC image as a *document* "
        "(📎 → File), not as a Telegram photo.\n\n"
        "This preserves the original quality.",
        parse_mode="Markdown",
    )


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Download, convert, and return the JPG version of a HEIC document."""
    message = update.message
    if not message or not message.document:
        return

    document = message.document
    filename = document.file_name or "image.heic"

    # ---- Validate file extension ------------------------------------------
    if not is_supported_file(filename):
        await message.reply_text(
            "❌ *Unsupported file type.*\n\n"
            "Please send a `.HEIC` or `.HEIF` file.",
            parse_mode="Markdown",
        )
        return

    # ---- Validate file size ----------------------------------------------
    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"❌ *File too large.*\n"
            f"Maximum allowed size is {MAX_FILE_SIZE_MB} MB.",
            parse_mode="Markdown",
        )
        return

    # ---- Prepare paths ---------------------------------------------------
    extension = Path(filename).suffix.lower()
    unique_id = document.file_unique_id
    input_file = DOWNLOAD_DIR / f"{unique_id}{extension}"
    output_file = OUTPUT_DIR / f"{unique_id}.jpg"

    # ---- Notify user -----------------------------------------------------
    status_message = await message.reply_text(
        "🔄 HEIC detected...\n⏳ Downloading and converting..."
    )
    try:
        await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    except Exception:
        logger.debug("Could not send chat action", exc_info=True)

    try:
        # ---- Download from Telegram --------------------------------------
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(input_file)

        # ---- Convert HEIC → JPG ------------------------------------------
        convert_to_jpg(input_file, output_file, quality=JPEG_QUALITY)

        # ---- Send JPG back to the user -----------------------------------
        with output_file.open("rb") as jpg_file:
            await message.reply_document(
                document=jpg_file,
                filename=Path(filename).with_suffix(".jpg").name,
                caption="✅ HEIC → JPG conversion complete!",
            )

    except ConversionError:
        logger.warning("Conversion failed for user %s", message.from_user)
        await message.reply_text(
            "❌ *Conversion failed.*\n\n"
            "The file may be corrupted or uses an unsupported HEIC variant.",
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Unexpected error while handling document")
        await message.reply_text(
            "❌ Something went wrong while processing your file.\n"
            "Please try again later."
        )

    finally:
        # ---- Cleanup -----------------------------------------------------
        safe_unlink(input_file)
        safe_unlink(output_file)
        try:
            await status_message.delete()
        except Exception:
            logger.debug("Could not delete status message", exc_info=True)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Global error handler — logs any unhandled exceptions."""
    logger.error("Update %s caused error:", update, exc_info=context.error)


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    """Start the Telegram bot."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. Either hardcode it in bot.py "
            "or set the BOT_TOKEN environment variable."
        )

    logger.info("=" * 50)
    logger.info(" HEIC → JPG Telegram Bot")
    logger.info(" Starting...")
    logger.info("=" * 50)

    # ---- Start Flask server in a background thread -----------------------
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(" Flask health-check server started.")

    # ---- Build Telegram application --------------------------------------
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(300)
        .write_timeout(300)
        .pool_timeout(60)
        .build()
    )

    # ---- Register handlers -----------------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # ---- Register global error handler -----------------------------------
    application.add_error_handler(error_handler)

    logger.info(" Bot is running. Send /start on Telegram.")
    logger.info("=" * 50)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ===========================================================================
# ENTRY POINT
# ===========================================================================
if __name__ == "__main__":
    main()