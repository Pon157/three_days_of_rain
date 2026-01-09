import asyncio
import logging
import os
import random
import string
import sys
from datetime import datetime
from typing import Union, List, Optional

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
    """Получить пользователя из БД"""
    try:
        if user_id:
            res = supabase.table("users").select("*").eq("user_id", user_id).execute()
        elif topic_id:
            res = supabase.table("users").select("*").eq("topic_id", topic_id).execute()
        else:
            return None
        
        # Проверяем наличие данных
        if hasattr(res, 'data') and res.data:
            return res.data[0]
        return None
    except Exception as e:
        logger.error(f"Ошибка БД (get_user): {e}")
        return None

def sync_user_data(user: types.User, topic_id: int = None, topic_name: str = None):
    """Синхронизировать данные пользователя с БД"""
    try:
        # Сначала проверяем существующего пользователя
        existing_user = get_user(user_id=user.id)
        
        data = {
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "last_seen": datetime.now().isoformat()
        }
        
        # Если переданы данные топика, обновляем их
        if topic_id:
            data["topic_id"] = topic_id
            data["topic_name"] = topic_name
        # Иначе сохраняем существующий topic_id, если он есть
        elif existing_user and existing_user.get("topic_id"):
            data["topic_id"] = existing_user.get("topic_id")
            data["topic_name"] = existing_user.get("topic_name")
        
        # Используем upsert для создания/обновления
        supabase.table("users").upsert(data).execute()
        logger.info(f"Синхронизирован пользователь {user.id}")
        
    except Exception as e:
        logger.error(f"Ошибка БД (sync_user_data): {e}")

def update_user_sanctions(user_id: int, **kwargs):
    """Обновить санкции пользователя"""
    try:
        kwargs["last_sanction_date"] = datetime.now().isoformat()
        supabase.table("users").update(kwargs).eq("user_id", user_id).execute()
        logger.info(f"Обновлены санкции для {user_id}: {kwargs}")
    except Exception as e:
        logger.error(f"Ошибка БД (update_user_sanctions): {e}")

# --- ОБРАБОТКА ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(F.chat.type == "private", CommandStart())
async def start_handler(message: types.Message):
    """Стартовое сообщение с твоим текстом"""
    try:
        db_user = get_user(user_id=message.from_user.id)
        if db_user and db_user.get("is_banned"):
            await message.answer("🚫 Ваш доступ ограничен.")
            return

        # Сначала синхронизируем пользователя без topic_id
        sync_user_data(message.from_user)
        
        # Если у пользователя уже есть topic_id, отправляем в существующую тему
        if db_user and db_user.get("topic_id"):
            topic_id = db_user.get("topic_id")
            await bot.send_message(
                ADMIN_GROUP_ID,
                f"🔄 <b>Пользователь вернулся:</b> {message.from_user.full_name}",
                message_thread_id=topic_id,
                parse_mode="HTML"
            )

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
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)
            
    except Exception as e:
        logger.error(f"Ошибка в start_handler: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

@dp.message(F.chat.type == "private")
async def forward_to_admin(message: types.Message):
    """Пересылка сообщений от пользователя к админам"""
    try:
        # Игнорируем самого бота и сервисные сообщения
        if (message.from_user.id == bot.id or 
            message.content_type in ["new_chat_members", "left_chat_member"]):
            return

        # Получаем данные пользователя
        db_user = get_user(user_id=message.from_user.id)
        
        # Если пользователя нет в БД, создаем запись
        if not db_user:
            logger.info(f"Новый пользователь: {message.from_user.id}")
            sync_user_data(message.from_user)
            db_user = get_user(user_id=message.from_user.id)
        
        # Проверяем бан
        if db_user and db_user.get("is_banned"):
            return

        # Проверяем наличие топика
        topic_id = db_user.get("topic_id") if db_user else None
        
        logger.info(f"Пользователь {message.from_user.id}, topic_id в БД: {topic_id}")

        # Если топика нет - создаем новый
        if not topic_id:
            try:
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
                t_name = f"Anon {code}"
                new_topic = await bot.create_forum_topic(ADMIN_GROUP_ID, t_name)
                topic_id = new_topic.message_thread_id
                
                # Обновляем пользователя с topic_id
                sync_user_data(message.from_user, topic_id, t_name)
                
                logger.info(f"Создан новый топик {topic_id} для пользователя {message.from_user.id}")
                
                await bot.send_message(
                    ADMIN_GROUP_ID, 
                    f"🆕 <b>Новый пользователь:</b> {message.from_user.full_name}\n"
                    f"👤 ID: <code>{message.from_user.id}</code>",
                    message_thread_id=topic_id,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка создания топика: {e}")
                await message.answer("Ошибка связи. Попробуйте позже.")
                return
        
        # Пересылаем сообщение
        try:
            await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
            # Подтверждение (удаляется через 2 сек)
            confirm = await message.answer("✅ Отправлено")
            await asyncio.sleep(2)
            await confirm.delete()
        except TelegramBadRequest as e:
            logger.error(f"Ошибка копирования: {e}")
            # Если копия невозможна (защищенный контент), шлем текстом
            text = message.text or message.caption or '[Медиа без текста]'
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"⚠️ (Защищено) Сообщение от {message.from_user.full_name}:\n{text}", 
                message_thread_id=topic_id
            )
        except Exception as e:
            logger.error(f"Ошибка при пересылке: {e}")
            await message.answer("Ошибка при отправке.")
            
    except Exception as e:
        logger.error(f"Общая ошибка в forward_to_admin: {e}")

# --- АДМИН КОМАНДЫ ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("info"))
async def info_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return
    
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply("❌ Используйте команду внутри темы с пользователем.")
    
    user = get_user(topic_id=topic_id)
    if not user: 
        return await message.reply("❌ Пользователь не найден.")

    await message.reply(
        f"👤 <b>Инфо:</b>\n"
        f"ID: <code>{user.get('user_id', 'N/A')}</code>\n"
        f"Имя: {user.get('full_name', 'N/A')}\n"
        f"Username: @{user.get('username', 'нет')}\n"
        f"Варны: {user.get('warns', 0)}/3\n"
        f"Бан: {'✅ Да' if user.get('is_banned') else '❌ Нет'}\n"
        f"Последняя активность: {user.get('last_seen', 'неизвестно')}",
        parse_mode="HTML"
    )

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def ban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return
    
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply("❌ Используйте команду внутри темы с пользователем.")
    
    user = get_user(topic_id=topic_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=True)
        await message.reply("🚫 Пользователь забанен.")
        try: 
            await bot.send_message(user['user_id'], "🚫 Ваш доступ к боту ограничен.")
        except: 
            pass
    else:
        await message.reply("❌ Пользователь не найден.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def unban_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return
    
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply("❌ Используйте команду внутри темы с пользователем.")
    
    user = get_user(topic_id=topic_id)
    if user:
        update_user_sanctions(user['user_id'], is_banned=False, warns=0)
        await message.reply("✅ Пользователь разбанен.")
    else:
        await message.reply("❌ Пользователь не найден.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def warn_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return
    
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply("❌ Используйте команду внутри темы с пользователем.")
    
    user = get_user(topic_id=topic_id)
    if user:
        current_warns = user.get('warns', 0) or 0
        new_w = current_warns + 1
        ban_it = new_w >= 3
        
        update_user_sanctions(user['user_id'], warns=new_w, is_banned=ban_it)
        
        status = f"⚠️ Варн {new_w}/3"
        if ban_it:
            status += " (Автоматический бан)"
            
        await message.reply(status)
        
        try: 
            if ban_it:
                await bot.send_message(user['user_id'], "🚫 Вы получили максимальное количество предупреждений. Доступ ограничен.")
            else:
                await bot.send_message(user['user_id'], f"⚠️ Вы получили предупреждение {new_w}/3")
        except: 
            pass
    else:
        await message.reply("❌ Пользователь не найден.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def stats_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID: 
        return
    
    try:
        # Получаем общее количество пользователей
        all_users_res = supabase.table("users").select("user_id", count="exact").execute()
        # Получаем количество забаненных
        banned_res = supabase.table("users").select("user_id", count="exact").eq("is_banned", True).execute()
        
        # Извлекаем count из ответа Supabase
        all_count = all_users_res.count if hasattr(all_users_res, 'count') else len(all_users_res.data or [])
        banned_count = banned_res.count if hasattr(banned_res, 'count') else len(banned_res.data or [])
        
        await message.reply(
            f"📊 <b>Статистика бота:</b>\n"
            f"👤 Всего пользователей: {all_count}\n"
            f"🚫 Забанено: {banned_count}\n"
            f"✅ Активных: {all_count - banned_count}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await message.reply(f"❌ Ошибка получения статистики: {e}")

# --- РАССЫЛКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def start_bc(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: 
        return
    await message.reply("📢 Пришлите сообщение для рассылки:")
    await state.set_state(BotStates.broadcast)

@dp.message(BotStates.broadcast)
async def do_bc(message: types.Message, state: FSMContext):
    await state.clear()
    
    try:
        users_res = supabase.table("users").select("user_id").execute()
        users = users_res.data if hasattr(users_res, 'data') else []
        
        if not users:
            return await message.reply("❌ Нет пользователей для рассылки.")
        
        await message.reply(f"📢 Начинаю рассылку на {len(users)} пользователей...")
        
        ok, err = 0, 0
        for idx, u in enumerate(users, 1):
            try:
                await message.copy_to(u['user_id'])
                ok += 1
                
                # Делаем небольшую задержку каждые 10 сообщений
                if idx % 10 == 0:
                    await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(0.05)
                    
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await message.copy_to(u['user_id'])
                    ok += 1
                except:
                    err += 1
            except Exception as e:
                logger.error(f"Ошибка отправки пользователю {u['user_id']}: {e}")
                err += 1
        
        await message.reply(f"🏁 Рассылка завершена!\n✅ Успешно: {ok}\n❌ Ошибок: {err}")
        
    except Exception as e:
        logger.error(f"Ошибка в рассылке: {e}")
        await message.reply(f"❌ Ошибка рассылки: {e}")

# --- ОТВЕТ АДМИНА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.is_topic_message)
async def admin_to_user(message: types.Message):
    try:
        # Игнорируем сообщения от бота
        if message.from_user.id == bot.id:
            return
        
        # Игнорируем команды
        if message.text and message.text.startswith("/"):
            return
        
        # Проверяем, что это ответ в теме
        topic_id = message.message_thread_id
        if not topic_id:
            return
        
        # Получаем пользователя по topic_id
        user = get_user(topic_id=topic_id)
        if not user:
            logger.warning(f"Пользователь не найден для темы {topic_id}")
            return
        
        user_id = user.get('user_id')
        if not user_id:
            return
        
        # Проверяем не забанен ли пользователь
        if user.get('is_banned'):
            await message.reply("❌ Пользователь забанен.")
            return
        
        # Отправляем сообщение пользователю
        try:
            await message.copy_to(user_id)
            logger.info(f"Сообщение отправлено пользователю {user_id}")
        except TelegramBadRequest as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            await message.reply("❌ Не удалось отправить. Возможно, пользователь заблокировал бота.")
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
            await message.reply(f"❌ Ошибка отправки: {e}")
            
    except Exception as e:
        logger.error(f"Общая ошибка в admin_to_user: {e}")

# --- ЗАПУСК ---

async def main():
    try:
        logger.info("=" * 50)
        logger.info(f"Запуск бота... Owner ID: {OWNER_ID}")
        logger.info(f"Admin Group ID: {ADMIN_GROUP_ID}")
        logger.info("=" * 50)
        
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
