import os
import json
import uuid
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ContentType
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

# ══════════════════════════════════════
#  НАСТРОЙКИ (берутся из переменных среды)
# ══════════════════════════════════════
TOKEN    = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BOT_USER = os.environ["BOT_USERNAME"]          # без @
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
WH_PATH  = f"/wh/{TOKEN}"
PORT     = int(os.environ.get("PORT", 10000))
DB_FILE  = "db.json"

# ══════════════════════════════════════
#  ПРОСТОЕ ХРАНИЛИЩЕ (JSON-файл)
# ══════════════════════════════════════
def load_db() -> dict:
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(data: dict):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

db: dict = load_db()

# ══════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ БОТА
# ══════════════════════════════════════
bot    = Bot(token=TOKEN)
dp     = Dispatcher()
router = Router()

# Типы контента, которые бот принимает
MEDIA_TYPES = {
    ContentType.DOCUMENT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.AUDIO,
    ContentType.VOICE,
    ContentType.VIDEO_NOTE,
    ContentType.ANIMATION,
    ContentType.STICKER,
}

# Эти типы не поддерживают подпись (caption)
NO_CAPTION = {"video_note", "sticker"}


# ──────────────────────────────────────
#  /start  —  точка входа + deep-link
# ──────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: types.Message):
    parts = msg.text.split(maxsplit=1)

    # ── Если есть код файла (deep-link) ──
    if len(parts) > 1:
        code = parts[1]
        entry = db.get(code)

        if not entry:
            return await msg.answer(
                "❌ Файл не найден или ссылка устарела."
            )

        # Счётчик скачиваний
        entry["downloads"] = entry.get("downloads", 0) + 1
        save_db(db)

        # Отправляем файл по его типу
        # aiogram: message.answer_document, answer_photo и т.д.
        method = getattr(msg, f"answer_{entry['type']}", None)
        if not method:
            return await msg.answer("❌ Неизвестный тип файла.")

        kwargs = {}
        if entry["type"] not in NO_CAPTION and entry.get("caption"):
            kwargs["caption"] = entry["caption"]

        try:
            await method(entry["file_id"], **kwargs)
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")
            await msg.answer("❌ Не удалось отправить файл.")
        return

    # ── Обычный /start (без кода) ──
    if msg.from_user.id == OWNER_ID:
        await msg.answer(
            f"👑 <b>Вы владелец бота</b>\n\n"
            f"📂 Файлов в базе: <b>{len(db)}</b>\n\n"
            f"▸ Отправьте любой файл → получите ссылку\n\n"
            f"<b>Команды:</b>\n"
            f"/list — список файлов\n"
            f"/del <code>код</code> — удалить файл\n"
            f"/stats — статистика",
            parse_mode="HTML",
        )
    else:
        await msg.answer(
            "👋 Привет! Я бот-файлообменник.\n"
            "Перейдите по ссылке от отправителя, "
            "чтобы получить файл."
        )


# ──────────────────────────────────────
#  Владелец отправляет файл → бот сохраняет
# ──────────────────────────────────────
@router.message(
    F.from_user.id == OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def save_file(msg: types.Message):
    code = uuid.uuid4().hex[:8]

    entry = {
        "caption": msg.caption or "",
        "downloads": 0,
    }

    # Определяем тип и file_id
    extractors = [
        (msg.document,   "document",   lambda: (msg.document.file_id,
            msg.document.file_name or "file")),
        (msg.photo,      "photo",      lambda: (msg.photo[-1].file_id,
            "photo.jpg")),
        (msg.video,      "video",      lambda: (msg.video.file_id,
            msg.video.file_name or "video.mp4")),
        (msg.audio,      "audio",      lambda: (msg.audio.file_id,
            msg.audio.file_name or "audio.mp3")),
        (msg.voice,      "voice",      lambda: (msg.voice.file_id,
            "voice.ogg")),
        (msg.video_note, "video_note", lambda: (msg.video_note.file_id,
            "circle.mp4")),
        (msg.animation,  "animation",  lambda: (msg.animation.file_id,
            "animation.gif")),
        (msg.sticker,    "sticker",    lambda: (msg.sticker.file_id,
            "sticker")),
    ]

    for obj, file_type, fn in extractors:
        if obj:
            file_id, name = fn()
            entry.update(file_id=file_id, type=file_type, name=name)
            break

    db[code] = entry
    save_db(db)

    link = f"https://t.me/{BOT_USER}?start={code}"

    await msg.reply(
        f"✅ <b>Файл сохранён!</b>\n\n"
        f"📁 <b>{entry['name']}</b>\n"
        f"🔑 Код: <code>{code}</code>\n\n"
        f"🔗 Ссылка (нажмите чтобы скопировать):\n"
        f"<code>{link}</code>\n\n"
        f"Отправьте эту ссылку кому угодно!",
        parse_mode="HTML",
    )


# ──────────────────────────────────────
#  Не-владелец пытается отправить файл
# ──────────────────────────────────────
@router.message(
    F.from_user.id != OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def reject_file(msg: types.Message):
    await msg.answer("⛔ Только владелец может добавлять файлы.")


# ──────────────────────────────────────
#  /list — все файлы
# ──────────────────────────────────────
@router.message(Command("list"), F.from_user.id == OWNER_ID)
async def cmd_list(msg: types.Message):
    if not db:
        return await msg.answer("📂 База пуста.")

    lines = []
    for code, e in db.items():
        link = f"https://t.me/{BOT_USER}?start={code}"
        lines.append(
            f"📁 <b>{e.get('name', '?')}</b>  "
            f"📥 {e.get('downloads', 0)}\n"
            f"    Код: <code>{code}</code>\n"
            f"    {link}"
        )

    text = "\n\n".join(lines)

    # Telegram ограничивает 4096 символов — разбиваем
    for i in range(0, len(text), 4000):
        await msg.answer(
            text[i : i + 4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


# ──────────────────────────────────────
#  /del <код> — удалить файл
# ──────────────────────────────────────
@router.message(Command("del"), F.from_user.id == OWNER_ID)
async def cmd_del(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Формат: /del <code>код</code>",
                                parse_mode="HTML")
    code = parts[1].strip()
    if code not in db:
        return await msg.answer("❌ Файл не найден.")

    name = db[code].get("name", "?")
    del db[code]
    save_db(db)
    await msg.answer(
        f"🗑 Удалено: <b>{name}</b> (<code>{code}</code>)",
        parse_mode="HTML",
    )


# ──────────────────────────────────────
#  /stats — статистика
# ──────────────────────────────────────
@router.message(Command("stats"), F.from_user.id == OWNER_ID)
async def cmd_stats(msg: types.Message):
    total = len(db)
    downloads = sum(e.get("downloads", 0) for e in db.values())

    top = sorted(db.items(),
                 key=lambda x: x[1].get("downloads", 0),
                 reverse=True)[:5]

    top_text = ""
    for code, e in top:
        top_text += (
            f"  📁 {e.get('name','?')} — "
            f"{e.get('downloads',0)} скач.\n"
        )

    await msg.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"📁 Файлов: <b>{total}</b>\n"
        f"📥 Всего скачиваний: <b>{downloads}</b>\n\n"
        f"🔝 <b>Топ-5:</b>\n{top_text}" if top_text else
        f"📊 <b>Статистика</b>\n\n"
        f"📁 Файлов: <b>{total}</b>\n"
        f"📥 Всего скачиваний: <b>{downloads}</b>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────
#  Ловим всё остальное
# ──────────────────────────────────────
@router.message()
async def fallback(msg: types.Message):
    if msg.from_user.id == OWNER_ID:
        await msg.answer(
            "📤 Отправьте файл, чтобы получить ссылку.\n"
            "/list — список файлов"
        )
    else:
        await msg.answer(
            "Перейдите по ссылке от отправителя, "
            "чтобы получить файл."
        )


dp.include_router(router)


# ══════════════════════════════════════
#  WEBHOOK + ЗАПУСК
# ══════════════════════════════════════
async def on_startup(bot: Bot):
    url = f"{BASE_URL}{WH_PATH}"
    await bot.set_webhook(url)
    logging.info(f"✅ Webhook установлен: {url}")


async def health(_request):
    """Эндпоинт для UptimeRobot — поддерживает сервис онлайн."""
    return web.Response(text="OK")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp, bot=bot
    ).register(app, path=WH_PATH)

    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
