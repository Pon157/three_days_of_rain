import asyncio
import logging
import os
import random
import string
from datetime import datetime
from dotenv import load_dotenv

# Используем только supabase-py, asyncpg НЕ нужен
from supabase import create_client, Client
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Парсим владельцев
raw_owners = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or ""
OWNER_IDS = [int(oid.strip()) for oid in raw_owners.split(",") if oid.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- БАЗА ДАННЫХ (SUPABASE / POSTGRES) ---

async def get_db_conn():
    """Создает подключение к PostgreSQL."""
    return await asyncpg.connect(DATABASE_URL)

async def get_user_data(user_id=None, topic_id=None):
    conn = await get_db_conn()
    try:
        if user_id:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if topic_id:
            return await conn.fetchrow("SELECT * FROM users WHERE topic_id = $1", topic_id)
    finally:
        await conn.close()

async def register_or_update_user(tg_user: types.User, topic_id=None, topic_name=None):
    """Регистрирует или обновляет данные пользователя (username, ник)."""
    conn = await get_db_conn()
    try:
        if topic_id:
            await conn.execute("""
                INSERT INTO users (user_id, topic_id, topic_name, username, full_name)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE 
                SET topic_id = $2, topic_name = $3, username = $4, full_name = $5
            """, tg_user.id, topic_id, topic_name, tg_user.username, tg_user.full_name)
        else:
            await conn.execute("""
                UPDATE users SET username = $1, full_name = $2 WHERE user_id = $3
            """, tg_user.username, tg_user.full_name, tg_user.id)
    finally:
        await conn.close()

async def update_sanction(user_id, banned=None, warns=None):
    conn = await get_db_conn()
    try:
        if banned is not None:
            await conn.execute("UPDATE users SET is_banned = $1, last_sanction_date = NOW() WHERE user_id = $2", banned, user_id)
        if warns is not None:
            await conn.execute("UPDATE users SET warns = $1, last_sanction_date = NOW() WHERE user_id = $2", warns, user_id)
    finally:
        await conn.close()

# --- ФУНКЦИЯ ОТПРАВКИ ---

async def safe_reply_to_user(chat_id, message: types.Message):
    try:
        await message.copy_to(chat_id)
    except Exception as e:
        logging.warning(f"Copy failed, trying manual: {e}")
        try:
            if message.text:
                await bot.send_message(chat_id, f"🔔 <b>Ответ оператора:</b>\n\n{message.text}", parse_mode="HTML")
            elif message.photo:
                await bot.send_photo(chat_id, message.photo[-1].file_id, caption=message.caption)
            elif message.voice:
                await bot.send_voice(chat_id, message.voice.file_id)
            elif message.video:
                await bot.send_video(chat_id, message.video.file_id)
            elif message.sticker:
                await bot.send_sticker(chat_id, message.sticker.file_id)
        except TelegramForbiddenError:
            logging.error(f"Юзер {chat_id} заблокал бота.")
        except Exception as e2:
            logging.error(f"Ошибка отправки: {e2}")

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = await get_user_data(user_id=message.from_user.id)
    if user and user['is_banned']: return

    await register_or_update_user(message.from_user)
    
    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    text = (
        "👋 <b>Привет, путник!</b>\n\n"
        "Я твой анонимный помощник. Напиши мне что угодно, и оператор ответит тебе прямо здесь."
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
    
    if user and user['is_banned']: return

    async def create_topic():
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        name = f"Anon #{code}"
        try:
            topic = await bot.create_forum_topic(ADMIN_GROUP_ID, name)
            await register_or_update_user(message.from_user, topic.message_thread_id, name)
            await bot.send_message(ADMIN_GROUP_ID, f"🆕 <b>Новый пользователь:</b> {name}", message_thread_id=topic.message_thread_id, parse_mode="HTML")
            return topic.message_thread_id
        except Exception as e:
            logging.error(f"Error topic: {e}")
            return None

    topic_id = user['topic_id'] if user and user['topic_id'] else await create_topic()
    if not topic_id: return

    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        conf = await message.answer("✅ Отправлено")
        await asyncio.sleep(3)
        await conf.delete()
    except TelegramBadRequest:
        new_tid = await create_topic()
        if new_tid: await message.copy_to(ADMIN_GROUP_ID, message_thread_id=new_tid)

# --- КОМАНДЫ ВЛАДЕЛЬЦА (В ГРУППЕ) ---

@dp.message(Command("info"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_info(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user: return await message.reply("Юзер не найден.")

    last_sanct = user['last_sanction_date'].strftime("%Y-%m-%d %H:%M") if user['last_sanction_date'] else "Нет"
    text = (
        f"👤 <b>Данные юзера:</b>\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Ник: {user['full_name']}\n"
        f"Юзернейм: @{user['username'] or 'нет'}\n"
        f"Дата рег: {user['reg_date'].strftime('%Y-%m-%d')}\n"
        f"Варны: {user['warns']}/3\n"
        f"Бан: {'🔴 ДА' if user['is_banned'] else '🟢 НЕТ'}\n"
        f"Посл. санкция: {last_sanct}"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("ban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_ban(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    user = await get_user_data(topic_id=message.message_thread_id)
    if user:
        await update_sanction(user['user_id'], banned=True)
        await message.reply("🚫 Пользователь забанен.")
        try: await bot.send_message(user['user_id'], "🚫 Доступ заблокирован.")
        except: pass

@dp.message(Command("unban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_unban(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    user = await get_user_data(topic_id=message.message_thread_id)
    if user:
        await update_sanction(user['user_id'], banned=False, warns=0)
        await message.reply("✅ Разбанен.")

@dp.message(Command("warn"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_warn(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    user = await get_user_data(topic_id=message.message_thread_id)
    if user:
        new_w = user['warns'] + 1
        await update_sanction(user['user_id'], warns=new_w, banned=(new_w >= 3))
        await message.reply(f"⚠️ Варн {new_w}/3")

@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_stats(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    conn = await get_db_conn()
    total = await conn.fetchval("SELECT count(*) FROM users")
    banned = await conn.fetchval("SELECT count(*) FROM users WHERE is_banned = true")
    await conn.close()
    await message.reply(f"📊 Всего: {total}\n🚫 В бане: {banned}")

@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS: return
    await message.reply("Введите сообщение для рассылки:")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    conn = await get_db_conn()
    users = await conn.fetch("SELECT user_id FROM users WHERE is_banned = false")
    await conn.close()
    
    await message.reply(f"Начинаю рассылку на {len(users)} чел.")
    count = 0
    for u in users:
        try:
            await message.copy_to(u['user_id'])
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.reply(f"Завершено. Получили: {count}")
    await state.clear()

# --- ОТВЕТ АДМИНА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply_handler(message: types.Message):
    if message.text and message.text.startswith("/"): return
    user = await get_user_data(topic_id=message.message_thread_id)
    if user:
        await safe_reply_to_user(user['user_id'], message)

async def main():
    print("Бот запущен. База: Supabase. Владельцы:", OWNER_IDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
