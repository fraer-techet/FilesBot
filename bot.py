import os
import uuid
import asyncio
import logging
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ContentType, ChatMemberStatus
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

CHANNEL_ID   = os.environ.get("CHANNEL_ID", "")
CHANNEL_LINK = "https://t.me/venoloadertgk"

sub_required = True if CHANNEL_ID else False
notify_uploads = True

FILES_TABLE  = f"{SUPA_URL}/rest/v1/files"
USERS_TABLE  = f"{SUPA_URL}/rest/v1/users"
ADMINS_TABLE = f"{SUPA_URL}/rest/v1/admins"

http: ClientSession = None

# ══════════════════════════════════════════════
#  РОЛИ
# ══════════════════════════════════════════════
ROLES = {
    4: "👑 Владелец",
    3: "🔴 Старший админ",
    2: "🟡 Средний админ",
    1: "🟢 Младший админ",
    0: "👤 Пользователь",
}

ROLE_COMMANDS = {
    "senior": 3,
    "middle": 2,
    "junior": 1,
}


# ══════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════
def sub_keyboard(file_code: str = "") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data=f"checksub:{file_code}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ══════════════════════════════════════════════
#  ПРОВЕРКА ПОДПИСКИ
# ══════════════════════════════════════════════
async def is_subscribed(user_id: int) -> bool:
    if not sub_required or not CHANNEL_ID:
        return True
    role = await get_role(user_id)
    if role >= 1:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logging.error(f"Sub check error: {e}")
        return True


# ══════════════════════════════════════════════
#  СОСТОЯНИЯ
# ══════════════════════════════════════════════
class BroadcastState(StatesGroup):
    waiting_message = State()


# ══════════════════════════════════════════════
#  БАЗА ДАННЫХ — админы
# ══════════════════════════════════════════════
async def get_role(user_id: int) -> int:
    if user_id == OWNER_ID:
        return 4
    async with http.get(
        f"{ADMINS_TABLE}?user_id=eq.{user_id}&select=*"
    ) as r:
        data = await r.json()
        if data:
            return data[0].get("role", 0)
        return 0


async def get_admin_info(user_id: int) -> dict | None:
    if user_id == OWNER_ID:
        return {"user_id": OWNER_ID, "role": 4, "username": "owner"}
    async with http.get(
        f"{ADMINS_TABLE}?user_id=eq.{user_id}&select=*"
    ) as r:
        data = await r.json()
        return data[0] if data else None


async def set_admin(user_id: int, role: int, username: str):
    existing = await get_admin_info(user_id)
    if existing and user_id != OWNER_ID:
        async with http.patch(
            f"{ADMINS_TABLE}?user_id=eq.{user_id}",
            json={"role": role, "username": username}
        ) as r:
            pass
    else:
        async with http.post(
            ADMINS_TABLE,
            json={"user_id": user_id, "role": role, "username": username},
            headers={"Prefer": "return=minimal"}
        ) as r:
            pass


async def remove_admin(user_id: int):
    async with http.delete(
        f"{ADMINS_TABLE}?user_id=eq.{user_id}"
    ) as r:
        pass


async def get_all_admins():
    async with http.get(
        f"{ADMINS_TABLE}?select=*&order=role.desc"
    ) as r:
        return await r.json()


# ══════════════════════════════════════════════
#  БАЗА ДАННЫХ — файлы
# ══════════════════════════════════════════════
async def db_get(code: str):
    async with http.get(f"{FILES_TABLE}?code=eq.{code}&select=*") as r:
        data = await r.json()
        return data[0] if data else None


async def db_save(code: str, entry: dict):
    row = {"code": code}
    row.update(entry)
    async with http.post(FILES_TABLE, json=row, headers={"Prefer": "return=minimal"}) as r:
        if r.status >= 400:
            text = await r.text()
            logging.error(f"DB save: {r.status} {text}")


async def db_delete(code: str):
    async with http.delete(f"{FILES_TABLE}?code=eq.{code}") as r:
        pass


async def db_all():
    async with http.get(f"{FILES_TABLE}?select=*&order=created_at.desc") as r:
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
    async with http.post(
        USERS_TABLE,
        json={"user_id": user.id, "username": user.username or "", "first_name": user.first_name or ""},
        headers={"Prefer": "return=minimal", "on-conflict": "user_id"}
    ) as r:
        if r.status == 409:
            async with http.patch(
                f"{USERS_TABLE}?user_id=eq.{user.id}",
                json={"username": user.username or "", "first_name": user.first_name or ""}
            ) as r2:
                pass


async def get_all_users():
    async with http.get(f"{USERS_TABLE}?select=user_id") as r:
        rows = await r.json()
        return [row["user_id"] for row in rows]


async def count_users():
    async with http.get(
        f"{USERS_TABLE}?select=user_id",
        headers={"Prefer": "count=exact"}
    ) as r:
        cr = r.headers.get("content-range", "")
        try:
            return int(cr.split("/")[1])
        except Exception:
            data = await r.json()
            return len(data)


# ══════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ══════════════════════════════════════════════
NO_CAPTION = {"video_note", "sticker"}


async def send_file(target, entry: dict):
    send_method = getattr(target, f"answer_{entry['type']}", None)
    if not send_method:
        return await target.answer("❌ Неподдерживаемый тип.")
    kw = {}
    if entry["type"] not in NO_CAPTION and entry.get("caption"):
        kw["caption"] = entry["caption"]
    await send_method(entry["file_id"], **kw)


def get_username_display(user: types.User) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


def can_manage(manager_role: int, target_role: int) -> bool:
    return manager_role > target_role


def can_delete_file(deleter_role: int, deleter_id: int, file_entry: dict) -> bool:
    if deleter_role >= 4:
        return True
    file_owner = file_entry.get("uploaded_by", 0)
    if deleter_id == file_owner:
        return True
    file_owner_role = file_entry.get("uploader_role", 0)
    if deleter_role > file_owner_role:
        return True
    return False


# ══════════════════════════════════════════════
#  БОТ
# ══════════════════════════════════════════════
bot    = Bot(token=TOKEN)
dp     = Dispatcher()
router = Router()

MEDIA_TYPES = {
    ContentType.DOCUMENT, ContentType.PHOTO,
    ContentType.VIDEO, ContentType.AUDIO,
    ContentType.VOICE, ContentType.VIDEO_NOTE,
    ContentType.ANIMATION, ContentType.STICKER,
}


# ────────── /start ──────────
@router.message(CommandStart())
async def cmd_start(msg: types.Message, state: FSMContext):
    await save_user(msg.from_user)
    await state.clear()
    args = msg.text.split(maxsplit=1)

    if len(args) > 1:
        code = args[1]
        entry = await db_get(code)
        if not entry:
            return await msg.answer("❌ Файл не найден.")

        if not await is_subscribed(msg.from_user.id):
            return await msg.answer(
                "🔒 <b>Чтобы продолжить, подпишитесь на канал</b>\n\n"
                "После подписки нажмите «✅ Я подписался»",
                parse_mode="HTML",
                reply_markup=sub_keyboard(code),
            )

        await db_increment(code, entry.get("downloads", 0))
        try:
            await send_file(msg, entry)
        except Exception as e:
            logging.error(f"Send error: {e}")
            await msg.answer("❌ Не удалось отправить файл.")
        return

    role = await get_role(msg.from_user.id)

    if role >= 1:
        users = await count_users()
        rows = await db_all()
        sub_status = "✅ ВКЛ" if sub_required else "❌ ВЫКЛ"

        cmds = (
            f"<b>Ваша роль:</b> {ROLES[role]}\n\n"
            f"📂 Файлов: <b>{len(rows)}</b>\n"
            f"👥 Пользователей: <b>{users}</b>\n"
            f"📢 Подписка: <b>{sub_status}</b>\n\n"
            f"<b>Команды:</b>\n"
            f"📤 Отправьте файл — сохранить\n"
            f"/list — все файлы\n"
            f"/myfiles — мои файлы\n"
            f"/del <code>код</code> — удалить файл\n"
            f"/stats — статистика\n"
            f"/resign — снять админку с себя\n"
        )

        if role >= 2:
            cmds += f"/admins — список админов\n"

        if role >= 3:
            cmds += (
                f"/setadmin — назначить админа\n"
                f"/demote — понизить админа\n"
                f"/removeadmin — снять админа\n"
            )

        if role >= 4:
            cmds += (
                f"/send — рассылка\n"
                f"/sub — вкл/выкл подписку\n"
                f"/notify — вкл/выкл уведомления\n"
                f"/cancel — отмена\n"
            )

        await msg.answer(cmds, parse_mode="HTML")
    else:
        await msg.answer("👋 Привет! Перейдите по ссылке от отправителя.")


# ────────── Подписка ──────────
@router.callback_query(F.data.startswith("checksub:"))
async def check_sub_callback(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]

    if not await is_subscribed(call.from_user.id):
        return await call.answer("❌ Вы ещё не подписались!", show_alert=True)

    await call.message.delete()
    entry = await db_get(code)
    if not entry:
        return await call.message.answer("❌ Файл не найден.")

    await db_increment(code, entry.get("downloads", 0))
    send_method = getattr(call.message, f"answer_{entry['type']}", None)
    if not send_method:
        return await call.message.answer("❌ Неподдерживаемый тип.")

    kw = {}
    if entry["type"] not in NO_CAPTION and entry.get("caption"):
        kw["caption"] = entry["caption"]

    try:
        await send_method(entry["file_id"], **kw)
    except Exception as e:
        logging.error(f"Send error: {e}")
        await call.message.answer("❌ Ошибка отправки.")
    await call.answer()


# ══════════════════════════════════════════════
#  УПРАВЛЕНИЕ АДМИНАМИ
# ══════════════════════════════════════════════

@router.message(Command("setadmin"))
async def cmd_setadmin(msg: types.Message):
    caller_role = await get_role(msg.from_user.id)

    if caller_role < 3:
        return await msg.answer("⛔ Недостаточно прав.")

    parts = msg.text.split()
    if len(parts) < 3:
        return await msg.answer(
            "📝 Формат: /setadmin <code>user_id</code> <code>senior/middle/junior</code>\n\n"
            "Пример: /setadmin 123456789 junior",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await msg.answer("❌ user_id должен быть числом.")

    role_name = parts[2].lower()
    if role_name not in ROLE_COMMANDS:
        return await msg.answer("❌ Роль: senior, middle или junior")

    new_role = ROLE_COMMANDS[role_name]

    if target_id == OWNER_ID:
        return await msg.answer("⛔ Нельзя изменить роль владельца.")

    if new_role >= caller_role and caller_role < 4:
        return await msg.answer("⛔ Нельзя назначить роль равную или выше своей.")

    target_current_role = await get_role(target_id)

    if target_current_role >= caller_role and caller_role < 4:
        return await msg.answer("⛔ Нельзя изменить админа с ролью равной или выше вашей.")

    try:
        chat = await bot.get_chat(target_id)
        username = chat.username or chat.first_name or str(target_id)
    except Exception:
        username = str(target_id)

    await set_admin(target_id, new_role, username)

    await msg.answer(
        f"✅ <b>Админ назначен!</b>\n\n"
        f"👤 {username} (ID: {target_id})\n"
        f"📋 Роль: {ROLES[new_role]}",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            target_id,
            f"🎉 <b>Вам назначена роль:</b> {ROLES[new_role]}\n\n"
            f"Назначил: {get_username_display(msg.from_user)}",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: types.Message):
    caller_role = await get_role(msg.from_user.id)

    if caller_role < 3:
        return await msg.answer("⛔ Недостаточно прав.")

    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer(
            "📝 Формат: /removeadmin <code>user_id</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await msg.answer("❌ user_id должен быть числом.")

    if target_id == OWNER_ID:
        return await msg.answer("⛔ Нельзя снять владельца.")

    target_info = await get_admin_info(target_id)
    if not target_info:
        return await msg.answer("❌ Этот пользователь не админ.")

    target_role = target_info.get("role", 0)
    target_username = target_info.get("username", str(target_id))

    if not can_manage(caller_role, target_role) and caller_role < 4:
        return await msg.answer("⛔ Нельзя снять админа с ролью равной или выше вашей.")

    if caller_role < 4:
        caller_name = get_username_display(msg.from_user)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить",
                    callback_data=f"approve_remove:{target_id}:{msg.from_user.id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"deny_remove:{target_id}:{msg.from_user.id}"
                ),
            ]
        ])

        await bot.send_message(
            OWNER_ID,
            f"⚠️ <b>Запрос на снятие админа</b>\n\n"
            f"{ROLES[caller_role]} {caller_name}\n"
            f"хочет снять {ROLES[target_role]} админа\n"
            f"👤 {target_username} (ID: {target_id})",
            parse_mode="HTML",
            reply_markup=buttons,
        )

        return await msg.answer("📨 Запрос отправлен владельцу. Ожидайте решения.")

    await remove_admin(target_id)
    await msg.answer(
        f"✅ {ROLES[target_role]} {target_username} снят с должности.",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            target_id,
            f"❌ <b>Вы сняты с должности</b> {ROLES[target_role]}",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("demote"))
async def cmd_demote(msg: types.Message):
    caller_role = await get_role(msg.from_user.id)

    if caller_role < 3:
        return await msg.answer("⛔ Недостаточно прав.")

    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer(
            "📝 Формат: /demote <code>user_id</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await msg.answer("❌ user_id должен быть числом.")

    if target_id == OWNER_ID:
        return await msg.answer("⛔ Нельзя понизить владельца.")

    target_info = await get_admin_info(target_id)
    if not target_info:
        return await msg.answer("❌ Этот пользователь не админ.")

    target_role = target_info.get("role", 0)
    target_username = target_info.get("username", str(target_id))

    if target_role <= 1:
        return await msg.answer("❌ Уже на минимальной позиции. Используйте /removeadmin")

    if not can_manage(caller_role, target_role) and caller_role < 4:
        return await msg.answer("⛔ Нельзя понизить админа с ролью равной или выше вашей.")

    new_role = target_role - 1

    if caller_role < 4:
        caller_name = get_username_display(msg.from_user)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить",
                    callback_data=f"approve_demote:{target_id}:{new_role}:{msg.from_user.id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"deny_demote:{target_id}:{msg.from_user.id}"
                ),
            ]
        ])

        await bot.send_message(
            OWNER_ID,
            f"⚠️ <b>Запрос на понижение админа</b>\n\n"
            f"{ROLES[caller_role]} {caller_name}\n"
            f"хочет понизить {ROLES[target_role]} админа\n"
            f"👤 {target_username} (ID: {target_id})\n"
            f"📋 {ROLES[target_role]} → {ROLES[new_role]}",
            parse_mode="HTML",
            reply_markup=buttons,
        )

        return await msg.answer("📨 Запрос отправлен владельцу. Ожидайте решения.")

    await set_admin(target_id, new_role, target_username)
    await msg.answer(
        f"✅ {target_username} понижен:\n"
        f"{ROLES[target_role]} → {ROLES[new_role]}",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            target_id,
            f"⬇️ <b>Вы понижены:</b> {ROLES[target_role]} → {ROLES[new_role]}",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("resign"))
async def cmd_resign(msg: types.Message):
    role = await get_role(msg.from_user.id)

    if msg.from_user.id == OWNER_ID:
        return await msg.answer("👑 Владелец не может снять себя.")

    if role < 1:
        return await msg.answer("❌ Вы не админ.")

    await remove_admin(msg.from_user.id)
    await msg.answer(
        f"✅ Вы сняли с себя роль {ROLES[role]}.",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            OWNER_ID,
            f"ℹ️ {ROLES[role]} {get_username_display(msg.from_user)} снял с себя админку.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.message(Command("admins"))
async def cmd_admins(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role < 2:
        return await msg.answer("⛔ Недостаточно прав.")

    admins = await get_all_admins()
    if not admins:
        return await msg.answer("📋 Админов нет.")

    lines = [f"👑 <b>Владелец:</b> ID {OWNER_ID}\n"]
    for a in admins:
        r = a.get("role", 0)
        u = a.get("username", "?")
        uid = a.get("user_id", "?")
        lines.append(f"{ROLES.get(r, '?')} {u} (ID: {uid})")

    await msg.answer(
        f"📋 <b>Список админов:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════
#  CALLBACK — одобрение/отклонение
# ══════════════════════════════════════════════
@router.callback_query(F.data.startswith("approve_remove:"))
async def approve_remove(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Только владелец.", show_alert=True)

    parts = call.data.split(":")
    target_id = int(parts[1])
    requester_id = int(parts[2])

    await remove_admin(target_id)

    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>ОДОБРЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(target_id, "❌ <b>Вы сняты с должности админа.</b>", parse_mode="HTML")
    except Exception:
        pass

    try:
        await bot.send_message(requester_id, "✅ Ваш запрос на снятие админа одобрен владельцем.")
    except Exception:
        pass

    await call.answer("Одобрено!")


@router.callback_query(F.data.startswith("deny_remove:"))
async def deny_remove(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Только владелец.", show_alert=True)

    parts = call.data.split(":")
    requester_id = int(parts[2])

    await call.message.edit_text(
        call.message.text + "\n\n❌ <b>ОТКЛОНЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(requester_id, "❌ Ваш запрос на снятие админа отклонён владельцем.")
    except Exception:
        pass

    await call.answer("Отклонено!")


@router.callback_query(F.data.startswith("approve_demote:"))
async def approve_demote(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Только владелец.", show_alert=True)

    parts = call.data.split(":")
    target_id = int(parts[1])
    new_role = int(parts[2])
    requester_id = int(parts[3])

    target_info = await get_admin_info(target_id)
    username = target_info.get("username", str(target_id)) if target_info else str(target_id)

    await set_admin(target_id, new_role, username)

    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>ОДОБРЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            target_id,
            f"⬇️ <b>Вы понижены до:</b> {ROLES[new_role]}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(requester_id, "✅ Ваш запрос на понижение одобрен владельцем.")
    except Exception:
        pass

    await call.answer("Одобрено!")


@router.callback_query(F.data.startswith("deny_demote:"))
async def deny_demote(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Только владелец.", show_alert=True)

    parts = call.data.split(":")
    requester_id = int(parts[-1])

    await call.message.edit_text(
        call.message.text + "\n\n❌ <b>ОТКЛОНЕНО</b>",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(requester_id, "❌ Ваш запрос на понижение отклонён владельцем.")
    except Exception:
        pass

    await call.answer("Отклонено!")


# ══════════════════════════════════════════════
#  ПЕРЕКЛЮЧАТЕЛИ
# ══════════════════════════════════════════════
@router.message(Command("sub"))
async def cmd_sub(msg: types.Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.answer("⛔ Только владелец.")

    global sub_required
    sub_required = not sub_required
    if sub_required:
        await msg.answer(
            f"✅ <b>Обязательная подписка ВКЛЮЧЕНА</b>\n\nКанал: {CHANNEL_LINK}",
            parse_mode="HTML",
        )
    else:
        await msg.answer("❌ <b>Обязательная подписка ВЫКЛЮЧЕНА</b>", parse_mode="HTML")


@router.message(Command("notify"))
async def cmd_notify(msg: types.Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.answer("⛔ Только владелец.")

    global notify_uploads
    notify_uploads = not notify_uploads
    status = "✅ ВКЛ" if notify_uploads else "❌ ВЫКЛ"
    await msg.answer(
        f"🔔 <b>Уведомления о загрузках:</b> {status}",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════
#  РАССЫЛКА
# ══════════════════════════════════════════════
@router.message(Command("send"))
async def cmd_send(msg: types.Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return await msg.answer("⛔ Только владелец.")

    users = await count_users()
    await state.set_state(BroadcastState.waiting_message)
    await msg.answer(
        f"📢 <b>Режим рассылки</b>\n\n👥 Получателей: <b>{users}</b>\n\n"
        f"Отправьте сообщение для рассылки.\n/cancel — отмена",
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(msg: types.Message, state: FSMContext):
    role = await get_role(msg.from_user.id)
    if role < 1:
        return
    current = await state.get_state()
    if current:
        await state.clear()
        await msg.answer("❌ Отменено.")
    else:
        await msg.answer("Нечего отменять.")


@router.message(BroadcastState.waiting_message)
async def do_broadcast(msg: types.Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return

    await state.clear()
    user_ids = await get_all_users()
    total = len(user_ids)
    if total == 0:
        return await msg.answer("👥 Нет пользователей.")

    status = await msg.answer(f"📢 Рассылка... 0/{total}")
    sent = failed = blocked = 0

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

        done = sent + failed + blocked
        if done % 25 == 0:
            await asyncio.sleep(1)
        if done % 50 == 0:
            try:
                await status.edit_text(
                    f"📢 Рассылка... {done}/{total}\n✅{sent} 🚫{blocked} ❌{failed}"
                )
            except Exception:
                pass

    await status.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего: <b>{total}</b>\n✅ Доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали: <b>{blocked}</b>\n❌ Ошибки: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════
#  ФАЙЛЫ
# ══════════════════════════════════════════════
@router.message(F.content_type.in_(MEDIA_TYPES))
async def save_file_handler(msg: types.Message, state: FSMContext):
    role = await get_role(msg.from_user.id)

    if role < 1:
        return await msg.answer("⛔ Только админы могут добавлять файлы.")

    current = await state.get_state()
    if current == BroadcastState.waiting_message:
        return

    code = uuid.uuid4().hex[:8]
    entry = {
        "caption": msg.caption or "",
        "downloads": 0,
        "uploaded_by": msg.from_user.id,
        "uploader_role": role,
        "uploader_name": get_username_display(msg.from_user),
    }

    extractors = [
        (msg.document,   "document",   lambda: (msg.document.file_id, msg.document.file_name or "file")),
        (msg.photo,      "photo",      lambda: (msg.photo[-1].file_id, "photo.jpg")),
        (msg.video,      "video",      lambda: (msg.video.file_id, msg.video.file_name or "video.mp4")),
        (msg.audio,      "audio",      lambda: (msg.audio.file_id, msg.audio.file_name or "audio.mp3")),
        (msg.voice,      "voice",      lambda: (msg.voice.file_id, "voice.ogg")),
        (msg.video_note, "video_note", lambda: (msg.video_note.file_id, "circle.mp4")),
        (msg.animation,  "animation",  lambda: (msg.animation.file_id, "animation.gif")),
        (msg.sticker,    "sticker",    lambda: (msg.sticker.file_id, "sticker")),
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
        f"🔑 Код: <code>{code}</code>\n"
        f"👤 Загрузил: {entry['uploader_name']} ({ROLES[role]})\n\n"
        f"🔗 Ссылка:\n<code>{link}</code>",
        parse_mode="HTML",
    )

    if notify_uploads and msg.from_user.id != OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                f"📤 <b>Новый файл загружен</b>\n\n"
                f"👤 {entry['uploader_name']} ({ROLES[role]})\n"
                f"📁 {entry['name']}\n"
                f"🔑 Код: <code>{code}</code>\n"
                f"🔗 {link}",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("myfiles"))
async def cmd_myfiles(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role < 1:
        return await msg.answer("⛔ Недостаточно прав.")

    rows = await db_all()
    my = [e for e in rows if e.get("uploaded_by") == msg.from_user.id]

    if not my:
        return await msg.answer("📂 У вас нет загруженных файлов.")

    lines = []
    for e in my:
        link = f"https://t.me/{BOT_USER}?start={e['code']}"
        lines.append(
            f"📁 <b>{e.get('name','?')}</b> 📥{e.get('downloads',0)}\n"
            f"   <code>{e['code']}</code>\n   {link}"
        )
    text = "\n\n".join(lines)
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i+4000], parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("list"))
async def cmd_list(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role < 1:
        return await msg.answer("⛔ Недостаточно прав.")

    rows = await db_all()
    if not rows:
        return await msg.answer("📂 Пусто.")

    lines = []
    for e in rows:
        link = f"https://t.me/{BOT_USER}?start={e['code']}"
        uploader = e.get("uploader_name", "?")
        up_role = e.get("uploader_role", 0)
        lines.append(
            f"📁 <b>{e.get('name','?')}</b> 📥{e.get('downloads',0)}\n"
            f"   👤 {uploader} ({ROLES.get(up_role, '?')})\n"
            f"   <code>{e['code']}</code>\n   {link}"
        )
    text = "\n\n".join(lines)
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i+4000], parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("del"))
async def cmd_del(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role < 1:
        return await msg.answer("⛔ Недостаточно прав.")

    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Формат: /del <code>код</code>", parse_mode="HTML")

    code = parts[1].strip()
    entry = await db_get(code)
    if not entry:
        return await msg.answer("❌ Не найдено.")

    if not can_delete_file(role, msg.from_user.id, entry):
        return await msg.answer("⛔ Вы не можете удалить этот файл.")

    await db_delete(code)
    await msg.answer(
        f"🗑 Удалено: <b>{entry.get('name','?')}</b>\n"
        f"👤 Загружал: {entry.get('uploader_name', '?')}",
        parse_mode="HTML",
    )


@router.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role < 1:
        return await msg.answer("⛔ Недостаточно прав.")

    rows = await db_all()
    users = await count_users()
    total = len(rows)
    dl = sum(e.get("downloads", 0) for e in rows)
    top = sorted(rows, key=lambda x: x.get("downloads", 0), reverse=True)[:5]
    t = "\n".join(
        f"  📁 {e.get('name','?')} — {e.get('downloads',0)} "
        f"({e.get('uploader_name','?')})"
        for e in top
    )
    sub_status = "✅ ВКЛ" if sub_required else "❌ ВЫКЛ"
    admins = await get_all_admins()

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"📁 Файлов: <b>{total}</b>\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📥 Скачиваний: <b>{dl}</b>\n"
        f"👮 Админов: <b>{len(admins)}</b>\n"
        f"📢 Подписка: <b>{sub_status}</b>"
    )
    if t:
        text += f"\n\n🔝 <b>Топ-5:</b>\n{t}"
    await msg.answer(text, parse_mode="HTML")


@router.message()
async def fallback(msg: types.Message):
    role = await get_role(msg.from_user.id)
    if role >= 1:
        await msg.answer("📤 Отправьте файл для сохранения.\n/list · /myfiles · /stats")
    else:
        await msg.answer("Перейдите по ссылке от отправителя.")


dp.include_router(router)


# ══════════════════════════════════════════════
#  ЗАПУСК (WEBHOOK для Render)
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
