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
from aiogram.types import Message

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
OWNER_ID = int(os.getenv("OWNER_ID")) # ID главного админа

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
        # Таблица пользователей
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
        # Таблица администраторов (кто может делать рассылку и смотреть стату)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.commit()

# --- ФУНКЦИИ БД ---
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

async def is_admin(user_id):
    if user_id == OWNER_ID: return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def add_admin_db(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def del_admin_db(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
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
    if user and user[4]: 
        return # Бан

    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Знакомо чувство, когда после эпичной битвы хочется отдохнуть и поболтать с кем-то по душам? Или когда уже не хочется жить из-за тимейтов, которые идут на слив и пикают кого попало?\n\n"
        "<b><a href='https://t.me/Darius_will_bot'>Теперь у тебя есть личный помощник! Представляем бота поддержки, который всегда готов выслушать все твои проблемы и несчастья и поддержать.</a></b>\n\n"
        "<b><a href='https://t.me/moral_support_ML'>Здесь ты сможешь более подробно ознакомится о каждом нашем персонаже и о самом мире</a></b>"
    )

    try:
        sent_msg = await message.answer_photo(photo=photo_url, caption=text, parse_mode="HTML")
        # Закрепляем сообщение
        await bot.pin_chat_message(chat_id=message.chat.id, message_id=sent_msg.message_id)
    except Exception:
        await message.answer(text, parse_mode="HTML")

@dp.message(F.chat.type == "private")
async def user_message(message: types.Message):
    user_id = message.from_user.id
    user = await get_user_by_id(user_id)
    
    if user and user[4]: 
        return # Бан (игнор)
    
    topic_id = None
    if not user:
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
            await message.answer("Ошибка системы.")
            return
    else:
        topic_id = user[1]

    # Пересылка сообщения админам
    try:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
        
        # Уведомление "Отправлено" и удаление через 5 сек
        sent_confirm = await message.answer("✅ Сообщение отправлено")
        await asyncio.sleep(5)
        await sent_confirm.delete()
        
    except Exception as e:
        pass # Ошибки при удалении или отправке игнорируем, чтобы не спамить

# --- ХЕНДЛЕРЫ АДМИНОВ (Только в группе) ---

# Ответ админа юзеру
@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id, ~F.text.startswith("/"))
async def admin_reply(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: 
        return

    try:
        await message.copy_to(chat_id=user[0])
    except TelegramForbiddenError:
        await message.reply("❌ Юзер заблокировал бота.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

# БАН
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: 
        return await message.reply("Это не топик юзера.")
    
    await update_ban(user[0], True)
    await message.reply(f"⛔ Пользователь {user[2]} <b>ЗАБАНЕН</b>.", parse_mode="HTML")
    try: 
        await bot.send_message(user[0], "⛔ Вы были заблокированы.")
    except: 
        pass

# РАЗБАН
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def cmd_unban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: 
        return await message.reply("Это не топик юзера.")
    
    await update_ban(user[0], False)
    await message.reply(f"✅ Пользователь {user[2]} <b>РАЗБАНЕН</b>.", parse_mode="HTML")
    try: 
        await bot.send_message(user[0], "✅ Доступ восстановлен.")
    except: 
        pass

# ВАРН (с автобаном на 3-м варне)
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def cmd_warn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: 
        return await message.reply("Это не топик юзера.")
    
    current_warns = user[3]
    new_warns = current_warns + 1
    
    if new_warns >= 3:
        # Автоматический бан
        await update_warns(user[0], new_warns)
        await update_ban(user[0], True)
        await message.reply(f"⚠️ Варн 3/3. ⛔ <b>Пользователь автоматически забанен.</b>", parse_mode="HTML")
        try: 
            await bot.send_message(user[0], "⛔ Вы получили 3 предупреждения и были заблокированы.")
        except: 
            pass
    else:
        # Просто выдача варна
        await update_warns(user[0], new_warns)
        await message.reply(f"⚠️ Варн выдан. ({new_warns}/3)")
        try: 
            await bot.send_message(user[0], f"⚠️ Вам выдано предупреждение ({new_warns}/3). При 3 нарушениях — бан.")
        except: 
            pass

# УДАЛЕНИЕ ВАРНА
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unwarn"))
async def cmd_unwarn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: 
        return await message.reply("Это не топик юзера.")
    
    new_warns = max(0, user[3] - 1)
    await update_warns(user[0], new_warns)
    await message.reply(f"✅ Варн снят. Теперь: {new_warns}/3")

# --- УПРАВЛЕНИЕ АДМИНАМИ И СТАТИСТИКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("add_admin"))
async def cmd_add_admin(message: types.Message, command: CommandObject):
    # Только Владелец может добавлять админов
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Доступ запрещен. Только Владелец может добавлять админов.")
    
    if not command.args:
        return await message.reply("Использование: `/add_admin 123456789`")
    
    try:
        new_admin_id = int(command.args)
        await add_admin_db(new_admin_id)
        await message.reply(f"✅ Пользователь {new_admin_id} добавлен в список админов (доступ к stats/broadcast).")
    except ValueError:
        await message.reply("ID должен быть числом.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("del_admin"))
async def cmd_del_admin(message: types.Message, command: CommandObject):
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Доступ запрещен.")
    
    if not command.args:
        return await message.reply("Использование: `/del_admin 123456789`")
    
    try:
        rem_admin_id = int(command.args)
        await del_admin_db(rem_admin_id)
        await message.reply(f"🗑️ Админ {rem_admin_id} удален.")
    except ValueError:
        await message.reply("ID должен быть числом.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    # Проверка прав (Владелец ИЛИ есть в таблице admins)
    if not await is_admin(message.from_user.id):
        return await message.reply("❌ У вас нет прав на просмотр статистики.")

    total, banned = await get_stats_data()
    active = total - banned
    
    text = (
        f"📊 <b>Статистика системы:</b>\n\n"
        f"👥 Всего юзеров: <b>{total}</b>\n"
        f"✅ Активных: <b>{active}</b>\n"
        f"🚫 Забаненных: <b>{banned}</b>\n"
        f"🚀 Аптайм с: {START_TIME}"
    )
    await message.reply(text, parse_mode="HTML")

# --- РАССЫЛКА (Только для админов) ---
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return await message.reply("❌ У вас нет прав на рассылку.")

    await message.reply("📢 <b>Режим рассылки</b>\nОтправьте сообщение, которое получат ВСЕ пользователи.\n/cancel - отмена", parse_mode="HTML")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("cancel"))
async def cancel_br(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply("Отменено.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return # Двойная проверка на всякий случай

    users = await get_all_users_ids()
    if not users:
        await message.reply("Нет пользователей.")
        await state.clear()
        return

    msg = await message.reply(f"⏳ Рассылка на {len(users)} юзеров...")
    good, bad = 0, 0
    
    for u in users:
        try:
            await message.copy_to(chat_id=u[0])
            good += 1
            await asyncio.sleep(0.05) 
        except:
            bad += 1
            
    await msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n"
        f"✅ Успешно: {good}\n"
        f"❌ Блок/Ошибки: {bad}",
        parse_mode="HTML"
    )
    await state.clear()

async def main():
    await init_db()
    # Добавляем владельца в БД админов при старте, чтобы не потерять доступ
    await add_admin_db(OWNER_ID)
    print("Бот запущен! Owner ID:", OWNER_ID)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
