import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")
lock = asyncio.Lock()
PENDING = "❌ Не отвечено"
GREETING = (
    "<b>💘 Привет</b>\n\n"
    "<b>Напиши прямо сюда в бота сообщение</b> и ответ придёт в ближайшее "
    "время или будет опубликован в канале @FemboyNani\n\n"
    "☑️Можно отправлять только текст"
)


class Storage:
    def __init__(self, url):
        self.url = url
        if url:
            import psycopg

            self.db = psycopg.connect(url, autocommit=True, connect_timeout=10)
        else:
            self.db = sqlite3.connect(Path(__file__).with_name("bot.sqlite3"),
                                      isolation_level=None)

        self.run("""
            CREATE TABLE IF NOT EXISTS questions (
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                answered_by TEXT,
                PRIMARY KEY (user_id, source_id)
            )
        """)
        self.run("""
            CREATE TABLE IF NOT EXISTS deliveries (
                admin_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                source_id BIGINT NOT NULL,
                part INTEGER NOT NULL,
                status_text TEXT,
                PRIMARY KEY (admin_id, message_id),
                UNIQUE (admin_id, user_id, source_id, part)
            )
        """)
        self.run("""
            CREATE TABLE IF NOT EXISTS handled_messages (
                bot_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                PRIMARY KEY (bot_id, chat_id, message_id)
            )
        """)

    def run(self, sql, *args, fetch=False):
        if self.url:
            import psycopg

            if self.db.closed:
                self.db = psycopg.connect(self.url, autocommit=True, connect_timeout=10)
            with self.db.cursor() as cursor:
                cursor.execute(sql.replace("?", "%s"), args)
                return cursor.fetchall() if fetch else None
        cursor = self.db.execute(sql, args)
        return cursor.fetchall() if fetch else None


class IgnoreRepeatedMessages(BaseMiddleware):
    async def __call__(self, handler, event, data):
        storage = data["question_store"]
        key = (data["bot"].id, event.chat.id, event.message_id)
        claimed = storage.run(
            "INSERT INTO handled_messages (bot_id, chat_id, message_id) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING RETURNING message_id",
            *key, fetch=True,
        )
        if not claimed:
            logger.info("Повторная доставка сообщения пропущена.")
            return
        try:
            return await handler(event, data)
        except Exception:
            storage.run(
                "DELETE FROM handled_messages WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
                *key,
            )
            raise


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(GREETING, parse_mode="HTML")


@router.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Твой Telegram ID: {message.chat.id}")


async def setup_start(message: Message):
    await message.answer("Бот пока настраивается. Чтобы узнать свой Telegram ID, отправь /id.")


def question_parts(text):
    # Оставляем место для статуса, учитывая эмодзи и лимит Telegram.
    if len(text.encode("utf-16-le")) // 2 <= 4000:
        return [text]
    return [text[i:i + 1900] for i in range(0, len(text), 1900)]


async def submit(message, bot, storage, admins):
    key = (message.chat.id, message.message_id)
    storage.run("INSERT INTO questions (user_id, source_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING", *key)
    [(answered_by,)] = storage.run(
        "SELECT answered_by FROM questions WHERE user_id = ? AND source_id = ?",
        *key, fetch=True,
    )
    status = f"✅ Отвечено {answered_by}" if answered_by else PENDING
    parts = question_parts(message.text)
    sent_count = 0
    for admin in admins:
        existing = storage.run(
            "SELECT part FROM deliveries WHERE admin_id = ? AND user_id = ? AND source_id = ?",
            admin, *key, fetch=True,
        )
        delivered_parts = {part for (part,) in existing}
        try:
            for index, part in enumerate(parts):
                if index in delivered_parts:
                    continue
                status_text = part if index == len(parts) - 1 else None
                text = f"{part}\n\n{status}" if status_text is not None else part
                sent = await bot.send_message(admin, text)
                storage.run(
                    "INSERT INTO deliveries "
                    "(admin_id, message_id, user_id, source_id, part, status_text) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    admin, sent.message_id, *key, index, status_text,
                )
        except TelegramAPIError as error:
            logger.warning("Не удалось доставить вопрос администратору %s (%s)",
                           admin, type(error).__name__)
        else:
            sent_count += 1

    if sent_count:
        await message.answer("Спасибо! Твой вопрос отправлен.")
    else:
        await message.answer("Не удалось отправить вопрос. Попробуй чуть позже.")


async def reply(message, bot, storage):
    rows = storage.run(
        "SELECT user_id, source_id FROM deliveries WHERE admin_id = ? AND message_id = ?",
        message.chat.id, message.reply_to_message.message_id, fetch=True,
    )
    if not rows:
        await message.answer("Ответь через Reply на сообщение с вопросом.")
        return

    [key] = rows
    [(answered_by,)] = storage.run(
        "SELECT answered_by FROM questions WHERE user_id = ? AND source_id = ?",
        *key, fetch=True,
    )
    already_answered = answered_by is not None
    if not already_answered:
        if not message.text:
            await message.answer("Ответ можно отправить только текстом.")
            return
        try:
            await bot.send_message(key[0], message.text)
        except TelegramAPIError:
            await message.answer("Не удалось доставить ответ. Вопрос остаётся открытым.")
            return
        answered_by = (
            f"@{message.from_user.username}" if message.from_user.username
            else f"ID {message.chat.id}"
        )
        storage.run(
            "UPDATE questions SET answered_by = ? WHERE user_id = ? AND source_id = ?",
            answered_by, *key,
        )

    status_updated = True
    for admin, message_id, text in storage.run(
        "SELECT admin_id, message_id, status_text FROM deliveries "
        "WHERE user_id = ? AND source_id = ? AND status_text IS NOT NULL",
        *key, fetch=True,
    ):
        try:
            await bot.edit_message_text(
                chat_id=admin, message_id=message_id,
                text=f"{text}\n\n✅ Отвечено {answered_by}",
            )
        except TelegramAPIError as error:
            if isinstance(error, TelegramBadRequest) and "message is not modified" in error.message:
                continue
            logger.warning("Не удалось обновить статус в чате %s (%s)",
                           admin, type(error).__name__)
            status_updated = False

    if not status_updated:
        await message.answer("Ответ доставлен. Не все статусы удалось обновить.")
    elif already_answered:
        await message.answer(f"На этот вопрос уже ответил {answered_by}.")


@router.message()
async def handle_message(message: Message, bot: Bot, question_store: Storage, admins: tuple):
    async with lock:
        if message.chat.id in admins:
            if message.reply_to_message:
                await reply(message, bot, question_store)
            else:
                await message.answer("Ответь через Reply на сообщение с вопросом.")
        elif message.text:
            await submit(message, bot, question_store, admins)
        else:
            await message.answer("☑️Можно отправлять только текст")


def webhook_app(bot, dispatcher, url, secret):
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    async def health(request):
        return web.Response(text="OK")

    async def register_webhook(bot):
        await bot.set_webhook(
            f"{url.rstrip('/')}/telegram", secret_token=secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )

    dispatcher.startup.register(register_webhook)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(
        dispatcher, bot, secret_token=secret, handle_in_background=False,
    ).register(app, path="/telegram")
    setup_application(app, dispatcher, bot=bot)
    return app


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажи BOT_TOKEN в .env или в переменных окружения.")

    # Без ADMIN_IDS можно запустить бота локально, чтобы узнать ID через /id.
    raw_ids = os.getenv("ADMIN_IDS", "")
    try:
        admins = tuple(int(item.strip()) for item in raw_ids.split(",") if item.strip())
    except ValueError:
        raise SystemExit("Укажи два числовых Telegram ID через запятую в ADMIN_IDS.") from None
    url = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    database_url = os.getenv("DATABASE_URL")
    secret = os.getenv("WEBHOOK_SECRET")
    if (raw_ids or url) and (len(set(admins)) != 2 or len(admins) != 2 or min(admins) < 1):
        raise SystemExit("Укажи два разных числовых Telegram ID в ADMIN_IDS.")
    if url and (not database_url or not secret):
        raise SystemExit("Для вебхука задай DATABASE_URL и WEBHOOK_SECRET.")

    dispatcher = Dispatcher()
    bot = Bot(token)
    storage = Storage(database_url)
    dispatcher["question_store"] = storage
    dispatcher.message.middleware(IgnoreRepeatedMessages())
    if admins:
        dispatcher["admins"] = admins
        dispatcher.include_router(router)
    else:
        dispatcher.message.register(setup_start, CommandStart(), F.chat.type == "private")
        dispatcher.message.register(show_id, Command("id"), F.chat.type == "private")
        logger.info("Режим настройки. Попроси обоих администраторов отправить /id.")

    async def polling():
        async with bot.session:
            await bot.delete_webhook()
            await dispatcher.start_polling(
                bot, close_bot_session=False, handle_as_tasks=False,
            )

    try:
        if url:
            from aiohttp import web

            web.run_app(webhook_app(bot, dispatcher, url, secret),
                        host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                        access_log=None)
        else:
            asyncio.run(polling())
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram отклонил токен. Проверь BOT_TOKEN в .env.") from None
    except TelegramNetworkError:
        raise SystemExit(
            "Не удалось подключиться к Telegram API (api.telegram.org:443). "
            "Проверь доступ к этому адресу с компьютера, на котором запускаешь бота."
        ) from None
    finally:
        storage.db.close()


if __name__ == "__main__":
    main()
