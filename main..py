import asyncio
import logging
import os
import aiosqlite
import random
import string
import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))

# Проверка токена
if not BOT_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в .env")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_NAME = "anon_chat.db"
START_TIME = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# --- СОСТОЯНИЯ ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                topic_name TEXT,
                warns INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT 0,
                reg_date TEXT
            )
        """)
        await db.commit()

async def get_user_by_id(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_by_topic(topic_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE topic_id = ?", (topic_id,)) as cursor:
            return await cursor.fetchone()

async def create_user(user_id, topic_id, topic_name):
    reg_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, topic_id, topic_name, reg_date) VALUES (?, ?, ?, ?)", 
            (user_id, topic_id, topic_name, reg_date)
        )
        await db.commit()

async def update_ban(user_id, is_banned):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (is_banned, user_id))
        await db.commit()

async def update_warns(user_id, count):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (count, user_id))
        await db.commit()

async def get_stats_data():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned=1") as c: banned = (await c.fetchone())[0]
    return total, banned

async def get_all_users_ids():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            return await c.fetchall()

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = await get_user_by_id(message.from_user.id)
    if user and user[4]: return # Бан

    # Ссылка на картинку (можешь заменить на свою или удалить этот блок)
    photo_url = "https://cdn-icons-png.flaticon.com/512/9703/9703596.png" 
    text = (
        "👋 <b>Добро пожаловать в Анонимный Чат!</b>\n\n"
        "Все, что вы напишете здесь, будет отправлено нашим операторам анонимно.\n"
        "Мы не видим вашего профиля, имени или ID.\n\n"
        "✍️ <i>Просто отправьте сообщение...</i>"
    )
    
    try:
        await message.answer_photo(photo=photo_url, caption=text, parse_mode="HTML")
    except:
        await message.answer(text, parse_mode="HTML")

@dp.message(F.chat.type == "private")
async def user_message(message: types.Message):
    user_id = message.from_user.id
    user = await get_user_by_id(user_id)
    
    if user and user[4]: return # Бан
    
    topic_id = None
    if not user:
        # Создаем нового юзера
        anon_name = f"Anon #{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
        try:
            topic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=anon_name)
            topic_id = topic.message_thread_id
            await create_user(user_id, topic_id, anon_name)
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новый пользователь:</b> {anon_name}\nID топика: {topic_id}", 
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            await message.answer("Ошибка системы. Попробуйте позже.")
            return
    else:
        topic_id = user[1]

    # Пересылка сообщения админам
    try:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
    except Exception as e:
        await message.answer("Не удалось отправить сообщение.")

# --- ХЕНДЛЕРЫ АДМИНОВ (Только в группе) ---

# Фильтр: Сообщение в группе админов, но НЕ команда
@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id, ~F.text.startswith("/"))
async def admin_reply(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    
    # Если это топик General (id=None) или юзер не найден - игнор
    if not user: return

    try:
        await message.copy_to(chat_id=user[0])
    except TelegramForbiddenError:
        await message.reply("❌ Пользователь заблокировал бота.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

# Команда /ban
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Это не топик пользователя.")
    
    await update_ban(user[0], True)
    await message.reply(f"⛔ Пользователь {user[2]} <b>ЗАБАНЕН</b>.", parse_mode="HTML")
    try: await bot.send_message(user[0], "⛔ Вы были заблокированы администрацией.")
    except: pass

# Команда /unban
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def cmd_unban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Это не топик пользователя.")
    
    await update_ban(user[0], False)
    await message.reply(f"✅ Пользователь {user[2]} <b>РАЗБАНЕН</b>.", parse_mode="HTML")
    try: await bot.send_message(user[0], "✅ Доступ восстановлен.")
    except: pass

# Команда /warn
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def cmd_warn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Это не топик пользователя.")
    
    count = user[3] + 1
    await update_warns(user[0], count)
    await message.reply(f"⚠️ Варн выдан. Всего: {count}")
    try: await bot.send_message(user[0], f"⚠️ Вам выдано предупреждение. Нарушений: {count}")
    except: pass

# Команда /unwarn
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unwarn"))
async def cmd_unwarn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Это не топик пользователя.")
    
    count = max(0, user[3] - 1)
    await update_warns(user[0], count)
    await message.reply(f"✅ Варн снят. Всего: {count}")

# Команда /stats (Статистика)
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    total, banned = await get_stats_data()
    active = total - banned
    
    text = (
        f"📊 <b>Статистика системы:</b>\n\n"
        f"👥 Всего юзеров в базе: <b>{total}</b>\n"
        f"✅ Активных: <b>{active}</b>\n"
        f"🚫 Забаненных: <b>{banned}</b>\n"
        f"🚀 Бот запущен: {START_TIME}"
    )
    await message.reply(text, parse_mode="HTML")

# --- РАССЫЛКА (BROADCAST) ---
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    await message.reply("📢 <b>Режим рассылки</b>\nОтправьте сообщение (текст, фото, видео), которое получат все пользователи.\n/cancel - отмена", parse_mode="HTML")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("cancel"))
async def cancel_br(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Отменено.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    users = await get_all_users_ids()
    if not users:
        await message.reply("Нет пользователей для рассылки.")
        await state.clear()
        return

    msg = await message.reply(f"⏳ Начинаю рассылку на {len(users)} пользователей...")
    good, bad = 0, 0
    
    for u in users:
        try:
            await message.copy_to(chat_id=u[0])
            good += 1
            await asyncio.sleep(0.05) # Анти-спам задержка
        except:
            bad += 1
            
    await msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n"
        f"✅ Доставлено: {good}\n"
        f"❌ Недоставлено (блок): {bad}",
        parse_mode="HTML"
    )
    await state.clear()

# --- ЗАПУСК ---
async def main():
    await init_db()
    print("Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
