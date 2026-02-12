import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message
from aiohttp import web

# === НАСТРОЙКИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ (ЧТОБЫ БЫЛО БЕЗОПАСНО) ===
TOKEN = os.getenv("8509662585:AAErQX0z1mvVj20npoqfFtuKRnzShBlUq0U") 
CHANNEL_ID = os.getenv("-1003603094158") 
# =============================================================

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ЛОГИКА: СОХРАНЕНИЕ ФАЙЛА (Только в канал) ---
@dp.message(F.content_type.in_({'document', 'video', 'photo', 'audio'}))
async def save_file_to_channel(message: Message):
    # Проверка, что пишет админ (можно убрать, если канал приватный)
    # Но так как мы просто пересылаем, бот сам по себе защита
    
    try:
        # 1. Копируем файл в канал-хранилище
        sent_msg = await message.copy_to(chat_id=CHANNEL_ID)
        
        # 2. Получаем ID сообщения в канале (это и есть наш КОД)
        code = sent_msg.message_id
        
        # 3. Генерируем ссылку
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={code}"
        
        await message.reply(f"✅ Файл сохранен в базе!\nВот вечная ссылка:\n`{link}`", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"Ошибка доступа к каналу: {e}")

# --- ЛОГИКА: ВЫДАЧА ФАЙЛА ---
@dp.message(CommandStart(deep_link=True))
async def get_file_from_channel(message: Message, command: CommandObject):
    try:
        msg_id = int(command.args) # Аргумент ссылки - это номер сообщения
        
        # Копируем сообщение из канала пользователю
        await bot.copy_message(chat_id=message.from_user.id, from_chat_id=CHANNEL_ID, message_id=msg_id)
        
    except ValueError:
        await message.answer("❌ Ссылка повреждена.")
    except Exception:
        await message.answer("❌ Файл не найден (возможно, удален из канала).")

@dp.message(CommandStart())
async def welcome(message: Message):
    await message.answer("👋 Привет! Я файловое облако. Отправь мне файл, и я его сохраню.")

# --- ВЕБ-СЕРВЕР (ЧТОБЫ RENDER НЕ УСНУЛ И ДАЛ НАМ ПОРТ) ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render выдает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    # Запускаем веб-сервер и бота параллельно
    await asyncio.gather(start_web_server(), dp.start_polling(bot))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
