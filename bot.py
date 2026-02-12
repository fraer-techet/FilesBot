import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message
from aiohttp import web

# === НАСТРОЙКИ (Берем из "секретов" Render) ===
TOKEN = os.getenv("BOT_TOKEN")
# ID канала должен быть числом (например -100123456789)
try:
    CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
except:
    CHANNEL_ID = 0 # Заглушка, если забыл добавить ID
# =============================================

# Инициализация
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ЛОГИКА БОТА ---

# 1. Сохранение файла (Админ кидает файл -> Бот дает ссылку)
@dp.message(F.content_type.in_({'document', 'video', 'photo', 'audio'}))
async def save_file(message: Message):
    if not CHANNEL_ID:
        await message.answer("❌ Ошибка: Не настроен CHANNEL_ID в Render.")
        return

    try:
        # Копируем файл в канал-архив
        sent = await message.copy_to(chat_id=CHANNEL_ID)
        # ID сообщения в канале = код ссылки
        code = sent.message_id
        
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={code}"
        
        await message.reply(f"✅ **Файл сохранен!**\nВот вечная ссылка:\n`{link}`", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}\nПроверь, добавил ли ты бота в админы канала!")

# 2. Выдача файла (Юзер перешел по ссылке)
@dp.message(CommandStart(deep_link=True))
async def get_file(message: Message, command: CommandObject):
    try:
        msg_id = int(command.args)
        # Копируем из канала юзеру
        await bot.copy_message(chat_id=message.from_user.id, from_chat_id=CHANNEL_ID, message_id=msg_id)
    except Exception:
        await message.answer("❌ Файл не найден (или был удален из канала).")

# 3. Приветствие
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("👋 Привет! Я файловое облако. Работаю через ссылки.")

# --- ВЕБ-СЕРВЕР (Для Render) ---

async def handle(request):
    return web.Response(text="Bot is ALIVE and RUNNING!")

async def on_startup(app):
    # Запускаем бота в фоне, когда запускается сайт
    asyncio.create_task(dp.start_polling(bot))

def main():
    # Настройка логов
    logging.basicConfig(level=logging.INFO)
    
    # Настройка веб-сервера
    app = web.Application()
    app.router.add_get('/', handle)
    app.on_startup.append(on_startup) # Прицепляем бота к сайту
    
    # Получаем порт от Render (ЭТО ВАЖНО)
    port = int(os.environ.get("PORT", 8080))
    
    # Запуск
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()
