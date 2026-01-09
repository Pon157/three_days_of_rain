import asyncio
import logging
import os
import random
import string
from datetime import datetime
from dotenv import load_dotenv

# Используем API SDK Supabase
from supabase import create_client, Client
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = int(os.getenv("OWNER_ID"))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация клиента Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Состояния для рассылки
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
        logger.error(f"Ошибка получения данных: {e}")
        return None

def sync_user(tg_user: types.User, topic_id=None, topic_name=None):
    """Создание или обновление профиля пользователя (UPSERT)"""
    data = {
        "user_id": tg_user.id,
        "username": tg_user.username,
        "full_name": tg_user.full_name,
    }
    if topic_id:
        data.update({"topic_id": topic_id, "topic_name": topic_name})
    
    try:
        supabase.table("users").upsert(data).execute()
    except Exception as e:
        logger.error(f"Ошибка синхронизации: {e}")

def update_sanction(user_id: int, banned: bool = None, warns: int = None):
    """Обновление статуса бана или количества предупреждений"""
    update_data = {"last_sanction_date": datetime.now().isoformat()}
    if banned is not None: update_data["is_banned"] = banned
    if warns is not None: update_data["warns"] = warns
    
    try:
        supabase.table("users").update(update_data).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Ошибка обновления санкций: {e}")

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ (ЛИЧКА) ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = get_user_data(user_id=message.from_user.id)
    if user and user.get('is_banned'):
        return

    sync_user(message.from_user)
    
    photo_url = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    
    # ТВОЙ ОРИГИНАЛЬНЫЙ ТЕКСТ
    text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Знакомо чувство, когда после эпичной битвы хочется отдохнуть и поболтать с кем-то по душам? Или когда уже не хочется жить из-за тимейтов, которые идут на слив и пикают кого попало?\n\n"
        "<b><a href='https://t.me/Darius_will_bot'>Теперь у тебя есть личный помощник! Представляем бота поддержки, который всегда готов выслушать все твои проблемы и несчастья и поддержать.</a></b>\n\n"
        "<b><a href='https://t.me/moral_support_ML'>Здесь ты сможешь более подробно ознакомится о каждом нашем персонаже и о самом мире</a></b>"
    )
    
    try:
        msg = await message.answer_photo(photo_url, caption=text, parse_mode="HTML")
        await bot.pin_chat_message(message.chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Ошибка при команде start: {e}")
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)

@dp.message(F.chat.type == "private")
async def user_msg(message: types.Message):
    """Логика пересылки сообщения от пользователя админам"""
    user = get_user_data(user_id=message.from_user.id)
    if user and user.get('is_banned'):
        return

    # Создание топика, если его нет
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
            logger.error(f"Ошибка создания топика: {e}")
            return await message.answer("Ошибка связи с сервером поддержки.")
    else:
        topic_id = user['topic_id']

    # Пересылка
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        conf = await message.answer("✅ Отправлено")
        await asyncio.sleep(3)
        await conf.delete()
    except Exception:
        # Если топик удален, пересоздаем
        new_name = f"Anon-Retry #{''.join(random.choices(string.digits, k=4))}"
        new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, new_name)
        sync_user(message.from_user, new_topic.message_thread_id, new_name)
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=new_topic.message_thread_id)

# --- ФУНКЦИИ ВЛАДЕЛЬЦА (В ГРУППЕ АДМИНОВ) ---

@dp.message(Command("info"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_info(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if not user: return await message.reply("Пользователь не найден в базе.")

    last_sanc = user.get('last_sanction_date') or "Нет"
    text = (
        f"👤 <b>Карточка пользователя:</b>\n\n"
        f"<b>ID:</b> <code>{user['user_id']}</code>\n"
        f"<b>Имя:</b> {user['full_name']}\n"
        f"<b>Username:</b> @{user['username'] or 'скрыт'}\n"
        f"<b>Варны:</b> {user['warns']}/3\n"
        f"<b>Статус:</b> {'🛑 ЗАБАНЕН' if user['is_banned'] else '✅ Активен'}\n"
        f"<b>Последняя санкция:</b> {last_sanc}"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("ban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_ban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        update_sanction(user['user_id'], banned=True)
        await message.reply("🚫 Пользователь заблокирован.")
        try: await bot.send_message(user['user_id'], "🚫 Вы были заблокированы администрацией.")
        except: pass

@dp.message(Command("unban"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_unban(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        update_sanction(user['user_id'], banned=False, warns=0)
        await message.reply("✅ Пользователь разбанен.")
        try: await bot.send_message(user['user_id'], "✅ Ваш доступ к боту восстановлен.")
        except: pass

@dp.message(Command("warn"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_warn(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        new_warns = (user['warns'] or 0) + 1
        is_banning = new_warns >= 3
        update_sanction(user['user_id'], warns=new_warns, banned=is_banning)
        
        reply = f"⚠️ Выдан варн ({new_warns}/3)."
        if is_banning: reply += "\n🚫 Лимит превышен, бан выдан автоматически."
        await message.reply(reply)
        try: await bot.send_message(user['user_id'], f"⚠️ Вам выдано предупреждение ({new_warns}/3).")
        except: pass

@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def cmd_stats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    try:
        res_total = supabase.table("users").select("user_id", count="exact").execute()
        res_banned = supabase.table("users").select("user_id", count="exact").eq("is_banned", True).execute()
        await message.reply(
            f"📊 <b>Статистика:</b>\n"
            f"Всего пользователей: {res_total.count}\n"
            f"В черном списке: {res_banned.count}", 
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"Ошибка статистики: {e}")

# --- ФУНКЦИЯ РАССЫЛКИ (BROADCAST) ---

@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def start_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.reply("Отправьте сообщение (текст, фото или видео), которое нужно разослать всем пользователям.")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Получаем всех пользователей из базы
    try:
        users_res = supabase.table("users").select("user_id").execute()
        all_users = [u['user_id'] for u in users_res.data]
    except Exception as e:
        return await message.reply(f"Ошибка получения списка юзеров: {e}")

    await message.reply(f"🚀 Начинаю рассылку на {len(all_users)} пользователей...")
    
    count = 0
    blocked = 0
    
    for user_id in all_users:
        try:
            await message.copy_to(user_id)
            count += 1
            await asyncio.sleep(0.05) # Защита от спам-фильтра Telegram
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await message.copy_to(user_id)
            count += 1
        except Exception:
            pass
            
    await message.reply(f"🏁 Рассылка завершена!\n✅ Получили: {count}\n❌ Заблокировали бота: {blocked}")

# --- ОТВЕТ ОПЕРАТОРА (В ТОПИКЕ) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply(message: types.Message):
    # Игнорируем команды
    if message.text and message.text.startswith("/"): return
    
    user = get_user_data(topic_id=message.message_thread_id)
    if user:
        try:
            await message.copy_to(user['user_id'])
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user['user_id']}: {e}")

# --- ЗАПУСК ---

async def main():
    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
