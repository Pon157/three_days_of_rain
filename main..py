import asyncio
import logging
import os
import random
import string
import sys
from datetime import datetime
from typing import Union, Optional

# --- ИМПОРТЫ ---
try:
    from supabase import create_client, Client
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command, CommandStart
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
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

# Проверка переменных
if not all([BOT_TOKEN, ADMIN_GROUP_ID, SUPABASE_URL, SUPABASE_KEY, OWNER_ID]):
    print("Ошибка: Не все переменные окружения (.env) заполнены!")
    sys.exit(1)

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

# Состояния для рассылки
class BotStates(StatesGroup):
    broadcast = State()

# --- КЛАВИАТУРЫ ---

def get_main_keyboard():
    """Создает клавиатуру с кнопками Поддержка и Общение"""
    kb = [
        [KeyboardButton(text="🆘 Поддержка"), KeyboardButton(text="🗣 Общение")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

# --- РАБОТА С БАЗОЙ ДАННЫХ ---

def get_user(user_id: int = None, topic_id: int = None) -> Union[dict, None]:
    """Получить пользователя из БД"""
    try:
        if user_id:
            res = supabase.table("users").select("*").eq("user_id", user_id).execute()
        elif topic_id:
            res = supabase.table("users").select("*").eq("topic_id", topic_id).execute()
        else:
            return None
        
        if hasattr(res, 'data') and res.data:
            return res.data[0]
        return None
    except Exception as e:
        logger.error(f"Ошибка БД (get_user): {e}")
        return None

def sync_user_data(user: types.User, topic_id: int = None, topic_name: str = None):
    """Синхронизировать данные пользователя с БД"""
    try:
        existing_user = get_user(user_id=user.id)
        
        data = {
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "last_seen": datetime.now().isoformat()
        }
        
        # Если передали topic_id, сохраняем его. Если нет — оставляем старый.
        if topic_id:
            data["topic_id"] = topic_id
            data["topic_name"] = topic_name
        elif existing_user and existing_user.get("topic_id"):
            data["topic_id"] = existing_user.get("topic_id")
            data["topic_name"] = existing_user.get("topic_name")
        
        supabase.table("users").upsert(data).execute()
        logger.info(f"Синхронизирован пользователь {user.id}")
        
    except Exception as e:
        logger.error(f"Ошибка БД (sync_user_data): {e}")

def update_user_sanctions(user_id: int, **kwargs):
    """Обновить баны/варны пользователя"""
    try:
        kwargs["last_sanction_date"] = datetime.now().isoformat()
        supabase.table("users").update(kwargs).eq("user_id", user_id).execute()
        logger.info(f"Обновлены санкции для {user_id}: {kwargs}")
    except Exception as e:
        logger.error(f"Ошибка БД (update_user_sanctions): {e}")

# --- ОБРАБОТКА ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(F.chat.type == "private", CommandStart())
async def start_handler(message: types.Message):
    """Команда /start"""
    try:
        db_user = get_user(user_id=message.from_user.id)
        if db_user and db_user.get("is_banned"):
            await message.answer("🚫 Ваш доступ ограничен.")
            return

        sync_user_data(message.from_user)
        
        # Уведомляем админов, если старый юзер вернулся
        if db_user and db_user.get("topic_id"):
            topic_id = db_user.get("topic_id")
            try:
                await bot.send_message(
                    ADMIN_GROUP_ID,
                    f"🔄 <b>Пользователь вернулся:</b> {message.from_user.full_name}",
                    message_thread_id=topic_id,
                    parse_mode="HTML"
                )
            except:
                pass 

        photo = "https://i.postimg.cc/RFrwrtY8/photo-2026-01-07-11-42-49.jpg"
        text = (
            "👋 <b>Привет, путник мира!</b>\n\n"
            "Знакомо чувство, когда после эпичной битвы хочется отдохнуть и поболтать с кем-то по душам? "
            "Или когда уже не хочется жить из-за тимейтов, которые идут на слив и пикают кого попало?\n\n"
            "<b><a href='https://t.me/Darius_will_bot'>Теперь у тебя есть личный помощник!</a></b>\n"
            "Представляем бота поддержки, который всегда готов выслушать все твои проблемы и несчастья и поддержать.\n\n"
            "👇 <b>Выберите категорию ниже или просто напишите сообщение:</b>"
        )

        try:
            m = await message.answer_photo(
                photo, 
                caption=text, 
                parse_mode="HTML",
                reply_markup=get_main_keyboard() # <--- КЛАВИАТУРА ЗДЕСЬ
            )
            await bot.pin_chat_message(message.chat.id, m.message_id)
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())
            
    except Exception as e:
        logger.error(f"Ошибка в start_handler: {e}")

# Фильтр ~F.text.startswith("/") важен, чтобы не ловить команды как сообщения
@dp.message(F.chat.type == "private", ~F.text.startswith("/"))
async def forward_to_admin(message: types.Message):
    """Пересылка сообщений и обработка кнопок"""
    try:
        # Игнор служебных сообщений
        if message.from_user.is_bot or message.content_type in ["new_chat_members", "left_chat_member"]:
            return

        db_user = get_user(user_id=message.from_user.id)
        
        # Проверка бана
        if db_user and db_user.get("is_banned"):
            return

        # Ищем или создаем топик
        topic_id = db_user.get("topic_id") if db_user else None
        
        if not topic_id:
            # Создаем нового пользователя/топик
            sync_user_data(message.from_user) 
            try:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
                t_name = f"Anon {code} | {message.from_user.first_name}"
                new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, t_name)
                topic_id = new_topic.message_thread_id
                
                sync_user_data(message.from_user, topic_id, t_name)
                
                await bot.send_message(
                    ADMIN_GROUP_ID, 
                    f"🆕 <b>Новый диалог</b>",
                    message_thread_id=topic_id,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка создания топика: {e}")
                await message.answer("⚠️ Ошибка соединения. Попробуйте позже.")
                return

        # --- ЛОГИКА КНОПОК ---
        if message.text == "🆘 Поддержка":
            admin_text = "🛑 <b>ЗАПРОС ПОДДЕРЖКИ!</b>\nПользователь нажал кнопку.\nТег: #ПОДДЕРЖКА"
            # Ответ пользователю
            await message.answer("Принято. Админ поддержки скоро ответит вам.", reply_markup=get_main_keyboard())
            # Уведомление админу
            await bot.send_message(ADMIN_GROUP_ID, admin_text, message_thread_id=topic_id, parse_mode="HTML")
            return

        elif message.text == "🗣 Общение":
            admin_text = "🟢 <b>ЗАПРОС НА ОБЩЕНИЕ.</b>\nПользователь хочет просто поболтать.\nТег: #ОБЩЕНИЕ"
            await message.answer("Отлично! Кто-нибудь из нас скоро подключится к беседе.", reply_markup=get_main_keyboard())
            await bot.send_message(ADMIN_GROUP_ID, admin_text, message_thread_id=topic_id, parse_mode="HTML")
            return
            
        # --- ОБЫЧНАЯ ПЕРЕСЫЛКА СООБЩЕНИЯ ---
        try:
            await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
            
            # Визуальное подтверждение (исчезает через 2 сек)
            confirm = await message.answer("✅ Отправлено")
            await asyncio.sleep(2)
            await confirm.delete()
            
        except TelegramBadRequest:
            # Если контент защищен, шлем текст
            text_content = message.text or message.caption or "[Медиа]"
            await bot.send_message(
                ADMIN_GROUP_ID,
                f"⚠️ <b>(Защищенный контент)</b>:\n{text_content}",
                message_thread_id=topic_id,
                parse_mode="HTML"
            )
            await message.answer("✅ Отправлено (текстом)")

    except Exception as e:
        logger.error(f"Ошибка в forward_to_admin: {e}")

# --- АДМИН КОМАНДЫ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def info_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply("❌ Команда работает только внутри топика.")
        
    user = get_user(topic_id=topic_id)
    if not user: return await message.reply("❌ Пользователь не найден.")
    
    await message.reply(
        f"👤 <b>Инфо о пользователе:</b>\n"
        f"ID: <code>{user.get('user_id')}</code>\n"
        f"Имя: {user.get('full_name')}\n"
        f"Username: @{user.get('username')}\n"
        f"Варны: {user.get('warns', 0)}/3\n"
        f"Бан: {'ДА' if user.get('is_banned') else 'НЕТ'}",
        parse_mode="HTML"
    )

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def ban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    
    topic_id = message.message_thread_id
    user = get_user(topic_id=topic_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=True)
        await message.reply("🚫 Пользователь забанен.")
        try: await bot.send_message(user['user_id'], "🚫 Ваш доступ к боту ограничен.")
        except: pass
    else:
        await message.reply("❌ Не удалось найти пользователя.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def unban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    
    topic_id = message.message_thread_id
    user = get_user(topic_id=topic_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=False, warns=0)
        await message.reply("✅ Пользователь разбанен, варны сброшены.")
    else:
        await message.reply("❌ Не удалось найти пользователя.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def warn_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    
    topic_id = message.message_thread_id
    user = get_user(topic_id=topic_id)
    if user:
        new_w = (user.get('warns', 0) or 0) + 1
        ban_it = new_w >= 3
        
        update_user_sanctions(user['user_id'], warns=new_w, is_banned=ban_it)
        
        msg = f"⚠️ Выдано предупреждение {new_w}/3"
        if ban_it: msg += " (БАН)"
        await message.reply(msg)
        
        try: 
            user_msg = "⚠️ Вы получили предупреждение."
            if ban_it: user_msg = "🚫 Превышен лимит предупреждений. Вы забанены."
            await bot.send_message(user['user_id'], user_msg)
        except: pass
    else:
        await message.reply("❌ Не удалось найти пользователя.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    try:
        all_res = supabase.table("users").select("*", count="exact").execute()
        ban_res = supabase.table("users").select("*", count="exact").eq("is_banned", True).execute()
        
        all_c = all_res.count or len(all_res.data)
        ban_c = ban_res.count or len(ban_res.data)
        
        await message.reply(
            f"📊 <b>Статистика:</b>\n"
            f"Всего юзеров: {all_c}\n"
            f"В бане: {ban_c}\n"
            f"Активных: {all_c - ban_c}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"Ошибка получения статистики: {e}")

# --- РАССЫЛКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def start_bc(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.reply("📢 Отправьте сообщение (текст/фото), которое нужно разослать всем:")
    await state.set_state(BotStates.broadcast)

@dp.message(BotStates.broadcast)
async def do_bc(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Берем всех юзеров
    users_res = supabase.table("users").select("user_id").execute()
    users = users_res.data if hasattr(users_res, 'data') else []
    
    if not users: 
        return await message.reply("Нет пользователей в базе.")
    
    await message.reply(f"🚀 Начинаю рассылку на {len(users)} чел...")
    ok, err, blocked = 0, 0, 0
    
    for i, u in enumerate(users):
        uid = u['user_id']
        try:
            await message.copy_to(uid)
            ok += 1
            # Небольшая задержка каждые 20 сообщений
            if i % 20 == 0: await asyncio.sleep(0.5)
            else: await asyncio.sleep(0.05)
            
        except TelegramForbiddenError:
            blocked += 1
            # Помечаем мертвых душ
            supabase.table("users").update({"is_banned": True}).eq("user_id", uid).execute()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try: await message.copy_to(uid); ok += 1
            except: err += 1
        except Exception as e:
            logger.error(f"Ошибка рассылки {uid}: {e}")
            err += 1
            
    await message.reply(
        f"🏁 <b>Рассылка завершена!</b>\n"
        f"✅ Доставлено: {ok}\n"
        f"💀 Заблокировали бота: {blocked}\n"
        f"❌ Ошибок: {err}",
        parse_mode="HTML"
    )

# --- ОТВЕТ АДМИНА ЮЗЕРУ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def admin_to_user(message: types.Message):
    """Админ пишет в топик -> сообщение летит юзеру"""
    try:
        # Игнорируем сообщения самого бота и команды
        if message.from_user.id == bot.id: return
        if message.text and message.text.startswith("/"): return
        
        topic_id = message.message_thread_id
        if not topic_id: return
        
        user = get_user(topic_id=topic_id)
        if not user or not user.get('user_id'): return
        
        if user.get('is_banned'):
            return await message.reply("❌ Этот пользователь забанен.")
            
        try:
            await message.copy_to(user['user_id'])
        except TelegramForbiddenError:
             await message.reply("❌ Пользователь заблокировал бота.")
        except Exception as e:
            await message.reply(f"❌ Не доставлено: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка admin_to_user: {e}")

# --- ЗАПУСК ---

async def main():
    logger.info("===============================")
    logger.info(f"Бот запускается... Owner: {OWNER_ID}")
    logger.info("===============================")
    
    # Удаляем вебхук и старые апдейты, чтобы не было спама при старте
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
