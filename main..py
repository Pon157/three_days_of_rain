import asyncio
import logging
import os
import aiosqlite
import random
import string
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# --- Загрузка конфигурации ---
# Как вы и просили, берем токен из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))

# Проверка наличия токена
if not BOT_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в .env файле.")

# --- Инициализация ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_NAME = "anon_chat.db"

# --- Машина состояний для рассылки ---
class BroadcastState(StatesGroup):
    waiting_for_message = State()

# --- Работа с БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                topic_name TEXT,
                warns INTEGER DEFAULT 0,
                is_banned BOOLEAN DEFAULT 0
            )
        """)
        await db.commit()

async def get_user_by_id(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_by_topic(topic_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE topic_id = ?", (topic_id,)) as cursor:
            return await cursor.fetchone()

async def create_user(user_id, topic_id, topic_name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, topic_id, topic_name) VALUES (?, ?, ?)", 
                         (user_id, topic_id, topic_name))
        await db.commit()

async def update_ban_status(user_id, is_banned):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (is_banned, user_id))
        await db.commit()

async def update_warns(user_id, count):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET warns = ? WHERE user_id = ?", (count, user_id))
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return await cursor.fetchall()

def generate_anon_name():
    """Генерирует случайное имя типа 'Anon #A1B2'"""
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"Anon #{suffix}"

# --- Хендлеры для ПОЛЬЗОВАТЕЛЕЙ (Личные сообщения) ---

@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start(message: types.Message):
    user = await get_user_by_id(message.from_user.id)
    if user and user[4]: # user[4] is is_banned
        return # Игнорируем забаненных

    # --- БЛОК С ФОТО ---
    # Вариант А: Фото по ссылке (самый простой)
    photo_url = "https://i.imgur.com/your_image.jpg" # Замените на реальную ссылку
    
    # Вариант Б: Локальный файл (раскомментируйте, если файл лежит в папке с ботом)
    # from aiogram.types import FSInputFile
    # photo_file = FSInputFile("welcome_image.jpg") 

    caption_text = (
        "👋 **Добро пожаловать в анонимный чат!**\n\n"
        "Здесь ты можешь задать любой вопрос или отправить предложение. "
        "Твоя личность полностью скрыта от администраторов.\n\n"
        "📝 Просто напиши сообщение ниже, и тебе ответят."
    )
  try:
        # Отправляем фото с подписью
        await message.answer_photo(
            photo=photo_url, 
            caption=caption_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        # Если фото не загрузилось, отправляем просто текст, чтобы бот не "упал"
        logging.error(f"Ошибка отправки фото: {e}")
        await message.answer(caption_text, parse_mode="Markdown")

@dp.message(F.chat.type == "private")
async def user_message_handler(message: types.Message):
    user_id = message.from_user.id
    
    # Проверка базы данных
    user = await get_user_by_id(user_id)
    
    # Если забанен - игнор
    if user and user[4]: 
        return

    topic_id = None
    
    # Если пользователя нет или у него нет топика - создаем
    if not user:
        anon_name = generate_anon_name()
        try:
            topic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=anon_name)
            topic_id = topic.message_thread_id
            await create_user(user_id, topic_id, anon_name)
            
            # Отправляем уведомление админам в новый топик
            await bot.send_message(
                chat_id=ADMIN_GROUP_ID, 
                message_thread_id=topic_id, 
                text=f"🆕 <b>Новый пользователь:</b> {anon_name}\nID и username скрыты.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка создания топика: {e}")
            await message.answer("Произошла ошибка связи с оператором.")
            return
    else:
        topic_id = user[1] # user[1] is topic_id

    # ПЕРЕСЫЛКА (КОПИРОВАНИЕ) АДМИНАМ
    # copy_message скрывает отправителя, поддерживая любые форматы и медиа
    try:
        await message.copy_to(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
    except Exception as e:
        await message.answer("Ошибка отправки. Возможно, топик был удален.")
        logging.error(e)


# --- Хендлеры для АДМИНОВ (В группе) ---

# 1. Ответ админа пользователю (просто сообщение в топике)
@dp.message(F.chat.id == ADMIN_GROUP_ID, F.message_thread_id, ~F.text.startswith("/"))
async def admin_reply_handler(message: types.Message):
    topic_id = message.message_thread_id
    
    # Ищем, какому юзеру принадлежит топик
    user = await get_user_by_topic(topic_id)
    if not user:
        return # Это не топик юзера или просто чат
        
    user_id = user[0]
    
    try:
        # Копируем сообщение админа пользователю (сохраняя медиа и форматирование)
        await message.copy_to(chat_id=user_id)
    except TelegramForbiddenError:
        await message.reply("❌ Пользователь заблокировал бота.")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}")

# 2. Команды модерации (работают внутри топика)
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("ban"))
async def cmd_ban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Не найден пользователь для этого топика.")
    
    await update_ban_status(user[0], True)
    await message.reply(f"⛔️ Пользователь {user[2]} <b>забанен</b>. Бот больше не будет пересылать его сообщения.", parse_mode="HTML")
    # Опционально: уведомить юзера
    try: await bot.send_message(user[0], "⛔️ Вы были заблокированы администратором.")
    except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unban"))
async def cmd_unban(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Не найден пользователь.")
    
    await update_ban_status(user[0], False)
    await message.reply(f"✅ Пользователь {user[2]} <b>разбанен</b>.", parse_mode="HTML")
    try: await bot.send_message(user[0], "✅ Доступ восстановлен.")
    except: pass

  @dp.message(F.chat.id == ADMIN_GROUP_ID, Command("warn"))
async def cmd_warn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Не найден пользователь.")
    
    new_warns = user[3] + 1
    await update_warns(user[0], new_warns)
    
    await message.reply(f"⚠️ Предупреждение выдано. Всего варнов: {new_warns}")
    try: await bot.send_message(user[0], f"⚠️ Вам выдано предупреждение. Всего: {new_warns}")
    except: pass

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("unwarn"))
async def cmd_unwarn(message: types.Message):
    topic_id = message.message_thread_id
    user = await get_user_by_topic(topic_id)
    if not user: return await message.reply("Не найден пользователь.")
    
    new_warns = max(0, user[3] - 1)
    await update_warns(user[0], new_warns)
    await message.reply(f"✅ Предупреждение снято. Всего варнов: {new_warns}")

# 3. Статистика
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("stats"))
async def cmd_stats(message: types.Message):
    # Эта команда может быть вызвана в общем чате (General), поэтому topic_id может быть None
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT count(*) FROM users WHERE is_banned=1") as cursor:
            banned_users = (await cursor.fetchone())[0]
            
    await message.reply(
        f"📊 <b>Статистика бота:</b>\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"⛔️ Забанено: {banned_users}",
        parse_mode="HTML"
    )

# 4. Рассылка
@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    await message.reply("📢 Отправьте сообщение (текст, фото, видео, голосовое), которое нужно разослать всем пользователям.\n"
                        "Напишите /cancel для отмены.")
    await state.set_state(BroadcastState.waiting_for_message)

@dp.message(F.chat.id == ADMIN_GROUP_ID, Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    await state.clear()
    await message.reply("Действие отменено.")

@dp.message(F.chat.id == ADMIN_GROUP_ID, BroadcastState.waiting_for_message)
async def process_broadcast(message: types.Message, state: FSMContext):
    users = await get_all_users() # список кортежей [(id,), (id,), ...]
    
    status_msg = await message.reply("⏳ Рассылка началась...")
    
    success = 0
    blocked = 0
    failed = 0
    
    for user_row in users:
        user_id = user_row[0]
        try:
            # copy_to идеально подходит для рассылки любого контента
            await message.copy_to(chat_id=user_id)
            success += 1
            # Небольшая задержка, чтобы не поймать лимиты телеграма при большой базе
            await asyncio.sleep(0.05) 
        except TelegramForbiddenError:
            blocked += 1
        except Exception as e:
            failed += 1
            
    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n"
        f"✅ Успешно: {success}\n"
        f"🚫 Бот заблокирован: {blocked}\n"
        f"❌ Ошибки: {failed}",
        parse_mode="HTML"
    )
    await state.clear()

# --- Запуск ---
async def main():
    await init_db()
    print("Бот запущен...")
    # Удаляем вебхук и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if name == "main":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
      
