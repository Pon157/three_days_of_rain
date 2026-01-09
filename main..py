import asyncio
import logging
import os
import random
import string
from datetime import datetime
from dotenv import load_dotenv

# Работаем через API Supabase (библиотека supabase-py)
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
OWNER_ID = int(os.getenv("OWNER_ID"))

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация клиента Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- ФУНКЦИИ БАЗЫ ДАННЫХ (SUPABASE API) ---

def get_user_data(user_id=None, topic_id=None):
    """Получение данных пользователя из таблицы 'users'"""
    try:
        if user_id:
            res = supabase.table("users").select("*").eq("user_id", user_id).execute()
        else:
            res = supabase.table("users").select("*").eq("topic_id", topic_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logging.error(f"Ошибка получения данных: {e}")
        return None

def sync_user(tg_user: types.User, topic_id=None, topic_name=None):
    """Синхронизация (создание или обновление) профиля пользователя"""
    data = {
        "user_id": tg_user.id,
        "username": tg_user.username,
        "full_name": tg_user.full_name,
    }
    if topic_id:
        data.update({"topic_id": topic_id, "topic_name": topic_name})
    
    try:
        # upsert автоматически создаст запись или обновит существующую по user_id
        supabase.table("users").upsert(data).execute()
    except Exception as e:
        logging.error(f"Ошибка синхронизации в Supabase: {e}")

def update_sanction(user_id: int, banned: bool = None, warns: int = None):
    """Обновление статуса бана или количества предупреждений"""
    update_data = {"last_sanction_date": datetime.now().isoformat()}
    if banned is not None: update_data["is_banned"] = banned
    if warns is not None: update_data["warns"] = warns
    
    try:
        supabase.table("users").update(update_data).eq("user_id", user_id).execute()
    except Exception as e:
        logging.error(f"Ошибка при сохранении санкции: {e}")

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ (ЛИЧКА) ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = get_user_data(user_id=message.from_user.id)
    if user and user.get('is_banned'):
        return

    sync_user(message.from_user)
    
    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Я твой бот поддержки. Напиши мне свой вопрос, и оператор ответит тебе здесь анонимно."
    )
    try:
        await message.answer_photo(photo_url, caption=text, parse_mode="HTML")
    except:
        await message.answer(text, parse_mode="HTML")

@dp.message(F.chat.type == "private")
async def user_msg(message: types.Message):
    user = get_user_data(user_id=message.from_user.id)
    if user and user.get('is_banned'):
        return

    # Если топика еще нет, создаем его
    if not user or not user.get('topic_id'):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        name = f"Anon #{code}"
        try:
            topic = await bot.create_forum_topic(ADMIN_GROUP_ID, name)
            sync_user(message.from_user, topic.message_thread_id, name)
            topic_id = topic.message_thread_id
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новое обращение:</b> {name}", 
                message_thread_id=topic_id, 
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            return await message.answer("Ошибка связи с сервером поддержки.")
    else:
        topic_id = user['topic_id']

    # Пересылаем сообщение в топик админам
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        # Подтверждение отправки (удаляется через 3 сек)
        conf = await message.answer("✅ Сообщение отправлено")
        await asyncio.sleep(3)
        await conf.delete()
    except Exception:
        # Если топик был удален вручную, создаем новый и пробуем снова
        new_name = f"Anon-Retry #{''.join(random.choices(string.digits, k=4))}"
        new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, new_name)
        sync_user(message.from_user, new_topic.message_thread_id, new_name)
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=new_topic.message_thread_id)

# --- ХЕНДЛЕРЫ ВЛАДЕЛЬЦА (В ГРУППЕ / ТОПИКАХ) ---

@dp.message(Command("info"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_info(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if not user: return await message.reply("Пользователь в базе не найден.")

    last_sanc = user.get('last_sanction_date') or "Нет данных"
    text = (
        f"👤 <b>Карточка пользователя:</b>\n\n"
        f"<b>ID:</b> <code>{user['user_id']}</code>\n"
        f"<b>Имя:</b> {user['full_name']}\n"
        f"<b>Username:</b> @{user['username'] or 'скрыт'}\n"
        f"<b>Варны:</b> {user['warns']}/3\n"
        f"<b>Статус:</b> {'🛑 ЗАБАНЕН' if user['is_banned'] else '✅ Активен'}\n"
        f"<b>Посл. санкция:</b> {last_sanc}"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("ban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_ban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        update_sanction(user['user_id'], banned=True)
        await message.reply("🚫 Пользователь заблокирован в боте.")
        try: await bot.send_message(user['user_id'], "🚫 Вы были заблокированы администрацией.")
        except: pass

@dp.message(Command("unban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_unban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        update_sanction(user['user_id'], banned=False, warns=0)
        await message.reply("✅ Пользователь полностью разбанен.")
        try: await bot.send_message(user['user_id'], "✅ Ваш доступ к боту восстановлен.")
        except: pass

@dp.message(Command("warn"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_warn(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        new_warns = user['warns'] + 1
        is_banning = new_warns >= 3
        update_sanction(user['user_id'], warns=new_warns, banned=is_banning)
        
        reply = f"⚠️ Выдан варн ({new_warns}/3)."
        if is_banning: reply += "\n🚫 Пользователь забанен автоматически."
        await message.reply(reply)
        try: await bot.send_message(user['user_id'], f"⚠️ Предупреждение ({new_warns}/3).")
        except: pass

@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    try:
        # Считаем количество записей через API
        res_total = supabase.table("users").select("user_id", count="exact").execute()
        res_banned = supabase.table("users").select("user_id", count="exact").eq("is_banned", True).execute()
        await message.reply(f"📊 <b>Статистика БД:</b>\nЮзеров: {res_total.count}\nВ бане: {res_banned.count}", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"Ошибка получения статистики: {e}")

# --- ОТВЕТ ОПЕРАТОРА (ОБРАТНАЯ СВЯЗЬ) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply(message: types.Message):
    # Не пересылаем команды
    if message.text and message.text.startswith("/"): return
    
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        try:
            await message.copy_to(user['user_id'])
        except TelegramForbiddenError:
            logging.warning(f"Пользователь {user['user_id']} заблокировал бота.")
        except Exception as e:
            logging.error(f"Ошибка доставки: {e}")

# --- ЗАПУСК БОТА ---

async def main():
    print(f"--- Бот запущен (ID владельца: {OWNER_ID}) ---")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
