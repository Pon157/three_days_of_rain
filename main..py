import asyncio
import logging
import os
import aiosqlite
import random
import string
import datetime
import sys
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))

# Парсинг владельцев
raw_owner_ids = os.getenv("OWNER_ID", "")
OWNER_IDS = [int(oid.strip()) for oid in raw_owner_ids.split(",") if oid.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_NAME = "anon_chat.db"
START_TIME = datetime.datetime.now()

# --- СОСТОЯНИЯ ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

class AdminManageState(StatesGroup):
    waiting_for_id = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ (Расширенная) ---
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
                reg_date TEXT,
                last_active TEXT
            )
        """)
        # Таблица администраторов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_date TEXT
            )
        """)
        await db.commit()
        logger.info("База данных инициализирована.")

async def is_admin(uid):
    if uid in OWNER_IDS:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM admins WHERE user_id = ?", (uid,)) as c:
            res = await c.fetchone()
            return res is not None

async def add_admin_db(uid, added_by=0):
    async with aiosqlite.connect(DB_NAME) as db:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)", 
                         (uid, added_by, now))
        await db.commit()

async def del_admin_db(uid):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (uid,))
        await db.commit()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_uptime():
    delta = datetime.datetime.now() - START_TIME
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}д {hours}ч {minutes}м"

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (uid,)) as c:
            user = await c.fetchone()
            if user and user[0]:
                return await message.answer("⛔ Вы заблокированы в этой системе.")

    photo_url = "https://cdn-icons-png.flaticon.com/512/9703/9703596.png"
    text = (
        "<b>🛡 Анонимная Служба Поддержки</b>\n\n"
        "Ваши сообщения будут переданы операторам без указания вашего имени или ссылки на профиль.\n\n"
        "<i>Просто напишите что угодно ниже...</i>"
    )
    
    try:
        sent = await message.answer_photo(photo=photo_url, caption=text, parse_mode="HTML")
        await bot.pin_chat_message(message.chat.id, sent.message_id)
    except Exception as e:
        logger.error(f"Ошибка при старте: {e}")
        await message.answer(text, parse_mode="HTML")

@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    uid = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (uid,)) as c:
            user = await c.fetchone()
    
    if user and user[4]: # is_banned
        return

    # Если новый пользователь
    if not user:
        anon_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        topic_name = f"User {anon_id}"
        
        try:
            topic = await bot.create_forum_topic(ADMIN_GROUP_ID, topic_name)
            reg_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "INSERT INTO users (user_id, topic_id, topic_name, reg_date, last_active) VALUES (?, ?, ?, ?, ?)",
                    (uid, topic.message_thread_id, topic_name, reg_date, reg_date)
                )
                await db.commit()
            
            tid = topic.message_thread_id
            await bot.send_message(
                ADMIN_GROUP_ID, 
                f"🆕 <b>Новое обращение</b>\nИмя: {topic_name}\nID: <code>{uid}</code>", 
                message_thread_id=tid,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка создания топика: {e}")
            return await message.answer("⚠️ Ошибка на стороне сервера. Попробуйте позже.")
    else:
        tid = user[1]
        # Обновляем активность
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET last_active = ? WHERE user_id = ?", 
                             (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), uid))
            await db.commit()

    # Пересылка
    try:
        await message.copy_to(ADMIN_GROUP_ID, message_thread_id=tid)
        
        # Уведомление с таймером
        confirm = await message.answer("✅ Сообщение доставлено")
        await asyncio.sleep(5)
        await confirm.delete()
    except Exception as e:
        logger.error(f"Ошибка пересылки: {e}")

# --- ХЕНДЛЕРЫ АДМИНИСТРАТОРА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id, ~F.text.startswith("/"))
async def admin_reply(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE topic_id = ?", (message.message_thread_id,)) as c:
            user = await c.fetchone()
            
    if not user:
        return # Это не топик пользователя

    try:
        await message.copy_to(user[0])
    except TelegramForbiddenError:
        await message.reply("❌ Не удалось отправить: пользователь заблокировал бота.")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}")

# Команды управления пользователем в топике
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def process_warn(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, warns, topic_name FROM users WHERE topic_id = ?", (message.message_thread_id,)) as c:
            user = await c.fetchone()
    
    if not user:
        return await message.reply("Эта команда работает только внутри топика пользователя.")

    new_warns = user[1] + 1
    async with aiosqlite.connect(DB_NAME) as db:
        if new_warns >= 3:
            await db.execute("UPDATE users SET warns = ?, is_banned = 1 WHERE user_id = ?", (new_warns, user[0]))
            await message.reply(f"⛔ <b>{user[2]} получил 3/3 варна и забанен.</b>", parse_mode="HTML")
            try: await bot.send_message(user[0], "⛔ Вы были забанены за нарушение правил общения (3/3 варнов).")
            except: pass
        else:
            await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (new_warns, user[0]))
            await message.reply(f"⚠️ Варн выдан пользователю {user[2]} ({new_warns}/3)")
            try: await bot.send_message(user[0], f"⚠️ Администратор выдал вам предупреждение ({new_warns}/3). Будьте вежливы.")
            except: pass
        await db.commit()

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unwarn"))
async def process_unwarn(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, warns FROM users WHERE topic_id = ?", (message.message_thread_id,)) as c:
            user = await c.fetchone()
    
    if user:
        new_v = max(0, user[1] - 1)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (new_v, user[0]))
            await db.commit()
        await message.reply(f"✅ Один варн снят. Текущий счет: {new_v}/3")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def process_ban(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, topic_name FROM users WHERE topic_id = ?", (message.message_thread_id,)) as c:
            user = await c.fetchone()
    
    if user:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user[0],))
            await db.commit()
        await message.reply(f"🚫 Пользователь {user[1]} успешно забанен.")
        try: await bot.send_message(user[0], "🚫 Ваш доступ к боту ограничен администратором.")
        except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def process_unban(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, topic_name FROM users WHERE topic_id = ?", (message.message_thread_id,)) as c:
            user = await c.fetchone()
    
    if user:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET is_banned = 0, warns = 0 WHERE user_id = ?", (user[0],))
            await db.commit()
        await message.reply(f"✅ Пользователь {user[1]} разбанен, история варнов очищена.")
        try: await bot.send_message(user[0], "✅ Администратор восстановил ваш доступ к боту.")
        except: pass

# --- УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ (Только OWNER) ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("add_admin"))
async def admin_add(message: types.Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        return await message.reply("❌ Эта команда доступна только главным владельцам.")
    
    if not command.args:
        return await message.reply("Укажите ID: `/add_admin 12345`")
    
    try:
        target_id = int(command.args)
        await add_admin_db(target_id, message.from_user.id)
        await message.reply(f"✅ Пользователь <code>{target_id}</code> теперь админ.", parse_mode="HTML")
    except ValueError:
        await message.reply("Ошибка: ID должен быть числом.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("del_admin"))
async def admin_del(message: types.Message, command: CommandObject):
    if message.from_user.id not in OWNER_IDS:
        return
    
    if command.args:
        await del_admin_db(int(command.args))
        await message.reply(f"🗑 Админ {command.args} удален из системы.")

# --- СТАТИСТИКА И РАССЫЛКА ---

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def show_stats(message: types.Message):
    if not await is_admin(message.from_user.id):
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned = 1") as c: banned = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM admins") as c: adm = (await c.fetchone())[0]
    
    text = (
        "<b>📊 Детальная статистика</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Всего в базе: <b>{total}</b>\n"
        f"🚫 Заблокировано: <b>{banned}</b>\n"
        f"🛡 Менеджеров: <b>{adm + len(OWNER_IDS)}</b>\n"
        f"⏳ Аптайм: <b>{get_uptime()}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await message.reply(text, parse_mode="HTML")

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def start_broadcast(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    
    await message.reply("📢 <b>Режим рассылки</b>\nОтправьте сообщение (текст, фото, видео), которое увидят все пользователи.\n\nДля отмены: `/cancel`", parse_mode="HTML")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(BroadcastState.waiting_for_message)
async def perform_broadcast(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("Рассылка отменена.")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned = 0") as c:
            targets = await c.fetchall()

    status_msg = await message.reply(f"🚀 Запуск... Целей: {len(targets)}")
    
    success, blocked, failed = 0, 0, 0
    
    for (uid,) in targets:
        try:
            await message.copy_to(uid)
            success += 1
            await asyncio.sleep(0.05) # Защита от Flood Limit
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            failed += 1
            
    await status_msg.edit_text(
        f"<b>📢 Рассылка завершена</b>\n\n"
        f"✅ Доставлено: {success}\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"❌ Ошибки: {failed}",
        parse_mode="HTML"
    )
    await state.clear()

# --- СИСТЕМНЫЕ ФУНКЦИИ ЗАПУСКА ---

async def on_startup():
    await init_db()
    # Авто-добавление владельцев в список админов при каждом запуске
    for owner_id in OWNER_IDS:
        await add_admin_db(owner_id, 0)
    logger.info(f"Владельцы системы: {OWNER_IDS}")

async def main():
    await on_startup()
    # Удаление старых обновлений перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот вышел в онлайн.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
