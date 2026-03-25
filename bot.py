import os
import sqlite3
import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)

DB_PATH = "bot.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    conn.commit()
    conn.close()


def save_member(chat_id: int, user_id: int, username: str | None,
                first_name: str | None, last_name: str | None, is_active: int = 1):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO members (chat_id, user_id, username, first_name, last_name, is_active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            is_active=excluded.is_active
    """, (chat_id, user_id, username, first_name, last_name, is_active))

    conn.commit()
    conn.close()


def deactivate_member(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE members
        SET is_active = 0
        WHERE chat_id = ? AND user_id = ?
    """, (chat_id, user_id))
    conn.commit()
    conn.close()


def get_active_members(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, first_name, last_name
        FROM members
        WHERE chat_id = ? AND is_active = 1
        ORDER BY COALESCE(first_name, username, '') COLLATE NOCASE
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def build_mention(user_id: int, username: str | None, first_name: str | None, last_name: str | None) -> str:
    if username:
        label = f"@{username}"
    else:
        full_name = " ".join(x for x in [first_name, last_name] if x).strip()
        label = full_name or f"user_{user_id}"

    return f'<a href="tg://user?id={user_id}">{escape(label)}</a>'


async def register_user_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if user.is_bot:
        return

    save_member(
        chat_id=chat.id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=1,
    )


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return

    chat = cmu.chat
    user = cmu.new_chat_member.user
    new_status = cmu.new_chat_member.status

    if chat.type not in ("group", "supergroup"):
        return

    if user.is_bot:
        return

    active_statuses = {"member", "administrator", "creator", "restricted"}

    if new_status in active_statuses:
        save_member(
            chat_id=chat.id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            is_active=1,
        )
    else:
        deactivate_member(chat.id, user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Я работаю.\n\n"
            "Команды:\n"
            "/all текст — отметить всех в группе\n"
            "/list — показать, кого я знаю в этой группе"
        )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message

    if not chat or not message:
        return

    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Эта команда работает только в группе.")
        return

    members = get_active_members(chat.id)
    if not members:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Пока список пуст. Пусть кто-нибудь напишет сообщение в группу."
        )
        return

    mentions = [
        build_mention(user_id, username, first_name, last_name)
        for user_id, username, first_name, last_name in members
    ]

    text = "Я знаю таких участников:\n" + "\n".join(f"• {m}" for m in mentions)
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message

    if not chat or not message:
        return

    if chat.type not in ("group", "supergroup"):
        await context.bot.send_message(
            chat_id=chat.id,
            text="Эта команда работает только в группе."
        )
        return

    members = get_active_members(chat.id)
    if not members:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Я пока никого не знаю. Пусть участники сначала напишут что-нибудь в чат."
        )
        return

    raw_text = message.text or ""
    payload = raw_text[len("/all"):].strip() if raw_text.startswith("/all") else ""

    mentions = [
        build_mention(user_id, username, first_name, last_name)
        for user_id, username, first_name, last_name in members
    ]

    header = " ".join(mentions)
    final_text = header if not payload else f"{header} {escape(payload)}"

    max_len = 3500
    if len(final_text) <= max_len:
        await context.bot.send_message(
            chat_id=chat.id,
            text=final_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    else:
        chunks = []
        current = ""

        for mention in mentions:
            candidate = (current + " " + mention).strip()
            if len(candidate) > max_len:
                if current:
                    chunks.append(current)
                current = mention
            else:
                current = candidate

        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1 and payload:
                chunk = f"{chunk} {escape(payload)}"

            await context.bot.send_message(
                chat_id=chat.id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

    # Удаляем команду пользователя
    try:
        await message.delete()
    except (Forbidden, BadRequest):
        # Если у бота нет права удалять сообщения — просто пропускаем
        pass


def main():
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("all", all_cmd))

    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, register_user_from_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
