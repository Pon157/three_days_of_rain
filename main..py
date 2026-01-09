import asyncio
import logging
import os
import random
import string
import sys
from datetime import datetime
from typing import Union, List

# Библиотеки для работы с API и Ботом
try:
    from supabase import create_client, Client
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command, CommandStart, CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.exceptions import (
        TelegramForbiddenError, 
        TelegramBadRequest, 
        TelegramRetryAfter,
        TelegramServerError
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Критическая ошибка: Не установлена библиотека! {e}")
    print("Выполните: pip install aiogram supabase python-dotenv")
    sys.exit(1)

# --- ИНИЦИАЛИЗАЦИЯ КОНФИГУРАЦИИ ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = int(os.getenv("OWNER_ID"))

# Настройка логирования в консоль и файл
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_debug.log", encoding='utf-8')
    ]
)
logger = logging.getLogger("AnonSupportBot")

# Проверка наличия всех переменных
if not all([BOT_TOKEN, ADMIN_GROUP_ID, SUPABASE_URL, SUPABASE_KEY, OWNER_ID]):
    logger.critical("Не все переменные окружения найдены в .env файле!")
    sys.exit(1)

# Инициализация объектов
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Состояния FSM
class AdminStates(StatesGroup):
    waiting_for_broadcast_msg = State()

# --- Вспомогательные функции БД ---

def db_get_user(user_id: int = None, topic_id: int = None) -> Union[dict, None]:
    """Универсальный поиск пользователя в Supabase"""
    try:
        query = supabase.table("users").select("*")
        if user_id:
            res = query.eq("user_id", user_id).execute()
        elif topic_id:
            res = query.eq("topic_id", topic_id).execute()
        else:
            return None
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Ошибка чтения БД: {e}")
        return None

def db_sync_user(user: types.User, topic_id: int = None, topic_name: str = None):
    """Синхронизация данных пользователя (Upsert)"""
    payload = {
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "last_seen": datetime.now().isoformat()
    }
    if topic_id:
        payload.update({"topic_id": topic_id, "topic_name": topic_name})
    
    try:
        supabase.table("users").upsert(payload).execute()
        logger.info(f"Синхронизация юзера {user.id} прошла успешно.")
    except Exception as e:
        logger.error(f"Ошибка синхронизации БД: {e}")

def db_update_status(user_id: int, **kwargs):
    """Обновление полей (ban, warns и т.д.)"""
    try:
        kwargs["last_sanction_date"] = datetime.now().isoformat()
        supabase.table("users").update(kwargs).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Ошибка обновления статуса юзера {user_id}: {e}")

# --- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(F.chat.type == "private", CommandStart())
async def handle_start(message: types.Message):
    """Команда /start с твоим оригинальным текстом"""
    user_id = message.from_user.id
    db_user = db_get_user(user_id=user_id)
    
    if db_user and db_user.get('is_banned'):
        return logger.info(f"Забаненный юзер {user_id} пытался нажать старт.")

    db_sync_user(message.from_user)

    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    welcome_text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Знакомо чувство, когда после эпичной битвы хочется отдохнуть и поболтать с кем-то по душам? "
        "Или когда уже не хочется жить из-за тимейтов, которые идут на слив и пикают кого попало?\n\n"
        "<b><a href='https://t.me/Darius_will_bot'>Теперь у тебя есть личный помощник! "
        "Представляем бота поддержки, который всегда готов выслушать все твои проблемы и несчастья и поддержать.</a></b>\n\n"
        "<b><a href='https://t.me/moral_support_ML'>Здесь ты сможешь более подробно ознакомится о каждом нашем персонаже и о самом мире</a></b>"
    )

    try:
        sent_msg = await message.answer_photo(
            photo=photo_url, 
            caption=welcome_text, 
            parse_mode="HTML"
        )
        await bot.pin_chat_message(message.chat.id, sent_msg.message_id)
    except Exception as e:
        logger.warning(f"Не удалось закрепить или отправить фото: {e}")
        await message.answer(welcome_text, parse_mode="HTML", disable_web_page_preview=False)

@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    """Пересылка сообщения админам в Forum Topic"""
    if message.from_user.id == bot.id:
        return

    db_user = db_get_user(user_id=message.from_user.id)
    
    if db_user and db_user.get('is_banned'):
        return

    # Проверка или создание топика
    topic_id = db_user.get('topic_id') if db_user else None
    
    if not topic_id:
        try:
            rnd_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
            topic_name = f"User #{rnd_id}"
            new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, topic_name)
            topic_id = new_topic.message_thread_id
            db_sync_user(message.from_user, topic_id, topic_name)
            
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новый чат открыт!</b>\nЮзер: {message.from_user.full_name}",
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Критическая ошибка создания топика: {e}")
            return await message.answer("⚠️ Ошибка на стороне сервера. Попробуйте позже.")

    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        # Временное уведомление
        status_msg = await message.answer("✅ Доставлено")
        await asyncio.sleep(2)
        await status_msg.delete()
    except TelegramBadRequest:
        logger.warning("Сообщение защищено от копирования, отправляем текст.")
        await bot.send_message(ADMIN_GROUP_ID, f"📎 <i>(Медиа или текст нельзя скопировать напрямую)</i>\n\n{message.text or 'Вложение'}", message_thread_id=topic_id)
    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}")

# --- АДМИН-КОМАНДЫ (В ГРУППЕ) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def admin_info(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    
    target_user = db_get_user(topic_id=message.message_thread_id)
    if not target_user:
        return await message.reply("❌ Пользователь не найден в базе данных.")

    info_card = (
        "👤 <b>Профиль пользователя</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>TG-ID:</b> <code>{target_user['user_id']}</code>\n"
        f"<b>Имя:</b> {target_user['full_name']}\n"
        f"<b>Username:</b> @{target_user['username'] or '—'}\n"
        f"<b>Варны:</b> {target_user['warns']}/3\n"
        f"<b>Статус:</b> {'🚫 ЗАБАНЕН' if target_user['is_banned'] else '✅ АКТИВЕН'}\n"
        f"<b>Последняя санкция:</b> {target_user.get('last_sanction_date', 'Нет')}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await message.reply(info_card, parse_mode="HTML")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def admin_ban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = db_get_user(topic_id=message.message_thread_id)
    if user:
        db_update_status(user['user_id'], is_banned=True)
        await message.reply("🚫 Пользователь заблокирован.")
        try: await bot.send_message(user['user_id'], "🚫 Вы были заблокированы администрацией.")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def admin_unban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = db_get_user(topic_id=message.message_thread_id)
    if user:
        db_update_status(user['user_id'], is_banned=False, warns=0)
        await message.reply("✅ Пользователь разблокирован, варны обнулены.")
        try: await bot.send_message(user['user_id'], "✅ Ваш доступ к поддержке восстановлен.")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def admin_warn(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = db_get_user(topic_id=message.message_thread_id)
    if user:
        current_warns = (user.get('warns') or 0) + 1
        ban_now = current_warns >= 3
        db_update_status(user['user_id'], warns=current_warns, is_banned=ban_now)
        
        msg = f"⚠️ Выдан варн {current_warns}/3."
        if ban_now: msg += "\n🛑 Лимит достигнут, бан выдан автоматически."
        await message.reply(msg)
        try: await bot.send_message(user['user_id'], f"⚠️ Вам выдано предупреждение ({current_warns}/3).")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    try:
        total = supabase.table("users").select("user_id", count="exact").execute().count
        banned = supabase.table("users").select("user_id", count="exact").eq("is_banned", True).execute().count
        await message.reply(f"📊 <b>Статистика бота:</b>\n\nВсего юзеров в базе: {total}\nВ бане: {banned}", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"Ошибка получения статистики: {e}")

# --- ГЛОБАЛЬНАЯ РАССЫЛКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.reply("📢 Отправьте сообщение для рассылки всем пользователям (можно с фото/видео).")
    await state.set_state(AdminStates.waiting_for_broadcast_msg)

@dp.message(AdminStates.waiting_for_broadcast_msg)
async def broadcast_process(message: types.Message, state: FSMContext):
    await state.clear()
    users = supabase.table("users").select("user_id").execute().data
    
    confirm = await message.reply(f"🚀 Начинаю рассылку на {len(users)} чел...")
    success, failed = 0, 0
    
    for u in users:
        try:
            await message.copy_to(u['user_id'])
            success += 1
            await asyncio.sleep(0.05) # Плавная рассылка
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await message.copy_to(u['user_id'])
            success += 1
        except Exception:
            failed += 1
            
    await confirm.edit_text(f"🏁 <b>Рассылка завершена!</b>\n✅ Успешно: {success}\n❌ Не удалось: {failed}", parse_mode="HTML")

# --- ОТВЕТ АДМИНА ПОЛЬЗОВАТЕЛЮ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def handle_admin_reply(message: types.Message):
    """Отправка ответа из топика обратно юзеру"""
    if message.from_user.id == bot.id or (message.text and message.text.startswith("/")):
        return

    user_data = db_get_user(topic_id=message.message_thread_id)
    if user_data:
        try:
            await message.copy_to(user_data['user_id'])
        except TelegramForbiddenError:
            await message.reply("❌ Бот заблокирован пользователем.")
        except Exception as e:
            logger.error(f"Ошибка при ответе админа: {e}")
            await message.reply(f"❌ Ошибка отправки: {e}")

# --- ТОЧКА ВХОДА ---

async def on_startup():
    logger.info("Проверка соединения с Supabase...")
    try:
        supabase.table("users").select("user_id").limit(1).execute()
        logger.info("Соединение с БД успешно!")
    except Exception as e:
        logger.error(f"Ошибка БД при запуске: {e}")

async def run_bot():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"Бот онлайн! Владелец ID: {OWNER_ID}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот выключен пользователем.")
