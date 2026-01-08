import asyncio
import logging
import os
import aiosqlite
import random
import string
import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))

# Парсим ID владельцев. Обрабатываем и OWNER_ID и OWNER_IDS для надежности
raw_owners = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or ""
OWNER_IDS = [int(oid.strip()) for oid in raw_owners.split(",") if oid.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_NAME = "anon_chat.db"

# --- СОСТОЯНИЯ ДЛЯ РАССЫЛКИ ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            topic_id INTEGER,
            topic_name TEXT,
            warns INTEGER DEFAULT 0,
            is_banned BOOLEAN DEFAULT 0,
            reg_date TEXT
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        await db.commit()

async def get_user_data(user_id=None, topic_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id:
            async with db.execute("SELECT user_id, topic_id, topic_name, warns, is_banned FROM users WHERE user_id = ?", (user_id,)) as c:
                return await c.fetchone()
        if topic_id:
            async with db.execute("SELECT user_id, topic_id, topic_name, warns, is_banned FROM users WHERE topic_id = ?", (topic_id,)) as c:
                return await c.fetchone()
    return None

async def register_user(user_id, topic_id, topic_name):
    date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, topic_id, topic_name, reg_date) VALUES (?, ?, ?, ?)", 
                         (user_id, topic_id, topic_name, date))
        await db.commit()

async def update_ban(user_id, banned: bool):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (banned, user_id))
        await db.commit()

async def update_warns(user_id, count: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (count, user_id))
        await db.commit()

# --- ФУНКЦИЯ "ЖЕЛЕЗОБЕТОННОЙ" ОТПРАВКИ (Admin -> User) ---
async def safe_reply_to_user(chat_id, message: types.Message):
    """
    Пытается скопировать сообщение. Если не выходит (ошибка API) — отправляет вручную текст/фото.
    """
    try:
        # Попытка №1: Красивая копия
        await message.copy_to(chat_id)
    except Exception as e:
        logging.warning(f"Copy failed ({e}), trying manual send...")
        try:
            # Попытка №2: Ручная отправка (Fallback)
            if message.text:
                await bot.send_message(chat_id, f"🔔 <b>Ответ оператора:</b>\n\n{message.text}", parse_mode="HTML")
            elif message.photo:
                await bot.send_photo(chat_id, message.photo[-1].file_id, caption=message.caption or "🔔 Ответ оператора")
            elif message.voice:
                await bot.send_voice(chat_id, message.voice.file_id, caption=message.caption)
            elif message.video:
                await bot.send_video(chat_id, message.video.file_id, caption=message.caption)
            elif message.sticker:
                await bot.send_sticker(chat_id, message.sticker.file_id)
            else:
                await bot.send_message(chat_id, "🔔 <i>(Оператор отправил файл, который не удалось отобразить)</i>", parse_mode="HTML")
        except TelegramForbiddenError:
            logging.error(f"Пользователь {chat_id} заблокировал бота.")
        except Exception as e2:
            logging.error(f"FATAL: Не удалось отправить сообщение юзеру {chat_id}: {e2}")

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = await get_user_data(user_id=message.from_user.id)
    if user and user[4]: return # Бан

    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Я — бот поддержки. Напиши сюда любой вопрос или жалобу, и операторы ответят тебе анонимно.\n"
        "Мы готовы выслушать!"
    )
    try:
        msg = await message.answer_photo(photo_url, caption=text, parse_mode="HTML")
        await bot.pin_chat_message(message.chat.id, msg.message_id)
    except:
        await message.answer(text, parse_mode="HTML")

@dp.message(F.chat.type == "private")
async def user_msg(message: types.Message):
    user_id = message.from_user.id
    user = await get_user_data(user_id=user_id)
    
    if user and user[4]: return # Игнор, если забанен

    topic_id = None

    # Функция создания топика
    async def create_topic():
        name = f"Anon #{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
        try:
            topic = await bot.create_forum_topic(ADMIN_GROUP_ID, name)
            await register_user(user_id, topic.message_thread_id, name)
            await bot.send_message(ADMIN_GROUP_ID, f"🆕 <b>Новый пользователь:</b> {name}", message_thread_id=topic.message_thread_id, parse_mode="HTML")
            return topic.message_thread_id
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            return None

    if not user:
        topic_id = await create_topic()
    else:
        topic_id = user[1]

    if not topic_id:
        return await message.answer("Ошибка связи с сервером поддержки.")

    # Отправка в группу
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        # Подтверждение пользователю (исчезает через 5 сек)
        conf = await message.answer("✅ Отправлено")
        await asyncio.sleep(5)
        await conf.delete()
    except TelegramBadRequest:
        # Если топик удален, создаем новый и пробуем снова
        new_tid = await create_topic()
        if new_tid:
            try:
                await message.copy_to(ADMIN_GROUP_ID, message_thread_id=new_tid)
            except: pass
    except Exception:
        pass

# --- ХЕНДЛЕРЫ АДМИНА (В ГРУППЕ) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply(message: types.Message):
    # Если это команда (начинается с /), не обрабатываем как ответ
    if message.text and message.text.startswith("/"): return
    
    # Ищем пользователя по ID топика
    topic_id = message.message_thread_id
    user = await get_user_data(topic_id=topic_id)
    
    if not user:
        return # Сообщение в топике, который не привязан к юзеру (или системный)

    # Используем надежную отправку
    await safe_reply_to_user(user[0], message)

# --- КОМАНДЫ АДМИНА (BAN/WARN) ---

@dp.message(Command("ban"), F.chat.id == ADMIN_GROUP_ID)
async def ban_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user: return await message.reply("Не найден пользователь для этого топика.")
    
    await update_ban(user[0], True)
    await message.reply(f"🚫 Пользователь забанен.")
    try: await bot.send_message(user[0], "🚫 Вы были заблокированы администрацией.")
    except: pass

@dp.message(Command("unban"), F.chat.id == ADMIN_GROUP_ID)
async def unban_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user: return
    
    await update_ban(user[0], False)
    await update_warns(user[0], 0)
    await message.reply(f"✅ Пользователь разбанен.")
    try: await bot.send_message(user[0], "✅ Ваш доступ восстановлен.")
    except: pass

@dp.message(Command("warn"), F.chat.id == ADMIN_GROUP_ID)
async def warn_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user: return
    
    new_warns = user[3] + 1
    if new_warns >= 3:
        await update_ban(user[0], True)
        await update_warns(user[0], new_warns)
        await message.reply("⛔ 3/3 варна. Пользователь забанен.")
        try: await bot.send_message(user[0], "⛔ Вы забанены за нарушения (3/3).")
        except: pass
    else:
        await update_warns(user[0], new_warns)
        await message.reply(f"⚠️ Варн выдан ({new_warns}/3).")
        try: await bot.send_message(user[0], f"⚠️ Предупреждение ({new_warns}/3).")
        except: pass

@dp.message(Command("unwarn"), F.chat.id == ADMIN_GROUP_ID)
async def unwarn_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user: return
    
    new_warns = max(0, user[3] - 1)
    await update_warns(user[0], new_warns)
    await message.reply(f"✅ Варн снят. Теперь: {new_warns}/3")

# --- УПРАВЛЕНИЕ АДМИНАМИ И СТАТИСТИКА (Только Владельцы) ---

@dp.message(Command("add_admin"), F.chat.id == ADMIN_GROUP_ID)
async def add_admin(message: types.Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS: return await message.reply("❌ Нет прав.")
    if not command.args: return await message.reply("Укажите ID.")
    try:
        uid = int(command.args)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
            await db.commit()
        await message.reply(f"✅ Админ {uid} добавлен.")
    except: await message.reply("Ошибка ID.")

@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def stats(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned=1") as c: banned = (await c.fetchone())[0]
    await message.reply(f"📊 <b>Статистика:</b>\nВсего: {total}\nВ бане: {banned}", parse_mode="HTML")

@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS: return
    await message.reply("📢 Отправьте сообщение для рассылки.")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def do_broadcast(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as c: users = await c.fetchall()
    
    await message.reply(f"🚀 Рассылка на {len(users)} чел...")
    count = 0
    for u in users:
        try:
            await message.copy_to(u[0])
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.reply(f"✅ Рассылка завершена. Дошло: {count}")
    await state.clear()

async def main():
    await init_db()
    print("Бот запущен. Владельцы:", OWNER_IDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
