import asyncio
import logging
import os
import random
import string
import sys
import time
import json
from datetime import datetime, timedelta
from typing import Union, List, Optional, Any, Dict, Final

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.exceptions import (
    TelegramForbiddenError, 
    TelegramRetryAfter, 
    TelegramBadRequest,
    TelegramNetworkError
)
from aiogram.types import (
    Message, 
    CallbackQuery, 
    BotCommand,
    ReactionTypeEmoji,
    BufferedInputFile,
    URLInputFile,
    ContentType
)

# --- 1. ИНИЦИАЛИЗАЦИЯ И ЛОГИРОВАНИЕ ---
load_dotenv()

logger = logging.getLogger("SpokElite_v15")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(formatter)
logger.addHandler(sh)

fh = logging.FileHandler("bot_v15_core.log", encoding='utf-8')
fh.setFormatter(formatter)
logger.addHandler(fh)

# Загрузка конфигурации
BOT_TOKEN: Final = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID: Final = int(os.getenv("ADMIN_GROUP_ID", 0))
REVIEWS_TOPIC_ID: Final = int(os.getenv("REVIEWS_TOPIC_ID", 0))
OWNER_ID: Final = int(os.getenv("OWNER_ID", 0))
START_PHOTO_URL: Final = os.getenv("START_PHOTO_URL", "https://images.unsplash.com/photo-1611224923853-80b023f02d71?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80")
DB_NAME: Final = "spok_v15_final.db"

if not BOT_TOKEN:
    logger.critical("Брат, добавь BOT_TOKEN в .env файл!")
    sys.exit(1)

# --- 2. УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ ---
class DatabaseManager:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self):
        """Создание таблиц с проверкой существующих колонок"""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            
            # Создание таблиц, если их нет
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    anon_id TEXT UNIQUE,
                    topic_id INTEGER UNIQUE,
                    referrer_id INTEGER,
                    warns INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    ban_until DATETIME,
                    ban_reason TEXT,
                    is_active INTEGER DEFAULT 1,
                    msg_count INTEGER DEFAULT 0,
                    created_at DATETIME,
                    last_seen DATETIME
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    admin_alias TEXT,
                    rating INTEGER,
                    comment TEXT,
                    created_at DATETIME
                );
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    admin_id INTEGER,
                    action TEXT,
                    details TEXT,
                    created_at DATETIME
                );
                CREATE TABLE IF NOT EXISTS warns_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    admin_id INTEGER,
                    reason TEXT,
                    created_at DATETIME
                );
                CREATE TABLE IF NOT EXISTS broadcast_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    message_type TEXT,
                    content TEXT,
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    created_at DATETIME
                );
            """)
            
            # Проверяем и добавляем отсутствующие колонки
            await self._migrate_database(db)
            
            await db.commit()
            logger.info("DB: Система данных v15 готова.")

    async def _migrate_database(self, db):
        """Миграция базы данных - добавление отсутствующих колонок"""
        # Получаем информацию о таблице users
        async with db.execute("PRAGMA table_info(users)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        
        # Добавляем отсутствующие колонки
        missing_columns = []
        
        if 'last_seen' not in columns:
            missing_columns.append("last_seen")
            await db.execute("ALTER TABLE users ADD COLUMN last_seen DATETIME")
        
        if 'ban_until' not in columns:
            missing_columns.append("ban_until")
            await db.execute("ALTER TABLE users ADD COLUMN ban_until DATETIME")
        
        if 'ban_reason' not in columns:
            missing_columns.append("ban_reason")
            await db.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
        
        if missing_columns:
            logger.info(f"DB: Добавлены колонки: {', '.join(missing_columns)}")

    async def register(self, uid: int, rid: int = None):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,)) as c:
                if not await c.fetchone():
                    aid = "USER-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
                    await db.execute("""
                        INSERT INTO users (user_id, anon_id, referrer_id, created_at, last_seen) 
                        VALUES (?, ?, ?, ?, ?)
                    """, (uid, aid, rid, datetime.now(), datetime.now()))
                    await db.commit()
                else:
                    await db.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", 
                                   (datetime.now(), uid))
                    await db.commit()

    async def get_user(self, uid: int = None, tid: int = None):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM users WHERE user_id = ?" if uid else "SELECT * FROM users WHERE topic_id = ?"
            async with db.execute(q, (uid or tid,)) as c:
                r = await c.fetchone()
                return dict(r) if r else None

    async def add_warn(self, uid: int, admin_id: int, reason: str = None) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET warns = warns + 1 WHERE user_id = ?", (uid,))
            await db.execute("""
                INSERT INTO warns_history (user_id, admin_id, reason, created_at) 
                VALUES (?, ?, ?, ?)
            """, (uid, admin_id, reason, datetime.now()))
            await db.commit()
            async with db.execute("SELECT warns FROM users WHERE user_id = ?", (uid,)) as c:
                res = await c.fetchone()
                return res[0]

    async def get_active_users_count(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE is_active = 1") as c:
                return (await c.fetchone())[0]

    async def get_today_users(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("""
                SELECT COUNT(*) FROM users 
                WHERE DATE(created_at) = DATE('now')
            """) as c:
                return (await c.fetchone())[0]

    async def get_avg_messages(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT AVG(msg_count) FROM users WHERE msg_count > 0") as c:
                return (await c.fetchone())[0] or 0

    async def get_top_referrers(self, limit=5):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT referrer_id, COUNT(*) as count 
                FROM users 
                WHERE referrer_id IS NOT NULL 
                GROUP BY referrer_id 
                ORDER BY count DESC 
                LIMIT ?
            """, (limit,)) as c:
                return await c.fetchall()

    async def get_daily_stats(self, days=7):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT 
                    DATE(created_at) as date,
                    COUNT(*) as registrations,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active
                FROM users 
                WHERE created_at >= DATE('now', ?)
                GROUP BY DATE(created_at)
                ORDER BY date
            """, (f'-{days} days',)) as c:
                return await c.fetchall()

db_engine = DatabaseManager(DB_NAME)

# --- 3. FSM И ЗАЩИТНАЯ МИДЛВАРЯ ---
class BotStates(StatesGroup):
    choosing_category = State()
    writing_issue = State()
    rev_adm = State()
    rev_rate = State()
    rev_msg = State()
    broadcasting = State()
    broadcast_confirm = State()

class GuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if not event.from_user or event.chat.type != "private":
            return await handler(event, data)
        
        await db_engine.register(event.from_user.id)
        u = await db_engine.get_user(uid=event.from_user.id)
        
        if u and u['is_banned']:
            if u['ban_until']:
                ban_time = datetime.fromisoformat(u['ban_until']) if u['ban_until'] else None
                if ban_time and ban_time < datetime.now():
                    # Разбан по времени
                    async with aiosqlite.connect(DB_NAME) as db:
                        await db.execute("""
                            UPDATE users SET is_banned = 0, ban_until = NULL, ban_reason = NULL 
                            WHERE user_id = ?
                        """, (event.from_user.id,))
                        await db.commit()
                elif ban_time:
                    remaining = ban_time - datetime.now()
                    hours = int(remaining.total_seconds() // 3600)
                    minutes = int((remaining.total_seconds() % 3600) // 60)
                    
                    reason_text = f"\nПричина: {u['ban_reason']}" if u['ban_reason'] else ""
                    
                    await event.answer(
                        f"🚫 Вы заблокированы!\n"
                        f"До разблока осталось: {hours}ч {minutes}м{reason_text}"
                    )
                    return
            else:
                await event.answer("🚫 Вы заблокированы навсегда!")
                return
        
        return await handler(event, data)

# --- 4. КЛАВИАТУРЫ ---
def get_main_kb():
    b = ReplyKeyboardBuilder()
    b.row(types.KeyboardButton(text="🆘 Создать обращение"))
    b.row(types.KeyboardButton(text="⭐️ Оставить отзыв"), types.KeyboardButton(text="📊 Стена отзывов"))
    b.row(types.KeyboardButton(text="👤 Мой профиль"))
    return b.as_markup(resize_keyboard=True)

def get_categories_kb():
    b = InlineKeyboardBuilder()
    categories = ["🛠 Тех. вопрос", "💬 Общение", "💰 Поддержка", "📱 Другое"]
    for cat in categories:
        b.button(text=cat, callback_data=f"cat_{cat}")
    b.adjust(2)
    return b.as_markup()

def get_cancel_kb():
    b = ReplyKeyboardBuilder()
    b.add(types.KeyboardButton(text="❌ Отмена"))
    return b.as_markup(resize_keyboard=True)

def get_admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="admin_stats")
    b.button(text="📢 Рассылка", callback_data="admin_broadcast")
    b.button(text="📁 Экспорт", callback_data="admin_export")
    b.button(text="🔄 Очистить кэш", callback_data="admin_clear_cache")
    b.adjust(2)
    return b.as_markup()

# --- 5. УТИЛИТЫ И ЭФФЕКТЫ ---
async def send_with_typing(chat_id: int, text: str, bot: Bot, 
                          parse_mode: str = "HTML", 
                          reply_markup: types.ReplyKeyboardMarkup = None,
                          delay: float = 0.05):
    """Отправка сообщения с эффектом печатания"""
    try:
        # Включаем статус "печатает"
        await bot.send_chat_action(chat_id, "typing")
        
        # Задержка для имитации печати
        await asyncio.sleep(min(len(text) * 0.03, 2.0))  # Не более 2 секунд
        
        # Отправляем сообщение
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in send_with_typing: {e}")
        # Если ошибка, отправляем без эффекта
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )

async def send_photo_with_typing(chat_id: int, photo_url: str, caption: str, bot: Bot,
                               parse_mode: str = "HTML",
                               reply_markup: types.ReplyKeyboardMarkup = None):
    """Отправка фото с эффектом печатания"""
    try:
        # Включаем статус "загружает фото"
        await bot.send_chat_action(chat_id, "upload_photo")
        await asyncio.sleep(1)  # Задержка для имитации загрузки
        
        # Отправляем фото
        photo = URLInputFile(photo_url)
        return await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error sending photo: {e}")
        # Если не удалось отправить фото, отправляем только текст
        return await send_with_typing(chat_id, caption, bot, parse_mode, reply_markup)

def parse_time(time_str: str) -> Optional[timedelta]:
    """Парсит время из строки типа '1d', '2h', '30m'"""
    time_str = time_str.lower()
    
    if time_str == "перманентно":
        return None  # Перманентный бан
    
    multipliers = {
        'd': 86400,  # дни
        'h': 3600,   # часы
        'm': 60,     # минуты
        's': 1       # секунды
    }
    
    total_seconds = 0
    num = ''
    
    for char in time_str:
        if char.isdigit():
            num += char
        elif char in multipliers:
            if num:
                total_seconds += int(num) * multipliers[char]
                num = ''
        else:
            return None
    
    return timedelta(seconds=total_seconds)

def format_timedelta(td: timedelta) -> str:
    """Форматирует timedelta в читаемый вид"""
    if td is None:
        return "навсегда"
    
    total_seconds = int(td.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}д")
    if hours > 0:
        parts.append(f"{hours}ч")
    if minutes > 0:
        parts.append(f"{minutes}м")
    
    return ' '.join(parts) if parts else "0м"

# --- 6. ЛОГИКА ТИКЕТОВ И ФОРУМА ---
async def init_ticket(uid: int, bot: Bot, category: str):
    user = await db_engine.get_user(uid=uid)
    if not user: return None
    
    if user['topic_id']:
        return user['topic_id']
    
    try:
        topic = await bot.create_forum_topic(
            ADMIN_GROUP_ID, 
            f"{category} | {user['anon_id']}"
        )
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET topic_id = ? WHERE user_id = ?", (topic.message_thread_id, uid))
            await db.commit()
            
        ticket_card = (
            f"🚀 <b>НОВАЯ ЗАЯВКА В ПОДДЕРЖКУ</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📁 Категория: <b>{category}</b>\n"
            f"👤 Клиент: <code>{user['anon_id']}</code>\n"
            f"🆔 User ID: <code>{uid}</code>\n"
            f"📊 Сообщений: {user['msg_count']}\n"
            f"⚠️ Варны: {user['warns']}/3\n"
            f"━━━━━━━━━━━━━━\n"
            f"📢 @all Администраторы, сформировано новое обращение!"
        )
        await bot.send_message(ADMIN_GROUP_ID, ticket_card, 
                             message_thread_id=topic.message_thread_id, 
                             parse_mode="HTML")
        return topic.message_thread_id
    except Exception as e:
        logger.error(f"Ticket Init Error: {e}")
        return None

# --- 7. УЛУЧШЕННЫЙ GATEWAY С РЕАКЦИЯМИ ---
async def safe_set_reaction(bot: Bot, chat_id: int, message_id: int, emoji: str):
    """Безопасная установка реакции с обработкой ошибок"""
    try:
        # Используем только базовые эмодзи, которые точно поддерживаются как реакции
        supported_emojis = ["👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", 
                          "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
                          "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
                          "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
                          "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
                          "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
                          "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
                          "🤷", "🤷‍♀", "😡"]
        
        # Проверяем, поддерживается ли эмодзи
        if emoji not in supported_emojis:
            # Используем базовые эмодзи как запасной вариант
            emoji = "👍" if emoji in ["✅", "📨", "👤"] else "👎" if emoji in ["❌", "🚫"] else "👍"
        
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)]
        )
        return True
    except TelegramBadRequest as e:
        if "REACTION_INVALID" in str(e):
            logger.warning(f"Invalid reaction emoji: {emoji}")
        elif "message to set reaction not found" in str(e):
            logger.warning(f"Message not found for reaction: {chat_id}/{message_id}")
        else:
            logger.error(f"BadRequest setting reaction: {e}")
        return False
    except Exception as e:
        logger.error(f"Error setting reaction: {e}")
        return False

async def send_message_to_admin(bot: Bot, user_id: int, message: Message, topic_id: int):
    """Отправка сообщения от пользователя админу"""
    try:
        # Получаем информацию о пользователе для форматирования
        user = await db_engine.get_user(uid=user_id)
        if not user:
            return None
        
        # Форматируем сообщение в зависимости от типа контента
        if message.text:
            # Текстовое сообщение
            text_content = message.html_text if hasattr(message, 'html_text') else message.text
            formatted_text = f"👤 <b>{user['anon_id']}</b>\n━━━━━━━━━━━━━━\n{text_content}"
            sent_msg = await bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=formatted_text,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        elif message.photo:
            # Фото с подписью
            caption_content = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            caption = f"👤 <b>{user['anon_id']}</b>\n━━━━━━━━━━━━━━\n{caption_content or ''}"
            sent_msg = await bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=message.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML" if caption_content else None,
                message_thread_id=topic_id
            )
            return sent_msg
            

        elif message.video:
            # Видео
            caption = f"👤 <b>{user['anon_id']}</b>\n━━━━━━━━━━━━━━\n{message.caption or ''}"
            sent_msg = await bot.send_video(
                chat_id=ADMIN_GROUP_ID,
                video=message.video.file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        elif message.document:
            # Документ
            caption = f"👤 <b>{user['anon_id']}</b>\n━━━━━━━━━━━━━━\n{message.caption or ''}"
            sent_msg = await bot.send_document(
                chat_id=ADMIN_GROUP_ID,
                document=message.document.file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        elif message.audio:
            # Аудио
            caption = f"👤 <b>{user['anon_id']}</b>\n━━━━━━━━━━━━━━\n{message.caption or ''}"
            sent_msg = await bot.send_audio(
                chat_id=ADMIN_GROUP_ID,
                audio=message.audio.file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        elif message.voice:
            # Голосовое сообщение
            caption = f"👤 <b>{user['anon_id']}</b> (голосовое сообщение)"
            sent_msg = await bot.send_voice(
                chat_id=ADMIN_GROUP_ID,
                voice=message.voice.file_id,
                caption=caption,
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        elif message.sticker:
            # Стикер
            sent_msg = await bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=f"👤 <b>{user['anon_id']}</b> отправил стикер",
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
        else:
            # Другие типы контента
            content_type = str(message.content_type).replace("ContentType.", "")
            sent_msg = await bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=f"👤 <b>{user['anon_id']}</b>\n📎 Тип контента: {content_type}\n{message.caption or ''}",
                parse_mode="HTML",
                message_thread_id=topic_id
            )
            return sent_msg
            
    except Exception as e:
        logger.error(f"Error sending message to admin: {e}")
        return None

async def send_message_to_user(bot: Bot, user_id: int, message: Message):
    """Отправка сообщения от админа пользователю"""
    try:
        if message.text:
            # Текстовое сообщение - используем html_text для сохранения форматирования
            text_to_send = message.html_text if hasattr(message, 'html_text') else message.text
            sent_msg = await bot.send_message(
                chat_id=user_id,
                text=text_to_send,
                parse_mode="HTML" if hasattr(message, 'html_text') else None
            )
            return sent_msg
            
        elif message.photo:
            # Фото с подписью
            caption = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            sent_msg = await bot.send_photo(
                chat_id=user_id,
                photo=message.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML" if hasattr(message, 'html_text') and message.caption else None
            )
            return sent_msg
            
        elif message.video:
            # Видео
            caption = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            sent_msg = await bot.send_video(
                chat_id=user_id,
                video=message.video.file_id,
                caption=caption,
                parse_mode="HTML" if hasattr(message, 'html_text') and message.caption else None
            )
            return sent_msg
            
        elif message.document:
            # Документ
            caption = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            sent_msg = await bot.send_document(
                chat_id=user_id,
                document=message.document.file_id,
                caption=caption,
                parse_mode="HTML" if hasattr(message, 'html_text') and message.caption else None
            )
            return sent_msg
            
        elif message.audio:
            # Аудио
            caption = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            sent_msg = await bot.send_audio(
                chat_id=user_id,
                audio=message.audio.file_id,
                caption=caption,
                parse_mode="HTML" if hasattr(message, 'html_text') and message.caption else None
            )
            return sent_msg
            
        elif message.voice:
            # Голосовое сообщение
            caption = message.html_text if hasattr(message, 'html_text') and message.caption else message.caption
            sent_msg = await bot.send_voice(
                chat_id=user_id,
                voice=message.voice.file_id,
                caption=caption,
                parse_mode="HTML" if hasattr(message, 'html_text') and message.caption else None
            )
            return sent_msg
            
        elif message.sticker:
            # Стикер
            sent_msg = await bot.send_sticker(
                chat_id=user_id,
                sticker=message.sticker.file_id
            )
            return sent_msg
            
        else:
            # Другие типы контента - отправляем текстовое представление
            content_type = str(message.content_type).replace("ContentType.", "")
            fallback_text = f"📎 <b>Сообщение от администратора</b>\n"
            fallback_text += f"Тип: {content_type}\n"
            
            if message.caption:
                fallback_text += f"\n{message.caption}"
            
            sent_msg = await bot.send_message(
                chat_id=user_id,
                text=fallback_text,
                parse_mode="HTML"
            )
            return sent_msg
            
    except TelegramForbiddenError:
        raise  # Перевыбрасываем чтобы обработать отдельно
    except Exception as e:
        logger.error(f"Error sending message to user: {e}")
        raise
            

# --- 8. ХЕНДЛЕРЫ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(GuardMiddleware())

# --- START С ФОТКОЙ И ЭФФЕКТОМ ПЕЧАТАНИЯ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref = int(command.args) if command.args and command.args.isdigit() else None
    await db_engine.register(message.from_user.id, ref)
    
    # Отправляем фото с приветствием
    welcome_text = (
        "🌟 <b>Добро пожаловать в Spok Elite Support!</b>\n\n"
        "✨ <i>Ваш надежный помощник в решении любых вопросов.</i>\n\n"
        "Здесь вы можете:\n"
        "• 🆘 Получить помощь по любым вопросам\n"
        "• ⭐️ Оставить отзыв о нашей работе\n"
        "• 📊 Посмотреть рейтинг администраторов\n"
        "• 👤 Управлять своим профилем\n\n"
        "<b>Используйте кнопки ниже для навигации ⤵️</b>"
    )
    
    try:
        # Пытаемся отправить фото
        await send_photo_with_typing(
            chat_id=message.chat.id,
            photo_url=START_PHOTO_URL,
            caption=welcome_text,
            bot=bot,
            parse_mode="HTML",
            reply_markup=get_main_kb()
        )
    except:
        # Если фото не загрузилось, отправляем только текст
        await send_with_typing(
            chat_id=message.chat.id,
            text=welcome_text,
            bot=bot,
            parse_mode="HTML",
            reply_markup=get_main_kb()
        )
    
    # Для админов показываем дополнительную панель
    if message.from_user.id == OWNER_ID:
        await asyncio.sleep(1)
        await send_with_typing(
            chat_id=message.chat.id,
            text="👑 <b>Панель администратора активирована</b>",
            bot=bot,
            parse_mode="HTML",
            reply_markup=get_admin_kb()
        )

# --- СОЗДАНИЕ ОБРАЩЕНИЯ ---
@dp.message(F.text == "🆘 Создать обращение")
async def process_cat_selection(message: Message):
    await send_with_typing(
        chat_id=message.chat.id,
        text="📁 <b>Выберите категорию вашего вопроса:</b>",
        bot=bot,
        parse_mode="HTML",
        reply_markup=get_categories_kb()
    )

@dp.callback_query(F.data.startswith("cat_"))
async def process_cat_callback(call: CallbackQuery, state: FSMContext):
    category = call.data.split("_", 1)[1]
    
    # Показываем эффект печатания
    await bot.send_chat_action(call.message.chat.id, "typing")
    await asyncio.sleep(1)
    
    tid = await init_ticket(call.from_user.id, bot, category)
    if tid:
        await call.message.edit_text(
            f"✅ <b>Категория '{category}' выбрана!</b>\n\n"
            f"📝 Теперь напишите ваш вопрос в чат.\n"
            f"<i>Администраторы получат уведомление и ответят вам здесь.</i>",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.writing_issue)
    else:
        await call.message.edit_text(
            "❌ <b>Не удалось создать обращение.</b>\n"
            "Пожалуйста, попробуйте позже или обратитесь к администратору.",
            parse_mode="HTML"
        )
    await call.answer()

# --- ПРОФИЛЬ ---
@dp.message(F.text == "👤 Мой профиль")
async def process_profile(message: Message):
    u = await db_engine.get_user(uid=message.from_user.id)
    
    # Показываем эффект печатания
    await bot.send_chat_action(message.chat.id, "typing")
    await asyncio.sleep(0.5)
    
    # Получаем количество рефералов
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (message.from_user.id,)) as c:
            refs = (await c.fetchone())[0]
        
        # Получаем историю варнов
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT reason, created_at FROM warns_history 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 3
        """, (message.from_user.id,)) as c:
            warns_history = await c.fetchall()
    
    me = await bot.get_me()
    profile_text = (
        f"👤 <b>ВАШ АККАУНТ</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{u['anon_id']}</code>\n"
        f"⚠️ Предупреждения: <b>{u['warns']}/3</b>\n"
        f"👥 Рефералы: <b>{refs}</b>\n"
        f"📩 Сообщений: <b>{u['msg_count']}</b>\n"
        f"📅 Регистрация: <b>{u['created_at'][:10]}</b>\n"
    )
    
    if warns_history:
        profile_text += f"\n<b>Последние предупреждения:</b>\n"
        for warn in warns_history:
            date = warn['created_at'][:16]
            reason = warn['reason'] or "без причины"
            profile_text += f"▫️ {date}: {reason}\n"
    
    profile_text += (
        f"━━━━━━━━━━━━━━\n"
        f"🔗 <b>Ссылка для друзей:</b>\n"
        f"<code>https://t.me/{me.username}?start={message.from_user.id}</code>"
    )
    
    await send_with_typing(
        chat_id=message.chat.id,
        text=profile_text,
        bot=bot,
        parse_mode="HTML"
    )

# --- СТЕНА ОТЗЫВОВ ---
@dp.message(F.text == "📊 Стена отзывов")
@dp.message(Command("reviews"))
async def process_reviews_wall(message: Message):
    # Показываем эффект загрузки
    await bot.send_chat_action(message.chat.id, "typing")
    await asyncio.sleep(1)
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        # Топ админов
        async with db.execute("""
            SELECT admin_alias, AVG(rating) as avg_r, COUNT(*) as cnt 
            FROM reviews 
            GROUP BY admin_alias 
            HAVING COUNT(*) >= 3 
            ORDER BY avg_r DESC 
            LIMIT 5
        """) as c: 
            top = await c.fetchall()
        
        # Последние отзывы
        async with db.execute("""
            SELECT r.*, u.anon_id 
            FROM reviews r 
            LEFT JOIN users u ON r.user_id = u.user_id 
            ORDER BY r.id DESC 
            LIMIT 10
        """) as c: 
            lasts = await c.fetchall()
        
        # Общая статистика
        async with db.execute("SELECT COUNT(*), AVG(rating) FROM reviews") as c:
            total_count, avg_rating = await c.fetchone()

    res = "🏆 <b>РЕЙТИНГ АДМИНИСТРАЦИИ:</b>\n"
    for i, a in enumerate(top, 1):
        stars = "⭐" * round(a['avg_r'])
        res += f"{i}. {a['admin_alias']} — {round(a['avg_r'], 1)} {stars} ({a['cnt']} отз.)\n"
    
    res += f"\n📊 <b>Общая статистика:</b>\n"
    res += f"Всего отзывов: {total_count}\n"
    res += f"Средний рейтинг: {round(avg_rating or 0, 2)}/5\n"
    
    res += "\n💬 <b>ПОСЛЕДНИЕ ОТЗЫВЫ:</b>\n"
    for r in lasts:
        anon_id = r['anon_id'] or "Скрыт"
        comment_preview = r['comment'][:50] + "..." if len(r['comment']) > 50 else r['comment']
        res += f"▫️ <b>{r['admin_alias']}</b> ({r['rating']}⭐)\n"
        res += f"   👤 {anon_id}: <i>{comment_preview}</i>\n"
    
    await send_with_typing(
        chat_id=message.chat.id,
        text=res,
        bot=bot,
        parse_mode="HTML"
    )

# --- ОТМЕНА ---
@dp.message(F.text == "❌ Отмена")
async def process_cancel(message: Message, state: FSMContext):
    await state.clear()
    await send_with_typing(
        chat_id=message.chat.id,
        text="🏠 <b>Возврат в главное меню.</b>",
        bot=bot,
        parse_mode="HTML",
        reply_markup=get_main_kb()
    )

# --- СИСТЕМА ОТЗЫВОВ ---
@dp.message(F.text == "⭐️ Оставить отзыв")
async def process_rev_1(message: Message, state: FSMContext):
    await state.set_state(BotStates.rev_adm)
    await send_with_typing(
        chat_id=message.chat.id,
        text=(
            "👤 <b>Кому из админов оставить отзыв?</b>\n\n"
            "Напишите имя или псевдоним администратора.\n"
            "<i>Пример: Иван, Алексей, Поддержка, Модератор</i>"
        ),
        bot=bot,
        parse_mode="HTML",
        reply_markup=get_cancel_kb()
    )

@dp.message(BotStates.rev_adm)
async def process_rev_2(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await process_cancel(message, state)
    
    await state.update_data(adm=message.text.strip())
    await state.set_state(BotStates.rev_rate)
    
    kb = InlineKeyboardBuilder()
    for i in range(1, 6):
        kb.button(text=f"{'⭐' * i}", callback_data=f"set_rate_{i}")
    kb.adjust(5)
    
    await send_with_typing(
        chat_id=message.chat.id,
        text=(
            f"<b>Оцените {message.text.strip()}:</b>\n\n"
            f"Выберите количество звёзд от 1 до 5\n"
            f"<i>1 — плохо, 5 — отлично</i>"
        ),
        bot=bot,
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(BotStates.rev_rate, F.data.startswith("set_rate_"))
async def process_rev_3(call: CallbackQuery, state: FSMContext):
    rate = int(call.data.split("_")[-1])
    await state.update_data(rate=rate)
    await state.set_state(BotStates.rev_msg)
    
    data = await state.get_data()
    await call.message.edit_text(
        f"✍️ <b>Напишите текст отзыва:</b>\n\n"
        f"👤 Админ: <b>{data['adm']}</b>\n"
        f"⭐ Оценка: <b>{'⭐' * rate}</b>\n\n"
        f"<i>Опишите ваш опыт взаимодействия...</i>",
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(BotStates.rev_msg)
async def process_rev_4(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await process_cancel(message, state)
    
    data = await state.get_data()
    uid = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute("""
            INSERT INTO reviews (user_id, admin_alias, rating, comment, created_at) 
            VALUES (?,?,?,?,?)
        """, (uid, data['adm'], data['rate'], message.text, datetime.now()))
        rid = c.lastrowid
        await db.commit()
    
    u = await db_engine.get_user(uid=uid)
    rev_msg = (
        f"🌟 <b>НОВЫЙ ОТЗЫВ #{rid}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 Клиент: <code>{u['anon_id']}</code>\n"
        f"🎯 Админ: <b>{data['adm']}</b>\n"
        f"⭐ Оценка: {'⭐' * data['rate']}\n"
        f"📝 Текст отзыва:\n<i>{message.text}</i>"
    )
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve_rev_{rid}")
    kb.button(text="🗑 Удалить", callback_data=f"rem_rev_{rid}")
    kb.adjust(2)
    
    await bot.send_message(
        ADMIN_GROUP_ID, 
        rev_msg, 
        message_thread_id=REVIEWS_TOPIC_ID, 
        reply_markup=kb.as_markup(), 
        parse_mode="HTML"
    )
    
    await state.clear()
    await send_with_typing(
        chat_id=message.chat.id,
        text=(
            "✅ <b>Спасибо за ваш отзыв!</b>\n\n"
            "Ваш отзыв передан на модерацию.\n"
            "После проверки администратором он появится на стене отзывов."
        ),
        bot=bot,
        parse_mode="HTML",
        reply_markup=get_main_kb()
    )

# --- ОДОБРЕНИЕ/УДАЛЕНИЕ ОТЗЫВА ---
@dp.callback_query(F.data.startswith("approve_rev_"))
async def process_rev_approve(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("❌ Доступ запрещен!", show_alert=True)
    
    rid = call.data.split("_")[-1]
    await call.message.edit_text(f"✅ Отзыв #{rid} одобрен и опубликован.")
    await call.answer("Отзыв одобрен!")

@dp.callback_query(F.data.startswith("rem_rev_"))
async def process_rev_del(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("❌ Доступ запрещен!", show_alert=True)
    
    rid = call.data.split("_")[-1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM reviews WHERE id = ?", (rid,))
        await db.commit()
    
    await call.message.edit_text(f"🗑 Отзыв #{rid} удален администратором.")
    await call.answer("Отзыв удален!")

# --- РАССЫЛКА ---
@dp.callback_query(F.data == "admin_broadcast")
async def start_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("❌ Доступ запрещен!", show_alert=True)
    
    await state.set_state(BotStates.broadcasting)
    await call.message.edit_text(
        "📢 <b>Режим рассылки активирован</b>\n\n"
        "Отправьте сообщение для рассылки:\n"
        "• Текст\n"
        "• Фото с подписью\n"
        "• Видео\n"
        "• Документ\n"
        "• Аудио\n\n"
        "<i>Сообщение будет отправлено всем активным пользователям.</i>",
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(Command("cancel"), BotStates.broadcasting)
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Рассылка отменена.", reply_markup=get_admin_kb())

@dp.message(BotStates.broadcasting)
async def process_broadcast_content(message: Message, state: FSMContext):
    await state.update_data(broadcast_message=message)
    await state.set_state(BotStates.broadcast_confirm)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Начать рассылку", callback_data="confirm_broadcast")
    kb.button(text="❌ Отмена", callback_data="cancel_broadcast")
    kb.adjust(1)
    
    content_type = message.content_type
    preview = message.text or message.caption or f"Сообщение типа: {content_type}"
    preview = preview[:200] + "..." if len(preview) > 200 else preview
    
    await message.answer(
        f"📢 <b>Подтверждение рассылки</b>\n\n"
        f"📁 Тип: <b>{content_type}</b>\n"
        f"📝 Содержимое:\n{preview}\n\n"
        f"<b>Будет отправлено всем активным пользователям.</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "confirm_broadcast", BotStates.broadcast_confirm)
async def confirm_broadcast(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🔄 <b>Начинаю рассылку...</b>", parse_mode="HTML")
    
    data = await state.get_data()
    message_to_send = data['broadcast_message']
    
    # Получаем всех активных пользователей
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_active = 1 AND is_banned = 0") as c:
            users = await c.fetchall()
    
    total = len(users)
    success = 0
    failed = 0
    start_time = time.time()
    
    progress_msg = await call.message.answer(f"📊 Прогресс: 0/{total}")
    
    for index, (user_id,) in enumerate(users, 1):
        try:
            # Отправляем сообщение пользователю
            await send_message_to_user(bot, user_id, message_to_send)
            
            success += 1
            
            # Обновляем прогресс каждые 10 пользователей
            if index % 10 == 0:
                await progress_msg.edit_text(
                    f"📊 Прогресс: {index}/{total}\n"
                    f"✅ Успешно: {success}\n"
                    f"❌ Ошибок: {failed}"
                )
                await asyncio.sleep(0.05)  # Анти-спам
            
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
                await db.commit()
            failed += 1
        except TelegramRetryAfter as e:
            # Превышен лимит запросов
            await asyncio.sleep(e.retry_after)
            index -= 1  # Повторим эту итерацию
        except Exception as e:
            logger.error(f"Broadcast error for {user_id}: {e}")
            failed += 1
    
    # Сохраняем статистику рассылки
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO broadcast_messages (admin_id, message_type, content, sent_count, failed_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            call.from_user.id,
            message_to_send.content_type,
            message_to_send.text or message_to_send.caption or "",
            success,
            failed,
            datetime.now()
        ))
        await db.commit()
    
    total_time = time.time() - start_time
    await progress_msg.delete()
    
    await call.message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📊 Статистика:\n"
        f"• Всего получателей: {total}\n"
        f"• Успешно отправлено: {success}\n"
        f"• Не удалось отправить: {failed}\n"
        f"• Время выполнения: {total_time:.1f} сек.\n"
        f"• Скорость: {total/max(total_time, 0.1):.1f} сообщ/сек.",
        parse_mode="HTML",
        reply_markup=get_admin_kb()
    )
    
    await state.clear()

@dp.callback_query(F.data == "cancel_broadcast", BotStates.broadcast_confirm)
async def cancel_broadcast_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена.", reply_markup=get_admin_kb())

# --- СТАТИСТИКА АДМИНА ---
@dp.callback_query(F.data == "admin_stats")
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def adm_stats(message: Union[Message, CallbackQuery]):
    if isinstance(message, CallbackQuery):
        msg = message.message
        user_id = message.from_user.id
    else:
        msg = message
        user_id = message.from_user.id
    
    if user_id != OWNER_ID:
        if isinstance(message, CallbackQuery):
            await message.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    # Показываем эффект загрузки
    if isinstance(message, CallbackQuery):
        await bot.send_chat_action(msg.chat.id, "typing")
    await asyncio.sleep(1)
    
    # Собираем статистику
    async with aiosqlite.connect(DB_NAME) as db:
        # Базовая статистика
        async with db.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_active = 1") as c: active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as c: banned = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE DATE(created_at) = DATE('now')") as c: new_today = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(msg_count) FROM users") as c: total_msgs = (await c.fetchone())[0] or 0
        
        # Статистика по варнам
        async with db.execute("SELECT SUM(warns) FROM users") as c: total_warns = (await c.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(*) FROM users WHERE warns > 0") as c: warned_users = (await c.fetchone())[0]
        
        # Статистика по отзывам
        async with db.execute("SELECT COUNT(*), AVG(rating) FROM reviews") as c:
            reviews_count, avg_rating = await c.fetchone()
        
        # Статистика по рефералам
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL") as c:
            ref_users = (await c.fetchone())[0]
        
        # Топ пользователей по сообщениям
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT anon_id, msg_count FROM users 
            ORDER BY msg_count DESC 
            LIMIT 5
        """) as c:
            top_senders = await c.fetchall()
        
        # Статистика за последние 7 дней
        async with db.execute("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as count
            FROM users 
            WHERE created_at >= DATE('now', '-7 days')
            GROUP BY DATE(created_at)
            ORDER BY date
        """) as c:
            weekly_stats = await c.fetchall()
        
        # Активные сегодня
        async with db.execute("""
            SELECT COUNT(*) FROM users 
            WHERE DATE(last_seen) = DATE('now') AND is_active = 1
        """) as c:
            active_today = (await c.fetchone())[0]
    
    # Формируем отчет
    stats_text = (
        f"📊 <b>СТАТИСТИКА СИСТЕМЫ</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: <b>{total}</b>\n"
        f"• Активных: <b>{active}</b>\n"
        f"• Активных сегодня: <b>{active_today}</b>\n"
        f"• Заблокированных: <b>{banned}</b>\n"
        f"• Новых сегодня: <b>{new_today}</b>\n"
        f"• С рефералами: <b>{ref_users}</b>\n\n"
        
        f"💬 <b>Сообщения:</b>\n"
        f"• Всего: <b>{total_msgs}</b>\n"
        f"• Среднее на пользователя: <b>{round(total_msgs/max(total, 1), 1)}</b>\n\n"
        
        f"⚠️ <b>Предупреждения:</b>\n"
        f"• Всего варнов: <b>{total_warns}</b>\n"
        f"• Пользователей с варнами: <b>{warned_users}</b>\n\n"
        
        f"⭐ <b>Отзывы:</b>\n"
        f"• Всего: <b>{reviews_count or 0}</b>\n"
        f"• Средний рейтинг: <b>{round(avg_rating or 0, 2)}/5</b>\n\n"
    )
    
    if top_senders:
        stats_text += f"🏆 <b>Топ отправителей:</b>\n"
        for i, user in enumerate(top_senders, 1):
            stats_text += f"{i}. {user['anon_id']}: {user['msg_count']} сообщ.\n"
        stats_text += "\n"
    
    if weekly_stats:
        stats_text += f"📈 <b>Регистрации за неделю:</b>\n"
        for stat in weekly_stats:
            stats_text += f"• {stat['date'][5:]}: {stat['count']} чел.\n"
    
    if isinstance(message, CallbackQuery):
        await msg.edit_text(stats_text, parse_mode="HTML")
        await message.answer()
    else:
        await msg.answer(stats_text, parse_mode="HTML")

# --- КОМАНДЫ МОДЕРАЦИИ ---
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"), F.is_topic_message)
async def adm_warn(message: Message, command: CommandObject):
    if message.from_user.id != OWNER_ID:
        return
    
    args = command.args or ""
    parts = args.split(maxsplit=2)
    
    if len(parts) < 2:
        await message.answer(
            "⚠️ <b>Использование:</b>\n"
            "/warn <время> <причина>\n\n"
            "Примеры:\n"
            "/warn 1h Спам\n"
            "/warn 30m Грубость\n"
            "/warn 2d Нарушение правил",
            parse_mode="HTML"
        )
        return
    
    time_str, reason = parts[0], parts[1]
    
    u = await db_engine.get_user(tid=message.message_thread_id)
    if not u:
        return await message.answer("❌ Пользователь не найден!")
    
    w_count = await db_engine.add_warn(u['user_id'], message.from_user.id, reason)
    
    warn_msg = (
        f"⚠️ Пользователю <code>{u['anon_id']}</code> выдан варн ({w_count}/3).\n"
        f"Причина: <i>{reason}</i>"
    )
    await message.answer(warn_msg, parse_mode="HTML")
    
    # Уведомляем пользователя
    user_notify = (
        f"⚠️ <b>Вам выдано предупреждение ({w_count}/3)!</b>\n\n"
        f"📋 Причина: <i>{reason}</i>\n\n"
        f"Пожалуйста, соблюдайте правила общения.\n"
        f"<i>При получении 3 предупреждений — автоматическая блокировка.</i>"
    )
    await bot.send_message(u['user_id'], user_notify, parse_mode="HTML")
    
    # Автоматический бан при 3 варнах
    if w_count >= 3:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                UPDATE users 
                SET is_banned = 1, ban_reason = '3 предупреждения', ban_until = NULL
                WHERE user_id = ?
            """, (u['user_id'],))
            await db.commit()
        
        await message.answer(f"🚫 <b>Лимит предупреждений достигнут!</b> Пользователь забанен.")
        await bot.send_message(u['user_id'], "🚫 <b>Вы заблокированы за получение 3 предупреждений.</b>", parse_mode="HTML")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"), F.is_topic_message)
async def adm_ban(message: Message, command: CommandObject):
    if message.from_user.id != OWNER_ID:
        return
    
    args = command.args or ""
    parts = args.split(maxsplit=2)
    
    if not parts:
        await message.answer(
            "🚫 <b>Использование:</b>\n"
            "/ban <время> [причина]\n\n"
            "Примеры:\n"
            "/ban 1d Спам\n"
            "/ban 2h Грубость\n"
            "/ban перманентно Нарушение правил",
            parse_mode="HTML"
        )
        return
    
    time_str = parts[0]
    reason = parts[1] if len(parts) > 1 else "Нарушение правил"
    
    u = await db_engine.get_user(tid=message.message_thread_id)
    if not u:
        return await message.answer("❌ Пользователь не найден!")
    
    ban_duration = parse_time(time_str)
    
    if ban_duration is None:  # Перманентный бан
        ban_until = None
        ban_duration_text = "навсегда"
    else:
        ban_until = datetime.now() + ban_duration
        ban_duration_text = format_timedelta(ban_duration)
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users 
            SET is_banned = 1, ban_until = ?, ban_reason = ?
            WHERE user_id = ?
        """, (ban_until, reason, u['user_id']))
        await db.commit()
    
    admin_msg = (
        f"🚫 Пользователь <code>{u['anon_id']}</code> забанен.\n"
        f"⏰ Длительность: <b>{ban_duration_text}</b>\n"
        f"📋 Причина: <i>{reason}</i>"
    )
    await message.answer(admin_msg, parse_mode="HTML")
    
    # Уведомляем пользователя
    if ban_until:
        user_msg = (
            f"🚫 <b>Вы заблокированы!</b>\n\n"
            f"⏰ Длительность: <b>{ban_duration_text}</b>\n"
            f"📋 Причина: <i>{reason}</i>\n\n"
            f"<i>Блокировка будет снята автоматически по истечении времени.</i>"
        )
    else:
        user_msg = (
            f"🚫 <b>Вы заблокированы навсегда!</b>\n\n"
            f"📋 Причина: <i>{reason}</i>\n\n"
            f"<i>Обратитесь к администратору для разблокировки.</i>"
        )
    
    await bot.send_message(u['user_id'], user_msg, parse_mode="HTML")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"), F.is_topic_message)
async def adm_unban(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    
    u = await db_engine.get_user(tid=message.message_thread_id)
    if not u:
        return await message.answer("❌ Пользователь не найден!")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users 
            SET is_banned = 0, ban_until = NULL, ban_reason = NULL, warns = 0
            WHERE user_id = ?
        """, (u['user_id'],))
        await db.commit()
    
    await message.answer(f"✅ Пользователь <code>{u['anon_id']}</code> разбанен.", parse_mode="HTML")
    await bot.send_message(u['user_id'], "✅ <b>Ваша блокировка снята!</b>", parse_mode="HTML")

# --- ЭКСПОРТ ДАННЫХ ---
@dp.callback_query(F.data == "admin_export")
async def adm_export(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("❌ Доступ запрещен!", show_alert=True)
    
    await call.message.edit_text("📥 <b>Готовлю отчет...</b>", parse_mode="HTML")
    
    # Показываем эффект
    await bot.send_chat_action(call.message.chat.id, "typing")
    await asyncio.sleep(2)
    
    # Собираем данные
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        # Пользователи
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as c:
            users = await c.fetchall()
        
        # Отзывы
        async with db.execute("SELECT * FROM reviews ORDER BY created_at DESC") as c:
            reviews = await c.fetchall()
        
        # Логи варнов
        async with db.execute("SELECT * FROM warns_history ORDER BY created_at DESC") as c:
            warns = await c.fetchall()
    
    # Создаем HTML отчет с исправленным CSS
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Отчет системы Spok Elite</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #2c3e50; text-align: center; margin-bottom: 30px; }}
            h2 {{ color: #3498db; border-bottom: 2px solid #3498db; padding-bottom: 10px; margin-top: 40px; }}
            .summary {{ background: #ecf0f1; padding: 20px; border-radius: 8px; margin-bottom: 30px; }}
            .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }}
            .summary-item {{ background: white; padding: 15px; border-radius: 6px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .summary-value {{ font-size: 24px; font-weight: bold; color: #2c3e50; margin: 5px 0; }}
            .summary-label {{ color: #7f8c8d; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
            th {{ background-color: #3498db; color: white; font-weight: bold; }}
            tr:nth-child(even) {{ background-color: #f8f9fa; }}
            tr:hover {{ background-color: #f1f8ff; }}
            .status-banned {{ color: #e74c3c; font-weight: bold; }}
            .status-active {{ color: #27ae60; font-weight: bold; }}
            .rating-stars {{ color: #f39c12; }}
            .timestamp {{ font-size: 12px; color: #95a5a6; }}
            @media print {{
                body {{ background: white; }}
                .container {{ box-shadow: none; }}
                .no-print {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📊 Отчет системы Spok Elite Support</h1>
            <div class="summary">
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Всего пользователей</div>
                        <div class="summary-value">{len(users)}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Отзывов</div>
                        <div class="summary-value">{len(reviews)}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Предупреждений</div>
                        <div class="summary-value">{len(warns)}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Дата отчета</div>
                        <div class="summary-value">{datetime.now().strftime("%d.%m.%Y %H:%M")}</div>
                    </div>
                </div>
            </div>
    """
    
    # Таблица пользователей
    html_content += "<h2>👥 Пользователи</h2>"
    html_content += """
    <table>
        <tr>
            <th>ID</th>
            <th>Anon ID</th>
            <th>Регистрация</th>
            <th>Последняя активность</th>
            <th>Сообщения</th>
            <th>Варны</th>
            <th>Статус</th>
            <th>Реферал</th>
        </tr>
    """
    
    for user in users:
        status = "BANNED" if user['is_banned'] else ("ACTIVE" if user['is_active'] else "INACTIVE")
        status_class = "status-banned" if user['is_banned'] else "status-active"
        last_seen = user['last_seen'][:19] if user['last_seen'] else "никогда"
        
        html_content += f"""
        <tr>
            <td>{user['user_id']}</td>
            <td><b>{user['anon_id']}</b></td>
            <td>{user['created_at'][:19]}</td>
            <td class="timestamp">{last_seen}</td>
            <td>{user['msg_count']}</td>
            <td>{user['warns']}</td>
            <td class="{status_class}">{status}</td>
            <td>{user['referrer_id'] or '-'}</td>
        </tr>
        """
    
    html_content += "</table>"
    
    # Таблица отзывов
    if reviews:
        html_content += "<h2>⭐ Отзывы</h2>"
        html_content += """
        <table>
            <tr>
                <th>ID</th>
                <th>Пользователь</th>
                <th>Админ</th>
                <th>Оценка</th>
                <th>Комментарий</th>
                <th>Дата</th>
            </tr>
        """
        
        for review in reviews:
            stars = "★" * review['rating'] + "☆" * (5 - review['rating'])
            html_content += f"""
            <tr>
                <td>{review['id']}</td>
                <td>{review['user_id']}</td>
                <td><b>{review['admin_alias']}</b></td>
                <td class="rating-stars">{stars} ({review['rating']}/5)</td>
                <td>{review['comment']}</td>
                <td class="timestamp">{review['created_at'][:19]}</td>
            </tr>
            """
        
        html_content += "</table>"
    
    # Таблица предупреждений
    if warns:
        html_content += "<h2>⚠️ История предупреждений</h2>"
        html_content += """
        <table>
            <tr>
                <th>ID</th>
                <th>Пользователь</th>
                <th>Админ</th>
                <th>Причина</th>
                <th>Дата</th>
            </tr>
        """
        
        for warn in warns:
            html_content += f"""
            <tr>
                <td>{warn['id']}</td>
                <td>{warn['user_id']}</td>
                <td>{warn['admin_id']}</td>
                <td>{warn['reason'] or 'Не указана'}</td>
                <td class="timestamp">{warn['created_at'][:19]}</td>
            </tr>
            """
        
        html_content += "</table>"
    
    html_content += """
            <div class="no-print" style="margin-top: 40px; text-align: center; color: #95a5a6; font-size: 12px;">
                <p>Отчет сгенерирован автоматически системой Spok Elite Support</p>
                <p>Для обновления данных перезапустите генерацию отчета</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    file = BufferedInputFile(html_content.encode('utf-8'), 
                           filename=f"spok_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    
    await call.message.answer_document(
        document=file,
        caption=(
            "📊 <b>Детальный отчет системы</b>\n\n"
            f"📅 Сгенерирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"👥 Пользователей: {len(users)}\n"
            f"⭐ Отзывов: {len(reviews)}\n"
            f"⚠️ Предупреждений: {len(warns)}"
        ),
        parse_mode="HTML"
    )
    await call.answer()

# --- GATEWAY ПЕРЕПИСКИ С РЕАКЦИЯМИ ---
@dp.message(F.chat.type == "private")
async def gateway_u2a(message: Message, state: FSMContext):
    # Игнорируем служебные сообщения и кнопки
    if message.content_type in [
        ContentType.FORUM_TOPIC_CREATED,
        ContentType.FORUM_TOPIC_EDITED,
        ContentType.FORUM_TOPIC_CLOSED,
        ContentType.FORUM_TOPIC_REOPENED,
        ContentType.GENERAL_FORUM_TOPIC_HIDDEN,
        ContentType.GENERAL_FORUM_TOPIC_UNHIDDEN
    ]:
        return
    
    protected_buttons = ["🆘 Создать обращение", "⭐️ Оставить отзыв", "📊 Стена отзывов", "👤 Мой профиль", "❌ Отмена"]
    
    if await state.get_state() or (message.text and (message.text.startswith("/") or message.text in protected_buttons)):
        return

    u = await db_engine.get_user(uid=message.from_user.id)
    if not u or not u['topic_id']:
        return await message.answer(
            "⚠️ <b>Сначала создайте обращение!</b>\n\n"
            "1. Нажмите кнопку <b>'🆘 Создать обращение'</b>\n"
            "2. Выберите категорию вопроса\n"
            "3. Напишите ваш вопрос в чат\n\n"
            "<i>После этого администраторы смогут вам ответить.</i>",
            parse_mode="HTML"
        )

    try:
        # Отправляем сообщение админу
        sent_message = await send_message_to_admin(bot, message.from_user.id, message, u['topic_id'])
        
        if sent_message:
            # Обновляем статистику
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET msg_count = msg_count + 1, last_seen = ? WHERE user_id = ?", 
                               (datetime.now(), message.from_user.id))
                await db.commit()
            
            # Ставим реакцию пользователю
            try:
                await message.react([ReactionTypeEmoji(emoji="👍")])
            except:
                pass  # Если реакции не поддерживаются
            
            # Ждем немного и ставим реакцию админу в группе
            await asyncio.sleep(0.5)
            await safe_set_reaction(bot, ADMIN_GROUP_ID, sent_message.message_id, "👤")
        else:
            logger.error(f"Failed to send message from user {message.from_user.id} to admin")
            try:
                await message.react([ReactionTypeEmoji(emoji="👎")])
            except:
                pass
            
    except Exception as e:
        logger.error(f"U2A gateway error: {e}")
        # Показываем пользователю, что произошла ошибка
        try:
            await message.react([ReactionTypeEmoji(emoji="❌")])
        except:
            pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def gateway_a2u(message: Message):
    # Игнорируем команды и служебные сообщения
    if message.text and message.text.startswith("/"):
        return
    
    if message.content_type in [
        ContentType.FORUM_TOPIC_CREATED,
        ContentType.FORUM_TOPIC_EDITED,
        ContentType.FORUM_TOPIC_CLOSED,
        ContentType.FORUM_TOPIC_REOPENED,
        ContentType.GENERAL_FORUM_TOPIC_HIDDEN,
        ContentType.GENERAL_FORUM_TOPIC_UNHIDDEN
    ]:
        return
    
    u = await db_engine.get_user(tid=message.message_thread_id)
    if u:
        try:
            # Отправляем сообщение пользователю
            sent_message = await send_message_to_user(bot, u['user_id'], message)
            
            # Ставим реакцию админу в группе (лайк)
            await safe_set_reaction(bot, ADMIN_GROUP_ID, message.message_id, "👍")
            
            # Ждем немного и ставим реакцию пользователю
            await asyncio.sleep(0.5)
            await safe_set_reaction(bot, u['user_id'], sent_message.message_id, "👍")
                
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            logger.warning(f"User {u['user_id']} blocked the bot")
            await safe_set_reaction(bot, ADMIN_GROUP_ID, message.message_id, "👎")
            
            # Деактивируем пользователя в БД
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (u['user_id'],))
                await db.commit()
            
        except Exception as e:
            logger.error(f"A2U gateway error: {e}")
            # Пробуем поставить реакцию об ошибке
            await safe_set_reaction(bot, ADMIN_GROUP_ID, message.message_id, "❌")
            
# --- ОЧИСТКА КЭША ---
@dp.callback_query(F.data == "admin_clear_cache")
async def clear_cache(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("❌ Доступ запрещен!", show_alert=True)
    
    try:
        # Очищаем сессии FSM
        from aiogram.fsm.storage.memory import MemoryStorage
        if isinstance(dp.storage, MemoryStorage):
            dp.storage._data.clear()
            dp.storage._chat_data.clear()
            dp.storage._user_data.clear()
        
        # Сборка мусора
        import gc
        gc.collect()
        
        await call.answer("✅ Кэш очищен!", show_alert=True)
    except Exception as e:
        logger.error(f"Clear cache error: {e}")
        await call.answer("❌ Ошибка очистки кэша!", show_alert=True)

# --- ЗАПУСК ---
async def on_start():
    await db_engine.initialize()
    logger.info("✅ SYSTEM ONLINE (V15)")
    
    # Устанавливаем команды бота
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Запустить бота"),
        BotCommand(command="reviews", description="📊 Смотреть отзывы"),
        BotCommand(command="stats", description="📈 Статистика (админ)"),
    ])
    
    # Отправляем уведомление владельцу
    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                "🤖 <b>Бот успешно запущен!</b>\n\n"
                f"Версия: <code>v15</code>\n"
                f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                f"База данных: {DB_NAME}\n\n"
                f"✅ Эффекты печати активны\n"
                f"✅ Реакции сообщений включены\n"
                f"✅ Стартовая фотка загружена\n"
                f"✅ Улучшенный gateway с фолбэками\n"
                f"✅ Фикс отправки медиафайлов",
                parse_mode="HTML"
            )
        except:
            pass

async def main():
    await on_start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹ Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"💥 Critical error: {e}")
