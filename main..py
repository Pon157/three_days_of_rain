import asyncio
import logging
import os
import random
import string
import sys
from datetime import datetime
from typing import Union

# Убедимся, что все библиотеки доступны
try:
    from supabase import create_client, Client
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command, CommandStart
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.exceptions import (
        TelegramForbiddenError, 
        TelegramBadRequest, 
        TelegramRetryAfter
    )
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Ошибка: Не установлены библиотеки! {e}")
    sys.exit(1)

# --- НАСТРОЙКИ ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OWNER_ID = int(os.getenv("OWNER_ID"))

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("AnonBot")

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class BotStates(StatesGroup):
    broadcast = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def get_user(user_id: int = None, topic_id: int = None) -> Union[dict, None]:
    try:
        table = supabase.table("users")
        if user_id:
            res = table.select("*").eq("user_id", user_id).execute()
        elif topic_id:
            res = table.select("*").eq("topic_id", topic_id).execute()
        else:
            return None
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"Ошибка БД (get): {e}")
        return None

def sync_user_data(user: types.User, topic_id: int = None, topic_name: str = None):
    data = {
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "last_seen": datetime.now().isoformat()
    }
    if topic_id:
        data.update({"topic_id": topic_id, "topic_name": topic_name})
    try:
        supabase.table("users").upsert(data).execute()
    except Exception as e:
        logger.error(f"Ошибка БД (sync): {e}")

def update_user_sanctions(user_id: int, **kwargs):
    try:
        kwargs["last_sanction_date"] = datetime.now().isoformat()
        supabase.table("users").update(kwargs).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"Ошибка БД (update): {e}")

# --- ОБРАБОТКА ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(F.chat.type == "private", CommandStart())
async def start_handler(message: types.Message):
    """Стартовое сообщение с твоим текстом"""
    db_user = get_user(user_id=message.from_user.id)
    if db_user and db_user.get("is_banned"):
        return

    sync_user_data(message.from_user)

    photo = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
    text = (
        "👋 <b>Привет, путник мира!</b>\n\n"
        "Знакомо чувство, когда после эпичной битвы хочется отдохнуть и поболтать с кем-то по душам? "
        "Или когда уже не хочется жить из-за тимейтов, которые идут на слив и пикают кого попало?\n\n"
        "<b><a href='https://t.me/Darius_will_bot'>Теперь у тебя есть личный помощник! "
        "Представляем бота поддержки, который всегда готов выслушать все твои проблемы и несчастья и поддержать.</a></b>\n\n"
        "<b><a href='https://t.me/moral_support_ML'>Здесь ты сможешь более подробно ознакомится о каждом нашем персонаже и о самом мире</a></b>"
    )

    try:
        m = await message.answer_photo(photo, caption=text, parse_mode="HTML")
        await bot.pin_chat_message(message.chat.id, m.message_id)
    except:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)

@dp.message(F.chat.type == "private")
async def forward_to_admin(message: types.Message):
    """Пересылка сообщений от пользователя к админам"""
    # Игнорируем самого бота и сервисные сообщения
    if message.from_user.id == bot.id or message.type in [types.ContentType.NEW_CHAT_MEMBERS, types.ContentType.LEFT_CHAT_MEMBER]:
        return

    db_user = get_user(user_id=message.from_user.id)
    if db_user and db_user.get("is_banned"):
        return

    # Проверяем наличие топика
    topic_id = db_user.get("topic_id") if db_user else None

    if not topic_id:
        try:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
            t_name = f"Anon {code}"
            new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, t_name)
            topic_id = new_topic.message_thread_id
            sync_user_data(message.from_user, topic_id, t_name)
            
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новый пользователь:</b> {message.from_user.full_name}",
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка создания топика: {e}")
            return await message.answer("Ошибка связи. Попробуйте позже.")

    # Пытаемся скопировать сообщение
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        # Подтверждение (удаляется через 2 сек)
        confirm = await message.answer("✅ Отправлено")
        await asyncio.sleep(2)
        await confirm.delete()
    except TelegramBadRequest:
        # Если копия невозможна (защищенный контент), шлем текстом
        await bot.send_message(
            ADMIN_GROUP_ID, 
            f"⚠️ (Защищено) Сообщение:\n{message.text or '[Медиа без текста]'}", 
            message_thread_id=topic_id
        )

# --- АДМИН КОМАНДЫ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def info_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user(topic_id=message.message_thread_id)
    if not user: return await message.reply("Пользователь не найден.")

    await message.reply(
        f"👤 <b>Инфо:</b>\nID: <code>{user['user_id']}</code>\n"
        f"Имя: {user['full_name']}\n"
        f"Варны: {user['warns']}/3\n"
        f"Бан: {'Да' if user['is_banned'] else 'Нет'}",
        parse_mode="HTML"
    )

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def ban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user(topic_id=message.message_thread_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=True)
        await message.reply("🚫 Забанен.")
        try: await bot.send_message(user['user_id'], "🚫 Доступ ограничен.")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def unban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user(topic_id=message.message_thread_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=False, warns=0)
        await message.reply("✅ Разбанен.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def warn_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    user = get_user(topic_id=message.message_thread_id)
    if user:
        new_w = (user['warns'] or 0) + 1
        ban_it = new_w >= 3
        update_user_sanctions(user['user_id'], warns=new_w, is_banned=ban_it)
        await message.reply(f"⚠️ Варн {new_w}/3. {'(Авто-бан)' if ban_it else ''}")
        try: await bot.send_message(user['user_id'], f"⚠️ Предупреждение {new_w}/3")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    all_users = supabase.table("users").select("user_id", count="exact").execute()
    banned = supabase.table("users").select("user_id", count="exact").eq("is_banned", True).execute()
    await message.reply(f"📊 Всего: {all_users.count}\n🚫 В бане: {banned.count}")

# --- РАССЫЛКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def start_bc(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.reply("Пришлите сообщение для рассылки:")
    await state.set_state(BotStates.broadcast)

@dp.message(BotStates.broadcast)
async def do_bc(message: types.Message, state: FSMContext):
    await state.clear()
    users = supabase.table("users").select("user_id").execute().data
    
    await message.reply(f"📢 Начинаю рассылку на {len(users)} чел.")
    ok, err = 0, 0
    for u in users:
        try:
            await message.copy_to(u['user_id'])
            ok += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await message.copy_to(u['user_id'])
            ok += 1
        except:
            err += 1
    await message.reply(f"🏁 Готово! ✅ {ok} | ❌ {err}")

# --- ОТВЕТ АДМИНА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def admin_to_user(message: types.Message):
    # Строжайшая проверка, чтобы не копировать лишнее
    if message.from_user.id == bot.id or (message.text and message.text.startswith("/")):
        return

    # Проверяем, что это не сервисное уведомление Telegram
    if not message.from_user or message.is_automatic_forward:
        return

    user = get_user(topic_id=message.message_thread_id)
    if user:
        try:
            await message.copy_to(user['user_id'])
        except Exception as e:
            logger.error(f"Ошибка доставки ответа: {e}")
            await message.reply("Не удалось отправить. Возможно, бот заблокирован.")

# --- ЗАПУСК ---

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
