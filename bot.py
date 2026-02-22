import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from openai import AsyncOpenAI


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    bot_token: str
    support_chat_id: int
    ai_enabled: bool
    ai_api_key: str
    ai_model: str
    ai_base_url: Optional[str]


class Storage:
    def __init__(self, path: str = "support_bot.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_map (
                user_id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                full_name TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.commit()

    def get_topic_id(self, user_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT topic_id FROM topic_map WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["topic_id"]) if row else None

    def get_user_id(self, topic_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT user_id FROM topic_map WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return int(row["user_id"]) if row else None

    def save_mapping(self, user_id: int, topic_id: int, username: str, full_name: str) -> None:
        self.conn.execute(
            """
            INSERT INTO topic_map (user_id, topic_id, username, full_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                topic_id=excluded.topic_id,
                username=excluded.username,
                full_name=excluded.full_name
            """,
            (user_id, topic_id, username, full_name),
        )
        self.conn.commit()


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "")
    support_chat_id_raw = os.getenv("SUPPORT_CHAT_ID", "")

    if not token:
        raise ValueError("BOT_TOKEN is required")
    if not support_chat_id_raw:
        raise ValueError("SUPPORT_CHAT_ID is required")

    return Config(
        bot_token=token,
        support_chat_id=int(support_chat_id_raw),
        ai_enabled=os.getenv("AI_ENABLED", "true").lower() == "true",
        ai_api_key=os.getenv("AI_API_KEY", ""),
        ai_model=os.getenv("AI_MODEL", "gpt-4o-mini"),
        ai_base_url=os.getenv("AI_BASE_URL") or None,
    )


router = Router()


class SupportBotService:
    def __init__(self, config: Config, storage: Storage, bot: Bot) -> None:
        self.config = config
        self.storage = storage
        self.bot = bot
        self.ai_client: Optional[AsyncOpenAI] = None

        if config.ai_enabled and config.ai_api_key:
            self.ai_client = AsyncOpenAI(api_key=config.ai_api_key, base_url=config.ai_base_url)

    async def ensure_user_topic(self, message: Message) -> int:
        assert message.from_user is not None
        user = message.from_user

        existing_topic = self.storage.get_topic_id(user.id)
        if existing_topic:
            return existing_topic

        title = f"{user.full_name} ({user.id})"
        topic = await self.bot.create_forum_topic(
            chat_id=self.config.support_chat_id,
            name=title[:128],
        )

        topic_id = topic.message_thread_id
        self.storage.save_mapping(
            user_id=user.id,
            topic_id=topic_id,
            username=user.username or "",
            full_name=user.full_name,
        )
        return topic_id

    async def relay_user_message_to_support(self, message: Message) -> None:
        topic_id = await self.ensure_user_topic(message)

        await self.bot.copy_message(
            chat_id=self.config.support_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=topic_id,
        )

        assert message.from_user is not None
        contact_text = (
            f"👤 Пользователь: {message.from_user.full_name} (@{message.from_user.username or 'нет'})\n"
            f"🆔 ID: <code>{message.from_user.id}</code>"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть диалог",
                        url=f"tg://user?id={message.from_user.id}",
                    )
                ]
            ]
        )
        await self.bot.send_message(
            chat_id=self.config.support_chat_id,
            message_thread_id=topic_id,
            text=contact_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def relay_support_message_to_user(self, message: Message) -> None:
        if message.message_thread_id is None:
            return

        user_id = self.storage.get_user_id(message.message_thread_id)
        if not user_id:
            return

        if message.from_user and message.from_user.is_bot:
            return

        await self.bot.copy_message(
            chat_id=user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )

    async def ai_reply(self, prompt: str) -> str:
        if not self.ai_client:
            return "AI недоступен. Укажите AI_API_KEY в .env"

        result = await self.ai_client.chat.completions.create(
            model=self.config.ai_model,
            messages=[
                {
                    "role": "system",
                    "content": "Ты помощник первой линии технической поддержки. Отвечай кратко и по делу.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return result.choices[0].message.content or "Нет ответа от AI"


service: Optional[SupportBotService] = None


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Привет! Это бот техподдержки.\n"
        "Отправьте сообщение, фото, документ, аудио или любой файл — мы передадим в поддержку.\n"
        "Для ответа ИИ используйте: /ai ваш вопрос"
    )


@router.message(Command("ai"))
async def on_ai(message: Message) -> None:
    global service
    if service is None:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /ai <ваш вопрос>")
        return

    await message.answer("🤖 Думаю...")
    answer = await service.ai_reply(parts[1])
    await message.answer(answer)


@router.message(F.chat.type == "private")
async def on_private_message(message: Message) -> None:
    global service
    if service is None:
        return

    if message.text and message.text.startswith("/ai"):
        return

    await service.relay_user_message_to_support(message)
    await message.answer("✅ Ваше сообщение передано в техподдержку.")


@router.message(F.message_thread_id.is_not(None))
async def on_support_group_message(message: Message) -> None:
    global service
    if service is None:
        return

    if message.chat.id != service.config.support_chat_id:
        return

    await service.relay_support_message_to_user(message)


async def main() -> None:
    global service
    config = load_config()

    bot = Bot(token=config.bot_token)
    storage = Storage()
    service = SupportBotService(config=config, storage=storage, bot=bot)

    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
