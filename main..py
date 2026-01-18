import asyncio
import logging
import os
import random
import string
import sys
import io
import gc
import shutil
from datetime import datetime
from typing import Union, List, Optional, Any, Dict, Final

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandStart, CommandObject, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.exceptions import (
    TelegramForbiddenError, 
    TelegramRetryAfter, 
    TelegramBadRequest
)
from aiogram.types import (
    Message, 
    CallbackQuery, 
    BotCommand,
    BotCommandScopeChat,
    ReactionTypeEmoji,
    BufferedInputFile,
    InlineKeyboardButton,
    ReplyKeyboardMarkup
)

# --- 1. ИНИЦИАЛИЗАЦИЯ И ЛОГИРОВАНИЕ ---
load_dotenv()

# Исправленное логирование (StreamHandler)
logger = logging.getLogger("SpokElite_v11")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(formatter)
logger.addHandler(sh)

fh = logging.FileHandler("bot_v11_core.log", encoding='utf-8')
fh.setFormatter(formatter)
logger.addHandler(fh)

# Загрузка конфигурации
BOT_TOKEN: Final = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID: Final = int(os.getenv("ADMIN_GROUP_ID", 0))
REVIEWS_TOPIC_ID: Final = int(os.getenv("REVIEWS_TOPIC_ID", 0))
OWNER_ID: Final = int(os.getenv("OWNER_ID", 0))
DB_NAME: Final = "spok_v11_final.db"

if not BOT_TOKEN:
    logger.critical("Брат, добавь BOT_TOKEN в .env файл!")
    sys.exit(1)

# --- 2. УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ ---
class DatabaseManager:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self):
        """Создание таблиц с поддержкой системы варнов и статусов"""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    anon_id TEXT UNIQUE,
                    topic_id INTEGER UNIQUE,
                    referrer_id INTEGER,
                    warns INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    msg_count INTEGER DEFAULT 0,
                    created_at DATETIME
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
                    u_id INTEGER,
                    event TEXT,
                    timestamp DATETIME
                );
            """)
            await db.commit()
            logger.info("DB: Система данных v11 готова.")

    async def register(self, uid: int, rid: int = None):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,)) as c:
                if not await c.fetchone():
                    aid = "USER-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
                    await db.execute("""
                        INSERT INTO users (user_id, anon_id, referrer_id, created_at) 
                        VALUES (?, ?, ?, ?)
                    """, (uid, aid, rid, datetime.now()))
                    await db.commit()

    async def get_user(self, uid: int = None, tid: int = None):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM users WHERE user_id = ?" if uid else "SELECT * FROM users WHERE topic_id = ?"
            async with db.execute(q, (uid or tid,)) as c:
                r = await c.fetchone()
                return dict(r) if r else None

    async def add_warn(self, uid: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET warns = warns + 1 WHERE user_id = ?", (uid,))
            await db.commit()
            async with db.execute("SELECT warns FROM users WHERE user_id = ?", (uid,)) as c:
                res = await c.fetchone()
                return res[0]

db_engine = DatabaseManager(DB_NAME)

# --- 3. FSM И ЗАЩИТНАЯ МИДЛВАРЬ ---
class BotStates(StatesGroup):
    # Категории и тикеты
    choosing_category = State()
    writing_issue = State()
    # Отзывы
    rev_adm = State()
    rev_rate = State()
    rev_msg = State()
    # Админ
    broadcasting = State()

class GuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if not event.from_user or event.chat.type != "private":
            return await handler(event, data)
        
        await db_engine.register(event.from_user.id)
        u = await db_engine.get_user(uid=event.from_user.id)
        
        if u and u['is_banned']:
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
    categories = ["🛠 Тех. вопрос", "🎨 Поддержка", "🎉 Общение"]
    for cat in categories:
        b.button(text=cat, callback_data=f"cat_{cat}")
    b.adjust(2)
    return b.as_markup()

def get_cancel_kb():
    b = ReplyKeyboardBuilder()
    b.add(types.KeyboardButton(text="❌ Отмена"))
    return b.as_markup(resize_keyboard=True)

# --- 5. ЛОГИКА ТИКЕТОВ И ФОРУМА ---
async def init_ticket(uid: int, bot: Bot, category: str):
    user = await db_engine.get_user(uid=uid)
    if not user: return None
    
    if user['topic_id']:
        return user['topic_id']
    
    try:
        # Создаем топик в админ-чате
        topic = await bot.create_forum_topic(
            ADMIN_GROUP_ID, 
            f"{category} | {user['anon_id']}"
        )
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET topic_id = ? WHERE user_id = ?", (topic.message_thread_id, uid))
            await db.commit()
            
        # Формируем красивую карточку заявки
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
        await bot.send_message(ADMIN_GROUP_ID, ticket_card, message_thread_id=topic.message_thread_id, parse_mode="HTML")
        return topic.message_thread_id
    except Exception as e:
        logger.error(f"Ticket Init Error: {e}")
        return None

# --- 6. ХЕНДЛЕРЫ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(GuardMiddleware())

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref = int(command.args) if command.args and command.args.isdigit() else None
    await db_engine.register(message.from_user.id, ref)
    await message.answer("👋 <b>Добро пожаловать в сервис анонимной помощи!</b>\n\n"
                         "Используйте кнопку ниже, чтобы выбрать категорию и создать запрос.", 
                         reply_markup=get_main_kb(), parse_mode="HTML")

# --- ЛОГИКА СОЗДАНИЯ ОБРАЩЕНИЯ ---
@dp.message(F.text == "🆘 Создать обращение")
async def process_cat_selection(message: Message):
    await message.answer("📁 <b>Выберите категорию вашего вопроса:</b>", reply_markup=get_categories_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("cat_"))
async def process_cat_callback(call: CallbackQuery, state: FSMContext):
    category = call.data.split("_")[1]
    tid = await init_ticket(call.from_user.id, bot, category)
    if tid:
        await call.message.edit_text(f"✅ <b>Категория '{category}' выбрана!</b>\nНапишите ваше сообщение в чат, и админы ответят вам здесь.", parse_mode="HTML")
    await call.answer()

# --- ПРОФИЛЬ (ИСПРАВЛЕННЫЙ) ---
@dp.message(F.text == "👤 Мой профиль")
async def process_profile(message: Message):
    u = await db_engine.get_user(uid=message.from_user.id)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (message.from_user.id,)) as c:
            refs = (await c.fetchone())[0]
    
    me = await bot.get_me()
    profile_text = (
        f"👤 <b>ВАШ АККАУНТ</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{u['anon_id']}</code>\n"
        f"⚠️ Предупреждения: <b>{u['warns']}/3</b>\n"
        f"👥 Рефералы: <b>{refs}</b>\n"
        f"📩 Сообщения: <b>{u['msg_count']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔗 Ссылка для друзей:\n<code>https://t.me/{me.username}?start={message.from_user.id}</code>"
    )
    await message.answer(profile_text, parse_mode="HTML")

# --- СТЕНА ОТЗЫВОВ И /REVIEWS ---
@dp.message(F.text == "📊 Стена отзывов")
@dp.message(Command("reviews"))
async def process_reviews_wall(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT admin_alias, AVG(rating) as avg_r, COUNT(*) as cnt 
            FROM reviews GROUP BY admin_alias ORDER BY avg_r DESC LIMIT 3
        """) as c: top = await c.fetchall()
        async with db.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 5") as c: lasts = await c.fetchall()

    res = "🏆 <b>РЕЙТИНГ АДМИНИСТРАЦИИ:</b>\n"
    for i, a in enumerate(top, 1):
        res += f"{i}. {a['admin_alias']} — {round(a['avg_r'], 1)}⭐ ({a['cnt']} отз.)\n"
    
    res += "\n💬 <b>ПОСЛЕДНИЕ ОТЗЫВЫ:</b>\n"
    for r in lasts:
        res += f"▪️ {r['admin_alias']} ({r['rating']}⭐): <i>{r['comment'][:40]}...</i>\n"
    
    await message.answer(res, parse_mode="HTML")

# --- СИСТЕМА ОТЗЫВОВ ---
@dp.message(F.text == "❌ Отмена")
async def process_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Возврат в меню.", reply_markup=get_main_kb())

@dp.message(F.text == "⭐️ Оставить отзыв")
async def process_rev_1(message: Message, state: FSMContext):
    await state.set_state(BotStates.rev_adm)
    await message.answer("👤 Кому из админов оставить отзыв?", reply_markup=get_cancel_kb())

@dp.message(BotStates.rev_adm)
async def process_rev_2(message: Message, state: FSMContext):
    if message.text == "❌ Отмена": return
    await state.update_data(adm=message.text)
    await state.set_state(BotStates.rev_rate)
    kb = InlineKeyboardBuilder()
    for i in range(1, 6): kb.button(text=f"{i}⭐", callback_data=f"set_rate_{i}")
    await message.answer(f"Оцените {message.text}:", reply_markup=kb.as_markup())

@dp.callback_query(BotStates.rev_rate, F.data.startswith("set_rate_"))
async def process_rev_3(call: CallbackQuery, state: FSMContext):
    rate = call.data.split("_")[-1]
    await state.update_data(rate=rate)
    await state.set_state(BotStates.rev_msg)
    await call.message.edit_text("✍️ Напишите текст отзыва:")

@dp.message(BotStates.rev_msg)
async def process_rev_4(message: Message, state: FSMContext):
    if message.text == "❌ Отмена": return
    data = await state.get_data()
    uid = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute("INSERT INTO reviews (user_id, admin_alias, rating, comment, created_at) VALUES (?,?,?,?,?)",
                         (uid, data['adm'], data['rate'], message.text, datetime.now()))
        rid = c.lastrowid
        await db.commit()
    
    u = await db_engine.get_user(uid=uid)
    rev_msg = (f"🌟 <b>ОТЗЫВ #{rid}</b>\n"
               f"━━━━━━━━━━━━━━\n"
               f"👤 Клиент: <code>{u['anon_id']}</code>\n"
               f"🎯 Админ: {data['adm']}\n"
               f"⭐ Оценка: {data['rate']}/5\n"
               f"💬 Текст: {message.text}")
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=f"rem_rev_{rid}")
    
    await bot.send_message(ADMIN_GROUP_ID, rev_msg, message_thread_id=REVIEWS_TOPIC_ID, 
                           reply_markup=kb.as_markup(), parse_mode="HTML")
    
    await state.clear()
    await message.answer("✅ Спасибо! Отзыв передан на модерацию.", reply_markup=get_main_kb())

# --- АДМИН: УДАЛЕНИЕ ОТЗЫВА ---
@dp.callback_query(F.data.startswith("rem_rev_"))
async def process_rev_del(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Доступ запрещен!", show_alert=True)
    
    rid = call.data.split("_")[-1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM reviews WHERE id = ?", (rid,))
        await db.commit()
    await call.message.edit_text(f"🗑 Отзыв #{rid} удален владельцем.")

# --- MASTER GATEWAY (USER <-> ADMIN) ---
@dp.message(F.chat.type == "private")
async def gateway_u2a(message: Message, state: FSMContext):
    # Список кнопок, которые нельзя пересылать как текст
    protected_buttons = ["🆘 Создать обращение", "⭐️ Оставить отзыв", "📊 Стена отзывов", "👤 Мой профиль", "❌ Отмена"]
    
    if await state.get_state() or (message.text and (message.text.startswith("/") or message.text in protected_buttons)):
        return

    u = await db_engine.get_user(uid=message.from_user.id)
    if not u or not u['topic_id']:
        return await message.answer("⚠️ <b>Сначала создайте обращение!</b>\nНажмите кнопку '🆘 Создать обращение' и выберите категорию.", parse_mode="HTML")

    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=u['topic_id'])
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET msg_count = msg_count + 1 WHERE user_id = ?", (message.from_user.id,))
            await db.commit()
        await message.react([ReactionTypeEmoji(emoji="✍️")])
    except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def gateway_a2u(message: Message):
    if message.text and message.text.startswith("/"): return
    u = await db_engine.get_user(tid=message.message_thread_id)
    if u:
        try:
            await message.copy_to(u['user_id'])
            await message.react([ReactionTypeEmoji(emoji="✅")])
        except: pass

# --- АДМИН КОМАНДЫ (STATS, WARN, BAN, EXPORT) ---
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def adm_stats(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_active = 1") as c: act = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_active = 0") as c: blk = (await c.fetchone())[0]
    
    await message.answer(f"📈 <b>СТАТИСТИКА:</b>\nВсего: {total}\nЖивых: {act}\nБлок: {blk}", parse_mode="HTML")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"), F.is_topic_message)
async def adm_warn(message: Message):
    u = await db_engine.get_user(tid=message.message_thread_id)
    if u:
        w_count = await db_engine.add_warn(u['user_id'])
        await message.answer(f"⚠️ Пользователю <code>{u['anon_id']}</code> выдан варн ({w_count}/3).", parse_mode="HTML")
        await bot.send_message(u['user_id'], f"⚠️ <b>Вам выдано предупреждение ({w_count}/3)!</b>\nПожалуйста, соблюдайте правила общения.", parse_mode="HTML")
        
        if w_count >= 3:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (u['user_id'],))
                await db.commit()
            await message.answer(f"🚫 <b>Лимит достигнут!</b> Пользователь забанен.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"), F.is_topic_message)
async def adm_ban(message: Message):
    if message.from_user.id != OWNER_ID: return
    u = await db_engine.get_user(tid=message.message_thread_id)
    if u:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (u['user_id'],))
            await db.commit()
        await message.answer(f"🚫 Пользователь <code>{u['anon_id']}</code> забанен навсегда.", parse_mode="HTML")

@dp.message(F.from_user.id == OWNER_ID, Command("export"))
async def adm_export(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as c: rows = await c.fetchall()
    
    html = "<html><head><meta charset='utf-8'><style>table{width:100%;border-collapse:collapse;}th,td{border:1px solid #ddd;padding:8px;text-align:left;}th{background:#4CAF50;color:white;}</style></head><body>"
    html += "<h2>ОТЧЕТ ПОЛЬЗОВАТЕЛЕЙ</h2><table><tr><th>ID</th><th>AnonID</th><th>Warns</th><th>Status</th></tr>"
    for r in rows:
        st = "BANNED" if r['is_banned'] else ("ACTIVE" if r['is_active'] else "LEFT")
        html += f"<tr><td>{r['user_id']}</td><td>{r['anon_id']}</td><td>{r['warns']}</td><td>{st}</td></tr>"
    html += "</table></body></html>"
    
    file = BufferedInputFile(html.encode('utf-8'), filename="Report_V11.html")
    await message.answer_document(file, caption="📊 Детальный HTML-отчет")

# --- ЗАПУСК ---
async def on_start():
    await db_engine.initialize()
    logger.info("SYSTEM ONLINE (V11)")

async def main():
    await on_start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except: logger.info("OFFLINE")
