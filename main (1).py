"""Telegram movie-code bot with JSON storage and Admin panel (Group support)."""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# HTTP request logs can contain Telegram API URLs, so keep them out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CATALOG_FILE = "catalog.json"
REQUIRED_CHAT_CONFIG_FILE = "required_chat_config.json"


def load_catalog() -> dict[str, object]:
    """Load the catalog, keeping older string entries compatible."""
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as catalog_file:
            catalog = json.load(catalog_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not load catalog file")
        raise

    if not isinstance(catalog, dict):
        raise ValueError("Catalog must be a JSON object")
    return catalog


def save_catalog(catalog: dict[str, object]) -> None:
    """Atomically save the catalog to disk."""
    temp_file = f"{CATALOG_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as catalog_file:
            json.dump(catalog, catalog_file, ensure_ascii=False, indent=2)
        os.replace(temp_file, CATALOG_FILE)
    except OSError:
        logger.exception("Could not save catalog file")
        try:
            os.remove(temp_file)
        except OSError:
            pass
        raise


def is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_video_caption(
    caption: str | None,
    fallback_code: str | None = None,
) -> tuple[str, str]:
    """Read a code from the first caption line and keep the remaining text."""
    lines = caption.strip().splitlines() if caption else []
    if fallback_code:
        code = fallback_code.strip().upper()
        if lines and lines[0].strip().upper() == code:
            lines = lines[1:]
        return code, "\n".join(lines).strip()

    if not lines:
        return "", ""

    first_line = lines.pop(0).strip().split(maxsplit=1)
    code = first_line[0].upper() if first_line else ""
    description = ([first_line[1]] if len(first_line) > 1 else []) + lines
    return code, "\n".join(description).strip()


def load_required_chat_config() -> tuple[int | str | None, str]:
    """Load the admin-configured chat, falling back to environment settings."""
    try:
        with open(REQUIRED_CHAT_CONFIG_FILE, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        config = {
            "chat_id": os.getenv("REQUIRED_CHANNEL_ID", "").strip(),
            "join_url": os.getenv("REQUIRED_CHANNEL_URL", "").strip(),
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not load required chat configuration")
        return None, ""

    if not isinstance(config, dict):
        return None, ""

    raw_id = str(config.get("chat_id", "")).strip()
    join_url = str(config.get("join_url", "")).strip()
    if not raw_id:
        return None, join_url
    try:
        chat_id: int | str = int(raw_id)
    except ValueError:
        chat_id = raw_id
    return chat_id, join_url


def save_required_chat_config(chat_id: int, join_url: str) -> None:
    """Save the private join URL outside source control."""
    config_path = os.path.abspath(REQUIRED_CHAT_CONFIG_FILE)
    temp_file = os.path.join(
        os.path.dirname(config_path),
        f".{os.path.basename(config_path)}.tmp",
    )
    try:
        with open(temp_file, "w", encoding="utf-8") as config_file:
            json.dump(
                {"chat_id": chat_id, "join_url": join_url},
                config_file,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temp_file, config_path)
    except OSError:
        logger.exception("Could not save required chat configuration")
        try:
            os.remove(temp_file)
        except OSError:
            pass
        raise


def parse_chat_reference(value: str) -> int | str:
    """Accept a Telegram @username or a negative Telegram group/channel ID."""
    value = value.strip()
    if value.startswith("@"):
        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
            return value
        raise ValueError("Invalid Telegram username")
    try:
        chat_id = int(value)
    except ValueError as error:
        raise ValueError("Use a Telegram @username or negative chat ID") from error
    if chat_id >= 0:
        raise ValueError("Telegram group/channel IDs must be negative")
    return chat_id


def is_telegram_join_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
    )


def user_is_channel_member(member) -> bool:
    """Return whether a Telegram chat-member result grants channel access."""
    status = getattr(member, "status", None)
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


async def require_channel_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    movie_code: str,
) -> bool:
    """Require channel membership before allowing a movie lookup."""
    message = update.effective_message
    user = update.effective_user
    channel_id, join_url = load_required_chat_config()

    if message is None or user is None:
        return False
    if channel_id is None or not join_url:
        await message.reply_text(
            "Kanal tekshiruvi sozlanmagan. Iltimos, admin bilan bog‘laning."
        )
        return False

    try:
        member = await context.bot.get_chat_member(
            chat_id=channel_id,
            user_id=user.id,
        )
    except TelegramError:
        logger.exception("Could not verify required channel membership")
        await message.reply_text(
            "A’zolikni tekshirib bo‘lmadi. Bot kanal admini ekanini tekshiring."
        )
        return False

    if user_is_channel_member(member):
        context.user_data.pop("pending_movie_lookup", None)
        return True

    context.user_data["pending_movie_lookup"] = movie_code
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Kanalga qo‘shilish", url=join_url)],
            [
                InlineKeyboardButton(
                    "A’zo bo‘ldim — tekshirish",
                    callback_data="check_channel_membership",
                )
            ],
        ]
    )
    await message.reply_text(
        "Kino olish uchun avval kanalga a’zo bo‘ling, keyin tekshirish tugmasini bosing.",
        reply_markup=keyboard,
    )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome users and explain how to find a movie."""
    message = update.effective_message
    if message is not None:
        await message.reply_text("Salom! Kino kodi yoki nomini yuboring:")


async def admin_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the admin menu to the configured admin."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if user.id != ADMIN_ID:
        await message.reply_text("Sizda bu buyruqdan foydalanish huquqi yo‘q!")
        return

    keyboard = [
        [InlineKeyboardButton("Statistika", callback_data="admin_stats")],
        [
            InlineKeyboardButton(
                "Kino qo‘shish",
                callback_data="admin_add_movie",
            )
        ],
        [
            InlineKeyboardButton(
                "Kanal/guruh sozlash",
                callback_data="admin_set_required_chat",
            )
        ],
    ]
    await message.reply_text(
        "<b>Xush kelibsiz, admin!</b>\nKerakli amalni tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle admin menu button selections."""
    query = update.callback_query
    if query is None:
        return

    if query.data == "check_channel_membership":
        channel_id, join_url = load_required_chat_config()
        if channel_id is None or not join_url:
            await query.answer("Kanal tekshiruvi sozlanmagan.", show_alert=True)
            return

        try:
            member = await context.bot.get_chat_member(
                chat_id=channel_id,
                user_id=query.from_user.id,
            )
        except TelegramError:
            logger.exception("Could not verify channel membership from button")
            await query.answer(
                "Tekshiruv ishlamadi. Bot kanal admini ekanini tekshiring.",
                show_alert=True,
            )
            return

        if not user_is_channel_member(member):
            await query.answer(
                "Kanalga a’zo bo‘ling, so‘ng yana tekshiring.",
                show_alert=True,
            )
            return

        movie_code = context.user_data.pop("pending_movie_lookup", None)
        await query.answer("A’zolik tasdiqlandi.")
        if movie_code and query.message is not None:
            await deliver_movie(query.message, context, movie_code)
        elif query.message is not None:
            await query.edit_message_text(
                "A’zolik tasdiqlandi. Endi kino kodini yuboring."
            )
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("Siz admin emassiz!", show_alert=True)
        return

    await query.answer()
    if query.data == "admin_stats":
        try:
            catalog = load_catalog()
        except (OSError, ValueError):
            await query.edit_message_text("Katalogni o‘qib bo‘lmadi.")
            return
        video_count = sum(
            isinstance(movie, dict) and movie.get("type") == "video"
            for movie in catalog.values()
        )
        link_count = len(catalog) - video_count
        await query.edit_message_text(
            f"Bot statistikasi:\n"
            f"Jami kino: {len(catalog)}\n"
            f"Video: {video_count}\n"
            f"Havola: {link_count}"
        )
    elif query.data == "admin_add_movie":
        context.user_data["awaiting_movie_add"] = True
        context.user_data.pop("awaiting_required_chat", None)
        context.user_data.pop("pending_movie_code", None)
        await query.edit_message_text(
            "Kino qo‘shish uchun:\n"
            "• Video captioni: birinchi qatorda kod, keyingi qatorda izoh;\n"
            "• Havola: birinchi qatorda kod va havola, keyingi qatorda izoh.\n"
            "Masalan: 101 https://example.com/video\n"
            "Kino haqida matn shu yerga yoziladi.\n\n"
            "Bekor qilish: /cancel"
        )
    elif query.data == "admin_set_required_chat":
        context.user_data["awaiting_required_chat"] = True
        context.user_data.pop("awaiting_movie_add", None)
        context.user_data.pop("pending_movie_code", None)
        await query.edit_message_text(
            "Majburiy kanal yoki guruhni sozlash:\n"
            "1-qatorga @username yoki manfiy chat ID yozing.\n"
            "2-qatorga qo‘shilish havolasini yozing.\n\n"
            "Masalan:\n"
            "@kinolar\n"
            "https://t.me/kinolar\n\n"
            "Yangi sozlama avvalgisini almashtiradi. "
            "Avval botni shu chatda admin qiling.\n"
            "Bekor qilish: /cancel"
        )


async def delete_movie(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete a movie code from the catalog; Telegram media itself remains."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if user.id != ADMIN_ID:
        await message.reply_text("Bu buyruq faqat admin uchun.")
        return

    if not context.args or not context.args[0].strip():
        await message.reply_text("Foydalanish: /del 101")
        return

    code = context.args[0].strip().upper()
    try:
        catalog = load_catalog()
    except (OSError, ValueError):
        logger.exception("Could not read catalog while deleting movie code")
        await message.reply_text("Kino katalogini o‘qib bo‘lmadi.")
        return

    if code not in catalog:
        await message.reply_text(f"{code} kodi katalogda topilmadi.")
        return

    del catalog[code]
    try:
        save_catalog(catalog)
    except OSError:
        logger.exception("Could not save catalog after deleting movie code")
        await message.reply_text("Kino kodini katalogdan o‘chirib bo‘lmadi.")
        return

    await message.reply_text(
        f"{code} kodi katalogdan o‘chirildi. Telegramdagi asl video o‘chmaydi."
    )


async def add_movie(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Add a movie using /add CODE URL or a replied-to/uploaded video."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if user.id != ADMIN_ID:
        await message.reply_text("Bu buyruq faqat admin uchun.")
        return

    args = context.args
    if not args:
        context.user_data["awaiting_movie_add"] = True
        context.user_data.pop("awaiting_required_chat", None)
        context.user_data.pop("pending_movie_code", None)
        await message.reply_text(
            "Videoni caption bilan yuboring: birinchi qatorda kod, "
            "keyingi qatorda izoh. Yoki kod va havolani, keyingi qatorda "
            "izohni yozing. Bekor qilish: /cancel"
        )
        return

    code = args[0].strip().upper()
    video = message.video
    video_caption = message.caption
    if video is None and message.reply_to_message is not None:
        video = message.reply_to_message.video
        video_caption = message.reply_to_message.caption

    if video is not None:
        _, description = parse_video_caption(video_caption, fallback_code=code)
        movie = {"type": "video", "file_id": video.file_id}
        if description:
            movie["description"] = description
        await store_movie(
            message,
            code,
            movie,
        )
        return

    if len(args) >= 2 and is_http_url(args[1]):
        movie = {"type": "link", "url": args[1]}
        description = " ".join(args[2:]).strip()
        if description:
            movie["description"] = description
        await store_movie(message, code, movie)
        return

    if len(args) == 1:
        context.user_data["awaiting_movie_add"] = True
        context.user_data["pending_movie_code"] = code
        await message.reply_text(
            f"{code} kodi tanlandi. Shu kodga video yuboring yoki "
            "havolani izohi bilan jo‘nating. Bekor qilish: /cancel"
        )
        return

    await message.reply_text(
        "Foydalanish: /add 101 https://example.com/video "
        "yoki videoga javob qilib /add 101 yuboring."
    )


async def store_movie(
    message,
    code: str,
    movie: dict[str, str],
) -> bool:
    """Save one movie entry and report success or failure to the admin."""
    try:
        catalog = load_catalog()
    except (OSError, ValueError):
        await message.reply_text("Kino katalogini o‘qib bo‘lmadi.")
        return False

    catalog[code] = movie
    try:
        save_catalog(catalog)
    except OSError:
        await message.reply_text("Kinoni katalogga saqlab bo‘lmadi.")
        return False

    confirmation = f"{code} kodi uchun kino saqlandi."
    description = movie.get("description")
    if isinstance(description, str) and description.strip():
        confirmation += f"\nIzoh:\n{description.strip()}"
    await message.reply_text(confirmation)
    return True


async def configure_required_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Set the single required channel or group from the admin panel."""
    message = update.effective_message
    if message is None or message.text is None:
        return False

    lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    if len(lines) != 2:
        await message.reply_text(
            "Ikkita qator yuboring:\n"
            "1-qator: @username yoki manfiy chat ID\n"
            "2-qator: https://t.me/ qo‘shilish havolasi"
        )
        return False

    try:
        chat_reference = parse_chat_reference(lines[0])
    except ValueError:
        await message.reply_text(
            "@username yoki manfiy Telegram chat ID kiriting."
        )
        return False

    join_url = lines[1]
    if not is_telegram_join_url(join_url):
        await message.reply_text(
            "Havola https://t.me/ yoki https://telegram.me/ bilan boshlanishi kerak."
        )
        return False

    try:
        chat = await context.bot.get_chat(chat_reference)
        bot_member = await context.bot.get_chat_member(
            chat_id=chat.id,
            user_id=context.bot.id,
        )
    except TelegramError:
        logger.exception("Could not validate required channel or group")
        await message.reply_text(
            "Chatni tekshirib bo‘lmadi. ID yoki @username’ni tekshiring "
            "va botni shu chatga admin qilib qo‘shing."
        )
        return False

    if getattr(bot_member, "status", None) not in {"creator", "administrator"}:
        await message.reply_text(
            "Bot bu kanal/guruhda admin emas. Botni admin qilib, qayta yuboring."
        )
        return False

    try:
        save_required_chat_config(chat.id, join_url)
    except OSError:
        await message.reply_text("Kanal/guruh sozlamasini saqlab bo‘lmadi.")
        return False

    context.user_data.pop("awaiting_required_chat", None)
    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None)
    await message.reply_text(
        f"Majburiy kanal/guruh saqlandi: {chat_name or 'chat'}.\n"
        "Bu sozlama avvalgisini almashtirdi."
    )
    return True


async def cancel_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancel an in-progress movie addition."""
    context.user_data.pop("awaiting_movie_add", None)
    context.user_data.pop("pending_movie_code", None)
    context.user_data.pop("awaiting_required_chat", None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("Amal bekor qilindi.")


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Accept pending admin additions or look up a movie code."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if context.user_data.get("awaiting_required_chat") and user.id == ADMIN_ID:
        await configure_required_chat(update, context)
        return

    if context.user_data.get("awaiting_movie_add") and user.id == ADMIN_ID:
        code_from_command = context.user_data.get("pending_movie_code")
        if message.video is not None:
            code, description = parse_video_caption(
                message.caption,
                fallback_code=code_from_command,
            )
            if not code:
                await message.reply_text(
                    "Captionning birinchi qatoriga kino kodini yozing."
                )
                return
            movie = {"type": "video", "file_id": message.video.file_id}
            if description:
                movie["description"] = description
            saved = await store_movie(
                message,
                code,
                movie,
            )
            if saved:
                context.user_data.pop("awaiting_movie_add", None)
                context.user_data.pop("pending_movie_code", None)
            return

        if message.text is None:
            return

        lines = message.text.strip().splitlines()
        first_line = lines[0].strip().split(maxsplit=2) if lines else []
        if code_from_command:
            code = code_from_command
            link_parts = lines[0].strip().split(maxsplit=1) if lines else []
            url = link_parts[0] if link_parts else ""
            description_lines = (
                ([link_parts[1]] if len(link_parts) > 1 else []) + lines[1:]
            )
        elif len(first_line) >= 2:
            code, url = first_line[0].upper(), first_line[1]
            description_lines = (
                ([first_line[2]] if len(first_line) > 2 else []) + lines[1:]
            )
        else:
            await message.reply_text(
                "Birinchi qatorda kod va havolani yozing; "
                "kino izohini keyingi qatorda kiriting."
            )
            return

        if not is_http_url(url):
            await message.reply_text("Havola http:// yoki https:// bilan boshlanishi kerak.")
            return
        movie = {"type": "link", "url": url}
        description = "\n".join(description_lines).strip()
        if description:
            movie["description"] = description
        saved = await store_movie(message, code, movie)
        if saved:
            context.user_data.pop("awaiting_movie_add", None)
            context.user_data.pop("pending_movie_code", None)
        return

    if message.text is None:
        return

    code = message.text.strip().upper()
    if not await require_channel_membership(update, context, code):
        return
    await deliver_movie(message, context, code)


async def deliver_movie(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
) -> None:
    """Send the requested video or link from the catalog."""
    try:
        catalog = load_catalog()
    except (OSError, ValueError):
        logger.exception("Catalog file contains invalid JSON")
        await message.reply_text("Kino katalogida xatolik bor. Admin bilan bog‘laning.")
        return

    movie = catalog.get(code.upper())
    if movie is None:
        await message.reply_text("Kechirasiz, bunday kodli kino topilmadi.")
    elif isinstance(movie, dict) and movie.get("type") == "video":
        file_id = movie.get("file_id")
        description = movie.get("description")
        description = description if isinstance(description, str) else ""
        if file_id:
            caption = description if len(description) <= 1024 else None
            await message.reply_video(
                video=file_id,
                supports_streaming=True,
                caption=caption,
            )
            if description and caption is None:
                await message.reply_text(description)
        else:
            await message.reply_text("Bu kino videosi katalogda noto‘g‘ri saqlangan.")
    elif isinstance(movie, dict) and movie.get("type") == "link":
        url = movie.get("url")
        if url:
            description = movie.get("description")
            description = description if isinstance(description, str) else ""
            text = f"{description}\n\n{url}" if description else url
            await message.reply_text(text, disable_web_page_preview=False)
        else:
            await message.reply_text("Bu kino havolasi katalogda noto‘g‘ri saqlangan.")
    else:
        # Older catalog entries stored the link as a plain string.
        await message.reply_text(str(movie), disable_web_page_preview=False)


def main() -> None:
    """Configure and run the Telegram bot."""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Xatolik: Replit Secrets'da BOT_TOKEN topilmadi!")
    if ADMIN_ID == 0:
        logger.warning("ADMIN_ID sozlanmagan; admin panelidan foydalanib bo‘lmaydi.")

    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("add", add_movie))
    application.add_handler(CommandHandler("del", delete_movie))
    application.add_handler(CommandHandler("cancel", cancel_add))
    application.add_handler(CallbackQueryHandler(admin_callback))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.VIDEO) & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Bot ishga tushdi.")
    application.run_polling()


if __name__ == "__main__":
    main()
    