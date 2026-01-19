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
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            topic_id INTEGER,
            topic_name TEXT,
            warns INTEGER DEFAULT 0,
            is_banned BOOLEAN DEFAULT 0,
            reg_date TEXT,
            last_activity TEXT
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        await db.commit()

async def get_user_data(user_id=None, topic_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        if user_id:
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as c:
                return await c.fetchone()
        if topic_id:
            async with db.execute("SELECT * FROM users WHERE topic_id = ?", (topic_id,)) as c:
                return await c.fetchone()
    return None

async def register_user(user_id, username, first_name, last_name, topic_id, topic_name):
    date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT OR REPLACE INTO users 
                          (user_id, username, first_name, last_name, topic_id, topic_name, reg_date, last_activity) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
                         (user_id, username, first_name, last_name, topic_id, topic_name, date, date))
        await db.commit()

async def update_activity(user_id):
    date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET last_activity = ? WHERE user_id = ?", (date, user_id))
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
                # Отправляем предупреждение и удаляем его через 10 секунд
                conf = await bot.send_message(
                    chat_id,
                    "🔔 <i>(Оператор отправил файл, который не удалось отобразить. Возможно это ошибка произошла, когда оператор переименовывал тему, поэтому просто игнорируйте. Она удалится через 10 секунд)</i>",
                    parse_mode="HTML"
                )
                await asyncio.sleep(10)
                await conf.delete()
        except TelegramForbiddenError:
            logging.error(f"Пользователь {chat_id} заблокировал бота.")
        except Exception as e2:
            logging.error(f"FATAL: Не удалось отправить сообщение юзеру {chat_id}: {e2}")

# --- ФУНКЦИЯ ДЛЯ ФОРМАТИРОВАНИЯ ИНФОРМАЦИИ О ПОЛЬЗОВАТЕЛЕ ---
def format_user_info(user_data, from_user):
    """Форматирует полную информацию о пользователе для отображения в топике"""
    if user_data:
        # user_data: (user_id, username, first_name, last_name, topic_id, topic_name, warns, is_banned, reg_date, last_activity)
        user_id = user_data[0]
        username = user_data[1] or "Не указан"
        first_name = user_data[2] or "Не указано"
        last_name = user_data[3] or "Не указано"
        warns = user_data[6]
        is_banned = user_data[7]
        reg_date = user_data[8]
        last_activity = user_data[9]
        
        status = "🚫 Заблокирован" if is_banned else "✅ Активен"
        
        info_text = (
            f"👤 <b>ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ:</b>\n\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"👁 <b>Username:</b> @{username}\n"
            f"📛 <b>Имя:</b> {first_name}\n"
            f"📛 <b>Фамилия:</b> {last_name}\n"
            f"⚠️ <b>Варны:</b> {warns}/3\n"
            f"📊 <b>Статус:</b> {status}\n"
            f"📅 <b>Регистрация:</b> {reg_date}\n"
            f"🕒 <b>Последняя активность:</b> {last_activity}\n"
            f"🔗 <b>Ссылка:</b> <a href='tg://user?id={user_id}'>Написать в ЛС</a>"
        )
    else:
        # Если пользователь еще не в базе (первое сообщение)
        user_id = from_user.id
        username = from_user.username or "Не указан"
        first_name = from_user.first_name or "Не указано"
        last_name = from_user.last_name or "Не указано"
        
        info_text = (
            f"👤 <b>НОВЫЙ ПОЛЬЗОВАТЕЛЬ:</b>\n\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"👁 <b>Username:</b> @{username}\n"
            f"📛 <b>Имя:</b> {first_name}\n"
            f"📛 <b>Фамилия:</b> {last_name}\n"
            f"⚠️ <b>Варны:</b> 0/3\n"
            f"📊 <b>Статус:</b> ✅ Новый\n"
            f"📅 <b>Регистрация:</b> Сейчас\n"
            f"🔗 <b>Ссылка:</b> <a href='tg://user?id={user_id}'>Написать в ЛС</a>"
        )
    
    return info_text

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = await get_user_data(user_id=message.from_user.id)
    if user and user[7]:  # Бан (7-й элемент в кортеже)
        return

    photo_url = "https://img.freepik.com/premium-vector/colorful-cat-flower-picture-background_580167-156.jpg?semt=ais_hybrid&w=740"
    text = (
        "👋 <b>Привет, это бот для поддержки и общения. Напишите ваш вопрос и мы ответим в ближайшее время</b>\n\n"

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
    
    if user and user[7]:  # Игнор, если забанен
        return

    topic_id = None
    topic_name = None

    # Функция создания топика с полной информацией о пользователе
    async def create_topic():
        name = f"Anon #{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
        try:
            topic = await bot.create_forum_topic(ADMIN_GROUP_ID, name)
            
            # Получаем данные пользователя
            username = message.from_user.username or ""
            first_name = message.from_user.first_name or ""
            last_name = message.from_user.last_name or ""
            
            # Регистрируем пользователя с полной информацией
            await register_user(user_id, username, first_name, last_name, 
                               topic.message_thread_id, name)
            
            # Формируем и отправляем полную информацию о пользователе
            user_info = format_user_info(None, message.from_user)
            
            # Отправляем информационное сообщение в топик
            await bot.send_message(
                ADMIN_GROUP_ID,
                user_info,
                message_thread_id=topic.message_thread_id,
                parse_mode="HTML"
            )
            
            return topic.message_thread_id
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            return None

    if not user:
        topic_id = await create_topic()
        topic_name = f"Anon #{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
    else:
        topic_id = user[4]  # 4-й элемент в кортеже - topic_id
        topic_name = user[5]  # 5-й элемент - topic_name
        # Обновляем время последней активности
        await update_activity(user_id)

    if not topic_id:
        return await message.answer("Ошибка связи с сервером поддержки.")

    # Отправка сообщения в группу
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=topic_id)
        # Подтверждение пользователю (исчезает через 5 сек)
        conf = await message.answer("✅ Отправлено")
        await asyncio.sleep(5)
        await conf.delete()
    except TelegramBadRequest as e:
        # Если топик удален, создаем новый и пробуем снова
        if "message thread not found" in str(e).lower():
            new_tid = await create_topic()
            if new_tid:
                try:
                    await message.copy_to(ADMIN_GROUP_ID, message_thread_id=new_tid)
                except Exception as ex:
                    logging.error(f"Ошибка отправки в новый топик: {ex}")
                    await message.answer("Напишите ваш вопрос ниже")
        else:
            await message.answer("Напишите ваш вопрос ниже")
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")
        await message.answer("Напишите ваш вопрос ниже")

# --- ХЕНДЛЕРЫ АДМИНА (В ГРУППЕ) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id)
async def admin_reply(message: types.Message):
    # Если это команда (начинается с /), не обрабатываем как ответ
    if message.text and message.text.startswith("/"):
        return
    
    # Ищем пользователя по ID топика
    topic_id = message.message_thread_id
    user = await get_user_data(topic_id=topic_id)
    
    if not user:
        return  # Сообщение в топике, который не привязан к юзеру (или системный)

    # Используем надежную отправку
    await safe_reply_to_user(user[0], message)  # user[0] - user_id

# --- КОМАНДЫ АДМИНА (BAN/WARN) ---

@dp.message(Command("ban"), F.chat.id == ADMIN_GROUP_ID)
async def ban_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user:
        return await message.reply("Не найден пользователь для этого топика.")
    
    await update_ban(user[0], True)
    await message.reply(f"🚫 Пользователь забанен.")
    try:
        await bot.send_message(user[0], "🚫 Вы были заблокированы администрацией.")
    except:
        pass

@dp.message(Command("unban"), F.chat.id == ADMIN_GROUP_ID)
async def unban_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user:
        return
    
    await update_ban(user[0], False)
    await update_warns(user[0], 0)
    await message.reply(f"✅ Пользователь разбанен.")
    try:
        await bot.send_message(user[0], "✅ Ваш доступ восстановлен.")
    except:
        pass

@dp.message(Command("warn"), F.chat.id == ADMIN_GROUP_ID)
async def warn_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user:
        return
    
    new_warns = user[6] + 1  # 6-й элемент - warns
    if new_warns >= 3:
        await update_ban(user[0], True)
        await update_warns(user[0], new_warns)
        await message.reply("⛔ 3/3 варна. Пользователь забанен.")
        try:
            await bot.send_message(user[0], "⛔ Вы забанены за нарушения (3/3).")
        except:
            pass
    else:
        await update_warns(user[0], new_warns)
        await message.reply(f"⚠️ Варн выдан ({new_warns}/3).")
        try:
            await bot.send_message(user[0], f"⚠️ Предупреждение ({new_warns}/3).")
        except:
            pass

@dp.message(Command("unwarn"), F.chat.id == ADMIN_GROUP_ID)
async def unwarn_user(message: types.Message):
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user:
        return
    
    new_warns = max(0, user[6] - 1)  # 6-й элемент - warns
    await update_warns(user[0], new_warns)
    await message.reply(f"✅ Варн снят. Теперь: {new_warns}/3")

@dp.message(Command("info"), F.chat.id == ADMIN_GROUP_ID)
async def user_info(message: types.Message):
    """Показывает полную информацию о пользователе в текущем топике"""
    user = await get_user_data(topic_id=message.message_thread_id)
    if not user:
        return await message.reply("❌ Не найден пользователь для этого топика.")
    
    info_text = format_user_info(user, None)
    await message.reply(info_text, parse_mode="HTML")

# --- УПРАВЛЕНИЕ АДМИНАМИ И СТАТИСТИКА (Только Владельцы) ---

@dp.message(Command("add_admin"), F.chat.id == ADMIN_GROUP_ID)
async def add_admin(message: types.Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        return await message.reply("❌ Нет прав.")
    if not command.args:
        return await message.reply("Укажите ID.")
    try:
        uid = int(command.args)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
            await db.commit()
        await message.reply(f"✅ Админ {uid} добавлен.")
    except:
        await message.reply("Ошибка ID.")

@dp.message(Command("stats"), F.chat.id == ADMIN_GROUP_ID)
async def stats(message: types.Message):
    if message.from_user.id not in OWNER_IDS:
        return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned=1") as c:
            banned = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE DATE(reg_date) = DATE('now')") as c:
            today = (await c.fetchone())[0]
    
    await message.reply(
        f"📊 <b>Статистика:</b>\n"
        f"👥 Всего пользователей: {total}\n"
        f"🚫 В бане: {banned}\n"
        f"🆕 Сегодня зарегистрировано: {today}",
        parse_mode="HTML"
    )

@dp.message(Command("broadcast"), F.chat.id == ADMIN_GROUP_ID)
async def broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    await message.reply("📢 Отправьте сообщение для рассылки.")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def do_broadcast(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as c:
            users = await c.fetchall()
    
    await message.reply(f"🚀 Рассылка на {len(users)} чел...")
    count = 0
    for u in users:
        try:
            await message.copy_to(u[0])
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.reply(f"✅ Рассылка завершена. Дошло: {count}")
    await state.clear()

async def main():
    await init_db()
    print("Бот запущен. Владельцы:", OWNER_IDS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
