"""
Бот анонимных групп — aiogram 3, HTML-разметка, SQLite, всё в одном файле.

Запуск:
    pip install -U aiogram
    export BOT_TOKEN="123456:ABC..."      # токен от @BotFather
    python anon_groups_bot.py

Как это работает: участники пишут боту в личку, бот пересылает сообщение
всем остальным в группе от своего имени с ником отправителя.
"""
import asyncio
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from html import escape as esc
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.utils.text_decorations import html_decoration

# ───────────────────────── Настройки ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
DB_PATH = "anon_groups.db"
MEDIA_LIMIT_MB = 100                       # лимит медиа на человека в сутки
MEDIA_LIMIT = MEDIA_LIMIT_MB * 1024 * 1024
RELAY_TTL = 3 * 24 * 3600                  # сколько хранить связку «сообщение → автор» (для модерации ответом)
DEFAULT_MUTE_MIN = 10
MAX_MUTE_MIN = 7 * 24 * 60
MAX_TEXT_LEN = 3500

NICK_RE = re.compile(r"[\w-]{3,20}")
CAPTION_TYPES = ("photo", "video", "document", "audio", "voice", "animation")
ALLOWED_TYPES = CAPTION_TYPES + ("text", "sticker", "video_note")
ROLE_ICON = {"owner": "👑", "moderator": "🛡", "member": "•"}
ROLE_NAME = {"owner": "владелец", "moderator": "модератор", "member": "участник"}

B_GROUP, B_MEMBERS = "👥 Группа", "📋 Участники"
B_NICK, B_PANEL = "✏️ Сменить ник", "⚙️ Управление"
B_HELP, B_ABOUT = "❓ Помощь", "ℹ️ О боте"

COMMANDS = [
    ("start", "Начало / регистрация"),
    ("newgroup", "Создать группу"),
    ("group", "Моя группа"),
    ("members", "Участники"),
    ("nick", "Сменить ник"),
    ("leave", "Выйти из группы"),
    ("panel", "Управление группой (модератор)"),
    ("kick", "Исключить (модератор)"),
    ("mute", "Заглушить (модератор)"),
    ("unmute", "Снять мут (модератор)"),
    ("link", "Ссылка-приглашение (модератор)"),
    ("newlink", "Обновить ссылку (модератор)"),
    ("mod", "Назначить модератора (владелец)"),
    ("unmod", "Снять модератора (владелец)"),
    ("help", "Помощь"),
    ("about", "О боте"),
]

HELP_TEXT = """❓ <b>Помощь</b>

<b>Как общаться</b>
Просто пишите боту — сообщение уйдёт всем в группе под вашим ником.

<b>Основное</b>
/newgroup Название — создать группу
/group — о моей группе
/members — кто в группе
/nick — сменить ник
/leave — выйти из группы

<b>Модераторы</b> (ответьте командой на сообщение или укажите ник)
/kick ник — исключить
/mute ник 30 — заглушить на 30 минут
/unmute ник — снять мут
/link — ссылка-приглашение
/newlink — обновить ссылку (старая перестанет работать)
/panel — панель управления

<b>Владелец</b>
/mod ник — назначить модератором
/unmod ник — снять права модератора
/panel — защита от пересылки, медиа, удаление группы"""

ABOUT_TEXT = f"""ℹ️ <b>О боте</b>

Бот анонимных групп. Вас видят только под ником — ваш Telegram-аккаунт скрыт от всех, включая владельца и модераторов группы.

• Вход только по ссылке-приглашению, ссылку можно обновить
• Один аккаунт — одна группа
• Защита от пересылки и сохранения — настройка группы
• Лимит медиа: {MEDIA_LIMIT_MB} МБ в сутки на человека (сброс в 00:00 UTC)
• Контакты, геопозиция и опросы не передаются, чтобы вас не раскрыть
• Правка и удаление сообщений у других участников не синхронизируются

<b>Что хранится:</b> ник, членство в группе и связка «сообщение → автор» на {RELAY_TTL // 86400} дня (нужна, чтобы модератор мог ответом на сообщение применить /kick или /mute). Текст сообщений не хранится. Тот, кто запустил бота, технически имеет доступ к базе."""

# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    nick TEXT, nick_lc TEXT UNIQUE,
    state TEXT DEFAULT '',            -- '' или 'nick' (ждём ник)
    pending TEXT DEFAULT '',          -- токен приглашения, ждущий регистрации
    created INTEGER
);
CREATE TABLE IF NOT EXISTS groups(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, owner_id INTEGER, token TEXT UNIQUE,
    protect INTEGER DEFAULT 1,        -- запрет пересылки и сохранения
    media INTEGER DEFAULT 1,          -- разрешены ли медиа
    created INTEGER
);
CREATE TABLE IF NOT EXISTS members(
    user_id INTEGER PRIMARY KEY,      -- один пользователь — одна группа
    group_id INTEGER, role TEXT DEFAULT 'member',
    muted_until INTEGER DEFAULT 0, joined INTEGER
);
CREATE INDEX IF NOT EXISTS ix_members_group ON members(group_id);
CREATE TABLE IF NOT EXISTS media_usage(
    user_id INTEGER, day TEXT, bytes INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS relay(
    chat_id INTEGER, msg_id INTEGER, sender_id INTEGER, group_id INTEGER, ts INTEGER,
    PRIMARY KEY(chat_id, msg_id)
);
""")


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def one(sql, args=()):
    return db.execute(sql, args).fetchone()


def many(sql, args=()):
    return db.execute(sql, args).fetchall()


def now() -> int:
    return int(time.time())


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def mb(n: int) -> str:
    return f"{n / 1048576:.1f}"


def ensure_user(uid: int):
    run("INSERT OR IGNORE INTO users(user_id, created) VALUES(?,?)", (uid, now()))
    return one("SELECT * FROM users WHERE user_id=?", (uid,))


def get_member(uid: int):
    return one("""SELECT m.user_id, m.group_id, m.role, m.muted_until,
                         g.title, g.protect, g.media, g.token
                  FROM members m JOIN groups g ON g.id = m.group_id
                  WHERE m.user_id=?""", (uid,))


def count_members(gid: int) -> int:
    return one("SELECT COUNT(*) AS c FROM members WHERE group_id=?", (gid,))["c"]


def used_today(uid: int) -> int:
    r = one("SELECT bytes FROM media_usage WHERE user_id=? AND day=?", (uid, today()))
    return r["bytes"] if r else 0


def add_usage(uid: int, size: int):
    run("""INSERT INTO media_usage(user_id, day, bytes) VALUES(?,?,?)
           ON CONFLICT(user_id, day) DO UPDATE SET bytes = bytes + excluded.bytes""",
        (uid, today(), size))


def new_token(gid: int) -> str:
    token = secrets.token_urlsafe(9)
    run("UPDATE groups SET token=? WHERE id=?", (token, gid))
    return token


# ───────────────────────── Вспомогательное ─────────────────────────
log = logging.getLogger("anon-bot")
router = Router()
router.message.filter(F.chat.type == "private")
bot: Bot = None  # type: ignore  # создаётся в main()
BOT_USERNAME = ""


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=B_GROUP), KeyboardButton(text=B_MEMBERS)],
            [KeyboardButton(text=B_NICK), KeyboardButton(text=B_PANEL)],
            [KeyboardButton(text=B_HELP), KeyboardButton(text=B_ABOUT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Написать в группу…",
    )


def link_text(token: str, title: str) -> str:
    return (f"🔗 Ссылка для входа в «{esc(title)}»:\n"
            f"https://t.me/{BOT_USERNAME}?start=g_{token}\n\n"
            "Отправляйте её только тем, кого хотите пригласить. Если ссылка утекла — обновите её.")


def panel_text(mem) -> str:
    return (f"⚙️ <b>Управление группой «{esc(mem['title'])}»</b>\n"
            f"Участников: {count_members(mem['group_id'])}\n\n"
            "🛡 Защита — сообщения нельзя пересылать и сохранять\n"
            "🖼 Медиа — можно ли слать фото, видео и файлы")


def panel_kb(mem) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text="🔗 Ссылка", callback_data="p:link"),
           InlineKeyboardButton(text="♻️ Обновить ссылку", callback_data="p:newlink")]]
    if mem["role"] == "owner":
        kb.append([InlineKeyboardButton(
            text=f"🛡 Защита от пересылки: {'вкл' if mem['protect'] else 'выкл'}", callback_data="p:protect")])
        kb.append([InlineKeyboardButton(
            text=f"🖼 Медиа: {'разрешены' if mem['media'] else 'запрещены'}", callback_data="p:media")])
        kb.append([InlineKeyboardButton(text="🗑 Удалить группу", callback_data="p:del")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except TelegramAPIError as e:
        log.info("не отправлено %s: %s", uid, e)


async def announce(gid: int, text: str, exclude=()):
    """Служебное сообщение всем в группе."""
    for r in many("SELECT user_id FROM members WHERE group_id=?", (gid,)):
        if r["user_id"] not in exclude:
            await notify(r["user_id"], text)
            await asyncio.sleep(0.04)


async def reg(m: Message):
    """Вернёт пользователя, если у него есть ник, иначе попросит придумать."""
    u = ensure_user(m.from_user.id)
    if not u["nick"]:
        run("UPDATE users SET state='nick' WHERE user_id=?", (u["user_id"],))
        await m.answer("✏️ Сначала придумайте ник — отправьте его сообщением "
                       "(3–20 символов: буквы, цифры, _ и -).")
        return None
    return u


async def staff(m: Message, owner_only: bool = False):
    """Вернёт участника, если он владелец/модератор своей группы."""
    if not await reg(m):
        return None
    mem = get_member(m.from_user.id)
    allowed = ("owner",) if owner_only else ("owner", "moderator")
    if not mem or mem["role"] not in allowed:
        await m.answer("🚫 Только для владельца группы." if owner_only
                       else "🚫 Только для владельца и модераторов группы.")
        return None
    return mem


def find_target(m: Message, args: list, mem):
    """Цель модерации: по ответу на сообщение или по нику. Возвращает (цель, остаток_аргументов)."""
    gid = mem["group_id"]
    base = ("SELECT m.user_id, m.role, u.nick FROM members m "
            "JOIN users u ON u.user_id = m.user_id WHERE m.group_id=? AND ")
    if m.reply_to_message:
        r = one("SELECT sender_id FROM relay WHERE chat_id=? AND msg_id=? AND group_id=?",
                (m.chat.id, m.reply_to_message.message_id, gid))
        if r:
            return one(base + "m.user_id=?", (gid, r["sender_id"])), args
    if args:
        return one(base + "u.nick_lc=?", (gid, args[0].lstrip("@").lower())), args[1:]
    return None, args


async def mod_ctx(m: Message, command: CommandObject, owner_only: bool = False):
    """Общая проверка для /kick /mute /unmute /mod /unmod. Вернёт (я, цель, аргументы) или None."""
    mem = await staff(m, owner_only)
    if not mem:
        return None
    t, rest = find_target(m, (command.args or "").split(), mem)
    if not t:
        await m.answer("Не нашёл участника. Ответьте командой на его сообщение "
                       "или укажите ник, например <code>/kick ник</code>.")
        return None
    err = None
    if t["user_id"] == mem["user_id"]:
        err = "🙂 К себе это применить нельзя."
    elif t["role"] == "owner":
        err = "🚫 Владельца трогать нельзя."
    elif mem["role"] == "moderator" and t["role"] == "moderator":
        err = "🚫 Модератор не может действовать против другого модератора."
    if err:
        await m.answer(err)
        return None
    return mem, t, rest


# ───────────────────────── Рассылка сообщений ─────────────────────────
def to_html(m: Message) -> str:
    raw = m.text or m.caption or ""
    ents = m.entities or m.caption_entities or []
    return html_decoration.unparse(raw, ents) if raw else ""


def media_size(m: Message) -> int:
    if m.photo:
        return m.photo[-1].file_size or 0
    for obj in (m.video, m.document, m.audio, m.voice, m.animation, m.video_note, m.sticker):
        if obj:
            return obj.file_size or 0
    return 0


async def deliver(m: Message, chat_id: int, nick: str, protect: bool) -> list:
    """Отправляет одно сообщение одному получателю, возвращает id отправленных сообщений."""
    head = f"<b>{esc(nick)}</b>"
    if m.text:
        r = await bot.send_message(chat_id, f"{head}:\n{to_html(m)}", protect_content=protect)
        return [r.message_id]
    ctype = str(getattr(m.content_type, "value", m.content_type))
    cap = to_html(m)
    text = head + (f"\n{cap}" if cap else "")
    if ctype in CAPTION_TYPES and len(text) <= 1000:      # ник — в подписи к медиа
        r = await bot.copy_message(chat_id, m.chat.id, m.message_id, caption=text, protect_content=protect)
        return [r.message_id]
    h = await bot.send_message(chat_id, f"{head}:", protect_content=protect)   # стикеры/кружки: ник отдельной строкой
    r = await bot.copy_message(chat_id, m.chat.id, m.message_id, protect_content=protect)
    return [h.message_id, r.message_id]


async def relay(m: Message, u, mem):
    protect = bool(mem["protect"])
    for r in many("SELECT user_id FROM members WHERE group_id=? AND user_id!=?",
                  (mem["group_id"], u["user_id"])):
        try:
            for mid in await deliver(m, r["user_id"], u["nick"], protect):
                db.execute("INSERT OR REPLACE INTO relay VALUES(?,?,?,?,?)",
                           (r["user_id"], mid, u["user_id"], mem["group_id"], now()))
        except TelegramAPIError as e:
            log.warning("не доставлено %s: %s", r["user_id"], e)
        await asyncio.sleep(0.04)      # ~25 сообщений/сек, чтобы не упереться в лимиты Telegram
    db.commit()


# ───────────────────────── Регистрация и ник ─────────────────────────
async def finish_nick(m: Message, u, text: str):
    nick = text.strip().lstrip("@")
    if not NICK_RE.fullmatch(nick):
        await m.answer("❌ Ник: 3–20 символов, только буквы, цифры, _ и -. Попробуйте ещё раз.")
        return
    if one("SELECT 1 FROM users WHERE nick_lc=? AND user_id!=?", (nick.lower(), u["user_id"])):
        await m.answer("❌ Этот ник занят. Придумайте другой.")
        return
    old, token, uid = u["nick"], u["pending"], u["user_id"]
    run("UPDATE users SET nick=?, nick_lc=?, state='', pending='' WHERE user_id=?",
        (nick, nick.lower(), uid))
    if old:
        await m.answer(f"✅ Ник изменён: <b>{esc(nick)}</b>", reply_markup=main_kb())
        mem = get_member(uid)
        if mem:
            await announce(mem["group_id"], f"✏️ <i>{esc(old)} теперь {esc(nick)}</i>", exclude=(uid,))
        return
    await m.answer(f"✅ Ник <b>{esc(nick)}</b> сохранён!\n\n"
                   "Создайте группу: <code>/newgroup Название</code>\n"
                   "или откройте ссылку-приглашение от владельца группы.\n"
                   "Меню — внизу 👇", reply_markup=main_kb())
    if token:
        await join_group(m, token)


async def join_group(m: Message, token: str):
    uid = m.from_user.id
    u = ensure_user(uid)
    g = one("SELECT * FROM groups WHERE token=?", (token,))
    if not g:
        await m.answer("❌ Ссылка недействительна или устарела — попросите у владельца новую.",
                       reply_markup=main_kb())
        return
    cur = get_member(uid)
    if cur:
        txt = ("Вы уже в этой группе." if cur["group_id"] == g["id"] else
               f"Вы уже состоите в группе «{esc(cur['title'])}». Чтобы вступить в другую, сначала выйдите: /leave")
        await m.answer(txt, reply_markup=main_kb())
        return
    run("INSERT INTO members(user_id, group_id, role, joined) VALUES(?,?,?,?)",
        (uid, g["id"], "member", now()))
    await m.answer(f"✅ Вы в группе «{esc(g['title'])}».\n"
                   f"Пишите сюда — сообщения увидят все участники под ником <b>{esc(u['nick'])}</b>.",
                   reply_markup=main_kb())
    await announce(g["id"], f"➕ <i>Участник {esc(u['nick'])} теперь в группе</i>", exclude=(uid,))


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    u = ensure_user(m.from_user.id)
    arg = (command.args or "").strip()
    token = arg[2:] if arg.startswith("g_") else ""
    if not u["nick"]:
        run("UPDATE users SET state='nick', pending=? WHERE user_id=?", (token, u["user_id"]))
        await m.answer("👋 <b>Добро пожаловать!</b>\nЭто бот анонимных групп: в группах вас видят только под ником.\n\n"
                       "✏️ Придумайте ник — отправьте его сообщением (3–20 символов: буквы, цифры, _ и -).")
        return
    run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
    if token:
        await join_group(m, token)
        return
    mem = get_member(u["user_id"])
    where = (f"Вы в группе «{esc(mem['title'])}». Просто пишите сюда." if mem else
             "Создайте группу: <code>/newgroup Название</code> или откройте ссылку-приглашение.")
    await m.answer(f"👋 Привет, <b>{esc(u['nick'])}</b>!\n{where}", reply_markup=main_kb())


@router.message(Command("nick"))
@router.message(F.text == B_NICK)
async def cmd_nick(m: Message, command: Optional[CommandObject] = None):
    u = ensure_user(m.from_user.id)
    if command and command.args:
        await finish_nick(m, u, command.args)
        return
    run("UPDATE users SET state='nick' WHERE user_id=?", (u["user_id"],))
    await m.answer("✏️ Отправьте новый ник: 3–20 символов, буквы, цифры, _ и -."
                   + ("\nОтмена — /cancel" if u["nick"] else ""))


# ───────────────────────── Группы ─────────────────────────
@router.message(Command("newgroup"))
async def cmd_newgroup(m: Message, command: CommandObject):
    u = await reg(m)
    if not u:
        return
    if get_member(u["user_id"]):
        await m.answer("Вы уже в группе. Выйдите (/leave) или, если вы владелец, удалите её в /panel.")
        return
    title = (command.args or "").strip()[:40]
    if not title:
        await m.answer("Напишите название: <code>/newgroup Моя группа</code>")
        return
    token = secrets.token_urlsafe(9)
    gid = run("INSERT INTO groups(title, owner_id, token, created) VALUES(?,?,?,?)",
              (title, u["user_id"], token, now())).lastrowid
    run("INSERT INTO members(user_id, group_id, role, joined) VALUES(?,?,'owner',?)",
        (u["user_id"], gid, now()))
    await m.answer(f"🎉 Группа «{esc(title)}» создана!\n\n{link_text(token, title)}\n\n"
                   "Настройки — /panel, команды — /help", reply_markup=main_kb())


@router.message(Command("group"))
@router.message(F.text == B_GROUP)
async def cmd_group(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer("Вы пока не в группе.\n• Создать: <code>/newgroup Название</code>\n"
                       "• Или откройте ссылку-приглашение от владельца группы.")
        return
    await m.answer(
        f"👥 <b>{esc(mem['title'])}</b>\n"
        f"Ваш ник: <b>{esc(u['nick'])}</b> · роль: {ROLE_NAME[mem['role']]}\n"
        f"Участников: {count_members(mem['group_id'])}\n"
        f"Защита от пересылки: {'вкл' if mem['protect'] else 'выкл'}\n"
        f"Медиа: {'разрешены' if mem['media'] else 'запрещены'}\n"
        f"Ваш лимит медиа сегодня: {mb(used_today(u['user_id']))} / {MEDIA_LIMIT_MB} МБ")


@router.message(Command("members"))
@router.message(F.text == B_MEMBERS)
async def cmd_members(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer("Вы не в группе.")
        return
    lines = []
    for r in many("""SELECT u.user_id, u.nick, m.role, m.muted_until
                     FROM members m JOIN users u ON u.user_id = m.user_id
                     WHERE m.group_id=?
                     ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, u.nick_lc""",
                  (mem["group_id"],)):
        tags = (" (вы)" if r["user_id"] == u["user_id"] else "") + (" 🔇" if r["muted_until"] > now() else "")
        lines.append(f"{ROLE_ICON[r['role']]} {esc(r['nick'])}{tags}")
    await m.answer(f"📋 <b>Участники «{esc(mem['title'])}»</b>\n" + "\n".join(lines)[:3800])


@router.message(Command("leave"))
async def cmd_leave(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer("Вы не в группе.")
        return
    if mem["role"] == "owner":
        await m.answer("👑 Владелец не может выйти. Удалите группу в /panel.")
        return
    run("DELETE FROM members WHERE user_id=?", (u["user_id"],))
    await m.answer("👋 Вы вышли из группы.", reply_markup=main_kb())
    await announce(mem["group_id"], f"👋 <i>Участник {esc(u['nick'])} вышел из группы</i>")


# ───────────────────────── Модерация ─────────────────────────
@router.message(Command("kick"))
async def cmd_kick(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command)
    if not ctx:
        return
    mem, t, _ = ctx
    run("DELETE FROM members WHERE user_id=?", (t["user_id"],))
    await m.answer(f"🚪 {esc(t['nick'])} исключён из группы.")
    await notify(t["user_id"], f"🚪 Вас исключили из группы «{esc(mem['title'])}». "
                               "Вернуться можно только по действующей ссылке-приглашению.")
    await announce(mem["group_id"], f"🚪 <i>Участник {esc(t['nick'])} исключён из группы</i>",
                   exclude=(mem["user_id"],))


@router.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command)
    if not ctx:
        return
    mem, t, rest = ctx
    minutes = DEFAULT_MUTE_MIN
    if rest and rest[0].isdigit():
        minutes = max(1, min(int(rest[0]), MAX_MUTE_MIN))
    run("UPDATE members SET muted_until=? WHERE user_id=?", (now() + minutes * 60, t["user_id"]))
    await m.answer(f"🔇 {esc(t['nick'])} в муте на {minutes} мин.")
    await notify(t["user_id"], f"🔇 Вы в муте на {minutes} мин.: читать можно, писать нельзя.")
    await announce(mem["group_id"], f"🔇 <i>Участник {esc(t['nick'])} в муте на {minutes} мин.</i>",
                   exclude=(t["user_id"], mem["user_id"]))


@router.message(Command("unmute"))
async def cmd_unmute(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command)
    if not ctx:
        return
    mem, t, _ = ctx
    run("UPDATE members SET muted_until=0 WHERE user_id=?", (t["user_id"],))
    await m.answer(f"🔊 Мут с {esc(t['nick'])} снят.")
    await notify(t["user_id"], "🔊 Мут снят — можно писать.")


@router.message(Command("mod"))
async def cmd_mod(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command, owner_only=True)
    if not ctx:
        return
    mem, t, _ = ctx
    if t["role"] == "moderator":
        await m.answer("Он уже модератор.")
        return
    run("UPDATE members SET role='moderator' WHERE user_id=?", (t["user_id"],))
    await m.answer(f"🛡 {esc(t['nick'])} теперь модератор.")
    await notify(t["user_id"], "🛡 Вас назначили модератором. Команды — /help, панель — /panel.")


@router.message(Command("unmod"))
async def cmd_unmod(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command, owner_only=True)
    if not ctx:
        return
    mem, t, _ = ctx
    if t["role"] != "moderator":
        await m.answer("Он не модератор.")
        return
    run("UPDATE members SET role='member' WHERE user_id=?", (t["user_id"],))
    await m.answer(f"{esc(t['nick'])} больше не модератор.")
    await notify(t["user_id"], "С вас сняли права модератора.")


@router.message(Command("link"))
async def cmd_link(m: Message):
    mem = await staff(m)
    if mem:
        await m.answer(link_text(mem["token"], mem["title"]))


@router.message(Command("newlink"))
async def cmd_newlink(m: Message):
    mem = await staff(m)
    if mem:
        token = new_token(mem["group_id"])
        await m.answer("♻️ Ссылка обновлена, старая больше не работает.\n\n" + link_text(token, mem["title"]))


# ───────────────────────── Панель управления ─────────────────────────
@router.message(Command("panel"))
@router.message(F.text == B_PANEL)
async def cmd_panel(m: Message):
    mem = await staff(m)
    if mem:
        await m.answer(panel_text(mem), reply_markup=panel_kb(mem))


async def refresh_panel(c: CallbackQuery, mem):
    try:
        await c.message.edit_text(panel_text(mem), reply_markup=panel_kb(mem))
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("p:"))
async def panel_cb(c: CallbackQuery):
    uid = c.from_user.id
    mem = get_member(uid)
    if not mem or mem["role"] not in ("owner", "moderator"):
        await c.answer("Нет доступа", show_alert=True)
        return
    act, gid, owner = c.data[2:], mem["group_id"], mem["role"] == "owner"

    if act == "link":
        await c.message.answer(link_text(mem["token"], mem["title"]))
    elif act == "newlink":
        token = new_token(gid)
        await c.message.answer("♻️ Ссылка обновлена, старая больше не работает.\n\n"
                               + link_text(token, mem["title"]))
    elif act == "cancel":
        await refresh_panel(c, mem)
    elif owner and act in ("protect", "media"):
        run(f"UPDATE groups SET {act}=1-{act} WHERE id=?", (gid,))
        mem = get_member(uid)
        on = bool(mem[act])
        if act == "protect":
            text = ("🛡 Защита включена: сообщения нельзя пересылать и сохранять." if on
                    else "🛡 Защита выключена: сообщения можно пересылать.")
        else:
            text = "🖼 Медиа разрешены." if on else "🖼 Медиа запрещены — только текст."
        await announce(gid, f"<i>{text}</i>", exclude=(uid,))
        await refresh_panel(c, mem)
    elif owner and act == "del":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="p:delyes"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data="p:cancel")]])
        await c.message.edit_text(f"🗑 Удалить группу «{esc(mem['title'])}»? "
                                  "Все участники будут исключены. Это необратимо.", reply_markup=kb)
    elif owner and act == "delyes":
        ids = [r["user_id"] for r in many("SELECT user_id FROM members WHERE group_id=?", (gid,))]
        run("DELETE FROM members WHERE group_id=?", (gid,))
        run("DELETE FROM relay WHERE group_id=?", (gid,))
        run("DELETE FROM groups WHERE id=?", (gid,))
        await c.message.edit_text("🗑 Группа удалена.")
        for i in ids:
            if i != uid:
                await notify(i, f"🗑 Группа «{esc(mem['title'])}» удалена владельцем.")
    else:
        await c.answer("Нет доступа", show_alert=True)
        return
    await c.answer()


# ───────────────────────── Помощь и информация ─────────────────────────
@router.message(Command("help"))
@router.message(F.text == B_HELP)
async def cmd_help(m: Message):
    await m.answer(HELP_TEXT)


@router.message(Command("about"))
@router.message(F.text == B_ABOUT)
async def cmd_about(m: Message):
    await m.answer(ABOUT_TEXT)


# ───────────────────────── Ввод ника и обычные сообщения ─────────────────────────
async def waiting_nick(m: Message) -> bool:
    r = one("SELECT state FROM users WHERE user_id=?", (m.from_user.id,))
    return bool(r and r["state"] == "nick")


@router.message(F.text, waiting_nick)
async def on_nick_text(m: Message):
    u = ensure_user(m.from_user.id)
    if m.text.strip().lower().startswith("/cancel") and u["nick"]:
        run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
        await m.answer("Отменено.", reply_markup=main_kb())
        return
    await finish_nick(m, u, m.text)


@router.message(F.text.startswith("/"))
async def unknown_command(m: Message):
    await m.answer("🤷 Неизвестная команда. Список — /help")


@router.message()
async def on_message(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer("Вы не в группе. Создайте её: <code>/newgroup Название</code> "
                       "или откройте ссылку-приглашение.")
        return
    ctype = str(getattr(m.content_type, "value", m.content_type))
    if ctype not in ALLOWED_TYPES:
        await m.answer("🚫 Такой тип сообщений не поддерживается (контакты, геопозиция и опросы "
                       "могут раскрыть вас). Можно: текст, фото, видео, файлы, голосовые, стикеры.")
        return
    if mem["muted_until"] > now():
        left = -(-(mem["muted_until"] - now()) // 60)
        await m.answer(f"🔇 Вы в муте ещё {left} мин.")
        return
    if m.text:
        if len(m.text) > MAX_TEXT_LEN:
            await m.answer(f"✂️ Слишком длинное сообщение (максимум {MAX_TEXT_LEN} символов).")
            return
    else:
        if not mem["media"]:
            await m.answer("🖼 В этой группе медиа запрещены — только текст.")
            return
        size, used = media_size(m), used_today(u["user_id"])
        if used + size > MEDIA_LIMIT:
            left = max(0, MEDIA_LIMIT - used)
            await m.answer(f"📦 Дневной лимит медиа — {MEDIA_LIMIT_MB} МБ. "
                           f"Осталось {mb(left)} МБ, файл весит {mb(size)} МБ. "
                           "Лимит сбрасывается в 00:00 UTC.")
            return
        add_usage(u["user_id"], size)
    await relay(m, u, mem)


# ───────────────────────── Запуск ─────────────────────────
async def cleanup_loop():
    while True:
        run("DELETE FROM relay WHERE ts<?", (now() - RELAY_TTL,))
        run("DELETE FROM media_usage WHERE day<?", (today(),))
        await asyncio.sleep(3600)


async def main():
    global bot, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    if ":" not in TOKEN:
        raise SystemExit("Укажите токен бота: export BOT_TOKEN=... (получить у @BotFather)")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(cleanup_loop())
    log.info("Бот @%s запущен", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
