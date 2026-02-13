import os
import uuid
import asyncio
import logging
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ContentType
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

# ══════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════
TOKEN    = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BOT_USER = os.environ["BOT_USERNAME"]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_KEY"]
WH_PATH  = f"/wh/{TOKEN}"
PORT     = int(os.environ.get("PORT", 10000))

FILES_TABLE = f"{SUPA_URL}/rest/v1/files"
USERS_TABLE = f"{SUPA_URL}/rest/v1/users"

http: ClientSession = None


# ══════════════════════════════════════════════
#  СОСТОЯНИЯ (FSM)
# ══════════════════════════════════════════════
class BroadcastState(StatesGroup):
    waiting_message = State()


# ══════════════════════════════════════════════
#  БАЗА ДАННЫХ — файлы
# ══════════════════════════════════════════════
async def db_get(code: str):
    async with http.get(
        f"{FILES_TABLE}?code=eq.{code}&select=*"
    ) as r:
        data = await r.json()
        return data[0] if data else None


async def db_save(code: str, entry: dict):
    row = {"code": code}
    row.update(entry)
    async with http.post(
        FILES_TABLE,
        json=row,
        headers={"Prefer": "return=minimal"}
    ) as r:
        if r.status >= 400:
            text = await r.text()
            logging.error(f"DB save error: {r.status} {text}")


async def db_delete(code: str):
    async with http.delete(
        f"{FILES_TABLE}?code=eq.{code}"
    ) as r:
        pass


async def db_all():
    async with http.get(
        f"{FILES_TABLE}?select=*&order=created_at.desc"
    ) as r:
        return await r.json()


async def db_increment(code: str, current: int):
    async with http.patch(
        f"{FILES_TABLE}?code=eq.{code}",
        json={"downloads": current + 1}
    ) as r:
        pass


# ══════════════════════════════════════════════
#  БАЗА ДАННЫХ — пользователи
# ══════════════════════════════════════════════
async def save_user(user: types.User):
    """Сохраняет пользователя. Если уже есть — обновляет имя."""
    async with http.post(
        USERS_TABLE,
        json={
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
        },
        headers={
            "Prefer": "return=minimal",
            "on-conflict": "user_id",
        }
    ) as r:
        # если уже существует — обновляем
        if r.status == 409:
            async with http.patch(
                f"{USERS_TABLE}?user_id=eq.{user.id}",
                json={
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                }
            ) as r2:
                pass


async def get_all_users():
    """Все user_id из базы."""
    async with http.get(
        f"{USERS_TABLE}?select=user_id"
    ) as r:
        rows = await r.json()
        return [row["user_id"] for row in rows]


async def count_users():
    async with http.get(
        f"{USERS_TABLE}?select=user_id",
        headers={"Prefer": "count=exact"}
    ) as r:
        # Supabase возвращает count в заголовке
        content_range = r.headers.get("content-range", "")
        try:
            return int(content_range.split("/")[1])
        except Exception:
            data = await r.json()
            return len(data)


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
async def cmd_start(msg: types.Message, state: FSMContext):
    # Сохраняем пользователя
    await save_user(msg.from_user)
    # Сбрасываем состояние
    await state.clear()

    args = msg.text.split(maxsplit=1)

    if len(args) > 1:
        code = args[1]
        entry = await db_get(code)
        if not entry:
            return await msg.answer("❌ Файл не найден.")

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

    if msg.from_user.id == OWNER_ID:
        users = await count_users()
        rows = await db_all()
        await msg.answer(
            f"👑 <b>Вы владелец</b>\n\n"
            f"📂 Файлов: <b>{len(rows)}</b>\n"
            f"👥 Пользователей: <b>{users}</b>\n\n"
            f"Отправьте файл → получите ссылку\n\n"
            f"<b>Команды:</b>\n"
            f"/list — все файлы\n"
            f"/del <code>код</code> — удалить\n"
            f"/stats — статистика\n"
            f"/send — рассылка всем\n"
            f"/cancel — отменить рассылку",
            parse_mode="HTML",
        )
    else:
        await msg.answer(
            "👋 Привет! Перейдите по ссылке от отправителя."
        )


# ══════════════════════════════════════════════
#  РАССЫЛКА
# ══════════════════════════════════════════════

# ────────── /send — начать рассылку ──────────
@router.message(Command("send"), F.from_user.id == OWNER_ID)
async def cmd_send(msg: types.Message, state: FSMContext):
    users = await count_users()
    await state.set_state(BroadcastState.waiting_message)
    await msg.answer(
        f"📢 <b>Режим рассылки</b>\n\n"
        f"👥 Получателей: <b>{users}</b>\n\n"
        f"Отправьте сообщение которое получат ВСЕ пользователи.\n"
        f"Можно отправить:\n"
        f"• Текст\n"
        f"• Фото с подписью\n"
        f"• Видео\n"
        f"• Документ\n"
        f"• Голосовое\n"
        f"• Что угодно!\n\n"
        f"/cancel — отменить",
        parse_mode="HTML",
    )


# ────────── /cancel — отменить рассылку ──────────
@router.message(Command("cancel"), F.from_user.id == OWNER_ID)
async def cmd_cancel(msg: types.Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await state.clear()
        await msg.answer("❌ Рассылка отменена.")
    else:
        await msg.answer("Нечего отменять.")


# ────────── Получаем сообщение для рассылки ──────────
@router.message(
    BroadcastState.waiting_message,
    F.from_user.id == OWNER_ID,
)
async def do_broadcast(msg: types.Message, state: FSMContext):
    await state.clear()

    user_ids = await get_all_users()
    total = len(user_ids)

    if total == 0:
        return await msg.answer("👥 Нет пользователей.")

    status = await msg.answer(
        f"📢 Рассылка началась...\n"
        f"👥 Получателей: {total}\n\n"
        f"⏳ Ждите..."
    )

    sent = 0
    failed = 0
    blocked = 0

    for uid in user_ids:
        try:
            await msg.copy_to(chat_id=uid)
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                blocked += 1
            else:
                failed += 1
            logging.warning(f"Broadcast to {uid}: {e}")

        # Telegram лимит: 30 сообщений/сек
        # Делаем паузу каждые 25 сообщений
        if (sent + failed + blocked) % 25 == 0:
            await asyncio.sleep(1)

        # Обновляем статус каждые 50 сообщений
        if (sent + failed + blocked) % 50 == 0:
            try:
                await status.edit_text(
                    f"📢 Рассылка...\n"
                    f"✅ {sent} · ❌ {failed} · 🚫 {blocked}\n"
                    f"Осталось: {total - sent - failed - blocked}"
                )
            except Exception:
                pass

    await status.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего: <b>{total}</b>\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
        f"❌ Ошибки: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════
#  ФАЙЛЫ (без изменений)
# ══════════════════════════════════════════════

@router.message(
    F.from_user.id == OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def save_file(msg: types.Message, state: FSMContext):
    # Проверяем что мы НЕ в режиме рассылки
    current = await state.get_state()
    if current == BroadcastState.waiting_message:
        return  # обработается в do_broadcast

    code = uuid.uuid4().hex[:8]
    entry = {"caption": msg.caption or "", "downloads": 0}

    extractors = [
        (msg.document,   "document",   lambda: (
            msg.document.file_id,
            msg.document.file_name or "file")),
        (msg.photo,      "photo",      lambda: (
            msg.photo[-1].file_id, "photo.jpg")),
        (msg.video,      "video",      lambda: (
            msg.video.file_id,
            msg.video.file_name or "video.mp4")),
        (msg.audio,      "audio",      lambda: (
            msg.audio.file_id,
            msg.audio.file_name or "audio.mp3")),
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


@router.message(
    F.from_user.id != OWNER_ID,
    F.content_type.in_(MEDIA_TYPES),
)
async def reject(msg: types.Message):
    await msg.answer("⛔ Только владелец может добавлять файлы.")


@router.message(Command("list"), F.from_user.id == OWNER_ID)
async def cmd_list(msg: types.Message):
    rows = await db_all()
    if not rows:
        return await msg.answer("📂 База пуста.")

    lines = []
    for e in rows:
        link = f"https://t.me/{BOT_USER}?start={e['code']}"
        lines.append(
            f"📁 <b>{e.get('name','?')}</b> "
            f"📥{e.get('downloads',0)}\n"
            f"   <code>{e['code']}</code>\n   {link}"
        )
    text = "\n\n".join(lines)
    for i in range(0, len(text), 4000):
        await msg.answer(
            text[i:i+4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


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


@router.message(Command("stats"), F.from_user.id == OWNER_ID)
async def cmd_stats(msg: types.Message):
    rows = await db_all()
    users = await count_users()
    total = len(rows)
    dl = sum(e.get("downloads", 0) for e in rows)
    top = sorted(rows, key=lambda x: x.get("downloads", 0),
                 reverse=True)[:5]
    t = "\n".join(
        f"  📁 {e.get('name','?')} — {e.get('downloads',0)}"
        for e in top
    )
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"📁 Файлов: <b>{total}</b>\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📥 Скачиваний: <b>{dl}</b>"
    )
    if t:
        text += f"\n\n🔝 <b>Топ-5:</b>\n{t}"
    await msg.answer(text, parse_mode="HTML")


@router.message()
async def fallback(msg: types.Message):
    if msg.from_user.id == OWNER_ID:
        await msg.answer(
            "📤 Отправьте файл.\n"
            "/list — файлы\n"
            "/send — рассылка"
        )
    else:
        await msg.answer(
            "Перейдите по ссылке от отправителя."
        )


dp.include_router(router)


# ══════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════
async def on_startup(**kwargs):
    global http
    http = ClientSession(headers={
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
    })
    await bot.set_webhook(f"{BASE_URL}{WH_PATH}")
    logging.info("Webhook set, Supabase connected")


async def on_shutdown(**kwargs):
    global http
    if http:
        await http.close()
        http = None


async def health(_r):
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
