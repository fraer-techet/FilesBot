import os
import uuid
import logging
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ContentType
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

# ══════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════
TOKEN        = os.environ["BOT_TOKEN"]
OWNER_ID     = int(os.environ["OWNER_ID"])
BOT_USER     = os.environ["BOT_USERNAME"]
BASE_URL     = os.environ.get("RENDER_EXTERNAL_URL", "")
SUPA_URL     = os.environ["SUPABASE_URL"]
SUPA_KEY     = os.environ["SUPABASE_KEY"]
WH_PATH      = f"/wh/{TOKEN}"
PORT         = int(os.environ.get("PORT", 10000))

TABLE = f"{SUPA_URL}/rest/v1/files"

# глобальная HTTP-сессия для Supabase
http = None

# ══════════════════════════════════════════════
#  ФУНКЦИИ БАЗЫ ДАННЫХ (Supabase REST API)
# ══════════════════════════════════════════════

async def db_get(code):
    """Получить один файл по коду."""
    async with http.get(
        f"{TABLE}?code=eq.{code}&select=*"
    ) as r:
        rows = await r.json()
        return rows[0] if rows else None


async def db_save(code, entry):
    """Сохранить новый файл."""
    await http.post(
        TABLE,
        json={"code": code, **entry},
        headers={"Prefer": "return=minimal"},
    )


async def db_delete(code):
    """Удалить файл."""
    await http.delete(f"{TABLE}?code=eq.{code}")


async def db_all():
    """Все файлы (новые первые)."""
    async with http.get(
        f"{TABLE}?select=*&order=created_at.desc"
    ) as r:
        return await r.json()


async def db_increment(code, current):
    """Увеличить счётчик скачиваний."""
    await http.patch(
        f"{TABLE}?code=eq.{code}",
        json={"downloads": current + 1},
    )


# ══════════════════════════════════════════════
#  БОТ
# ══════════════════════════════════════════════
bot    = Bot(token=TOKEN)
dp     = Dispatcher()
router = Router()

MEDIA_TYPES = {
    ContentType.DOCUMENT,  ContentType.PHOTO,
    ContentType.VIDEO,     ContentType.AUDIO,
    ContentType.VOICE,     ContentType.VIDEO_NOTE,
    ContentType.ANIMATION, ContentType.STICKER,
}
NO_CAPTION = {"video_note", "sticker"}


# ────────── /start + deep-link ──────────
@router.message(CommandStart())
async def cmd_start(msg: types.Message):
    args = msg.text.split(maxsplit=1)

    # Если есть код файла → отдаём файл
    if len(args) > 1:
        code = args[1]
        entry = await db_get(code)

        if not entry:
            return await msg.answer("❌ Файл не найден или ссылка устарела.")

        await db_increment(code, entry.get("downloads", 0))

        send = getattr(msg, f"answer_{entry['type']}", None)
        if not send:
            return await msg.answer("❌ Неподдерживаемый тип.")

        kw = {}
        if entry["type"] not in NO_CAPTION and entry.get("caption"):
            kw["caption"] = entry["caption"]

        try:
            await send(entry["file_id"], **kw)
        except Exception as e:
            logging.error(f"Send error: {e}")
            await msg.answer("❌ Не удалось отправить файл.")
        return

    # Обычный /start
    if msg.from_user.id == OWNER_ID:
        rows = await db_all()
        await msg.answer(
            f"👑 <b>Вы владелец</b>\n\n"
            f"📂 Файлов в базе: <b>{len(rows)}</b>\n\n"
            f"Отправьте файл → получите ссылку\n\n"
            f"<b>Команды:</b>\n"
            f"/list — все файлы\n"
            f"/del <code>код</code> — удалить\n"
            f"/stats — статистика",
            parse_mode="HTML",
        )
    else:
        await msg.answer(
            "👋 Привет! Перейдите по ссылке от отправителя, "
            "чтобы получить файл."
        )


# ────────── Владелец отправляет файл ──────────
@router.message(
    F.from_user.id == OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def save_file(msg: types.Message):
    code = uuid.uuid4().hex[:8]
    entry = {"caption": msg.caption or "", "downloads": 0}

    extractors = [
        (msg.document,   "document",   lambda: (
            msg.document.file_id, msg.document.file_name or "file")),
        (msg.photo,      "photo",      lambda: (
            msg.photo[-1].file_id, "photo.jpg")),
        (msg.video,      "video",      lambda: (
            msg.video.file_id, msg.video.file_name or "video.mp4")),
        (msg.audio,      "audio",      lambda: (
            msg.audio.file_id, msg.audio.file_name or "audio.mp3")),
        (msg.voice,      "voice",      lambda: (
            msg.voice.file_id, "voice.ogg")),
        (msg.video_note, "video_note", lambda: (
            msg.video_note.file_id, "circle.mp4")),
        (msg.animation,  "animation",  lambda: (
            msg.animation.file_id, "animation.gif")),
        (msg.sticker,    "sticker",    lambda: (
            msg.sticker.file_id, "sticker")),
    ]

    for obj, ftype, fn in extractors:
        if obj:
            fid, name = fn()
            entry.update(file_id=fid, type=ftype, name=name)
            break

    await db_save(code, entry)

    link = f"https://t.me/{BOT_USER}?start={code}"
    await msg.reply(
        f"✅ <b>Файл сохранён!</b>\n\n"
        f"📁 <b>{entry['name']}</b>\n"
        f"🔑 Код: <code>{code}</code>\n\n"
        f"🔗 Ссылка:\n<code>{link}</code>",
        parse_mode="HTML",
    )


# ────────── Чужой пытается загрузить ──────────
@router.message(
    F.from_user.id != OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def reject(msg: types.Message):
    await msg.answer("⛔ Только владелец может добавлять файлы.")


# ────────── /list ──────────
@router.message(Command("list"), F.from_user.id == OWNER_ID)
async def cmd_list(msg: types.Message):
    rows = await db_all()
    if not rows:
        return await msg.answer("📂 База пуста.")

    lines = []
    for e in rows:
        link = f"https://t.me/{BOT_USER}?start={e['code']}"
        lines.append(
            f"📁 <b>{e.get('name','?')}</b>  "
            f"📥 {e.get('downloads',0)}\n"
            f"   <code>{e['code']}</code> · {link}"
        )
    text = "\n\n".join(lines)

    for i in range(0, len(text), 4000):
        await msg.answer(
            text[i:i + 4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


# ────────── /del ──────────
@router.message(Command("del"), F.from_user.id == OWNER_ID)
async def cmd_del(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(
            "Формат: /del <code>код</code>", parse_mode="HTML"
        )
    code = parts[1].strip()
    entry = await db_get(code)
    if not entry:
        return await msg.answer("❌ Не найдено.")
    await db_delete(code)
    await msg.answer(
        f"🗑 Удалено: <b>{entry.get('name','?')}</b>",
        parse_mode="HTML",
    )


# ────────── /stats ──────────
@router.message(Command("stats"), F.from_user.id == OWNER_ID)
async def cmd_stats(msg: types.Message):
    rows = await db_all()
    total = len(rows)
    dl = sum(e.get("downloads", 0) for e in rows)
    top = sorted(
        rows, key=lambda x: x.get("downloads", 0), reverse=True
    )[:5]
    t = "\n".join(
        f"  📁 {e.get('name','?')} — {e.get('downloads',0)}"
        for e in top
    )
    await msg.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"📁 Файлов: <b>{total}</b>\n"
        f"📥 Скачиваний: <b>{dl}</b>\n\n"
        f"🔝 <b>Топ-5:</b>\n{t}" if t else
        f"📊 Файлов: <b>{total}</b> · Скачиваний: <b>{dl}</b>",
        parse_mode="HTML",
    )


# ────────── Всё остальное ──────────
@router.message()
async def fallback(msg: types.Message):
    if msg.from_user.id == OWNER_ID:
        await msg.answer(
            "📤 Отправьте файл для сохранения.\n/list — список"
        )
    else:
        await msg.answer(
            "Перейдите по ссылке от отправителя."
        )


dp.include_router(router)


# ══════════════════════════════════════════════
#  WEBHOOK + ЗАПУСК
# ══════════════════════════════════════════════
async def on_startup(bot_obj: Bot):
    global http
    http = ClientSession(headers={
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
    })
    await bot_obj.set_webhook(f"{BASE_URL}{WH_PATH}")
    logging.info("✅ Webhook set, Supabase connected")


async def on_shutdown(bot_obj: Bot):
    if http:
        await http.close()


async def health(_request):
    return web.Response(text="OK")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

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
