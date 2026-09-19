"""
Бот анонимных групп — aiogram 3, HTML-разметка, SQLite, всё в одном файле.

Запуск:
    pip install -U aiogram
    export BOT_TOKEN="123456:ABC..."      # токен от @BotFather
    python anon_groups_bot.py

Как это работает: участники пишут боту в личку, бот пересылает сообщение
всем остальным в активной группе от своего имени с ником отправителя.
Один человек может состоять максимум в 10 группах и переключаться между ними (/groups).
Сообщения из группы доходят только тем, у кого она сейчас активна — если человек сидит
в другой группе, сообщения из фоновой группы к нему не приходят, пока он не переключится.
База старой версии (одна группа на человека) обновляется автоматически при запуске.
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

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyParameters,
)
from aiogram.utils.text_decorations import html_decoration

# ───────────────────────── Настройки ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
# ВАЖНО для хостинга с редеплоями: DB_PATH должен указывать на диск, который переживает
# передеплой (persistent volume / persistent disk). Если оставить путь внутри папки с кодом,
# при каждом обновлении из GitHub хостинг может пересоздавать эту папку и база будет стираться.
os.makedirs("/app/data", exist_ok=True)
DB_PATH = os.getenv("DB_PATH", "/app/data/anon_groups.db")
MAX_GROUPS = 10                            # максимум групп на одного человека
TITLE_MAX = 40                             # максимальная длина названия группы
RELAY_TTL = 3 * 24 * 3600                  # сколько хранить связку «сообщение → автор» (для модерации ответом)
DEFAULT_MUTE_MIN = 10
MAX_MUTE_MIN = 7 * 24 * 60
MAX_TEXT_LEN = 3500

NICK_RE = re.compile(r"[\w-]{3,20}")
CAPTION_TYPES = ("photo", "video", "document", "audio", "voice", "animation")
ALLOWED_TYPES = CAPTION_TYPES + ("text", "sticker", "video_note")
ROLE_ICON = {"owner": "👑", "moderator": "🛡", "member": "•"}
ROLE_NAME = {"owner": "владелец", "moderator": "модератор", "member": "участник"}

B_GROUP, B_GROUPS, B_MEMBERS = "👥 Группа", "🗂 Мои группы", "📋 Участники"
B_NICK, B_PANEL = "✏️ Сменить ник", "⚙️ Управление"
B_STATS = "📊 Статистика"
B_HELP, B_ABOUT = "❓ Помощь", "ℹ️ О боте"
MENU_BUTTONS = {B_GROUP, B_GROUPS, B_MEMBERS, B_NICK, B_PANEL, B_STATS, B_HELP, B_ABOUT}

COMMANDS = [
    ("start", "Начало / регистрация"),
    ("newgroup", "Создать группу"),
    ("groups", "Мои группы и переключение"),
    ("group", "Активная группа"),
    ("members", "Участники"),
    ("stats", "Статистика (личная и по группе)"),
    ("nick", "Сменить ник"),
    ("leave", "Выйти из группы"),
    ("cancel", "Отменить ввод"),
    ("panel", "Управление группой (модератор)"),
    ("rename", "Сменить название группы (владелец)"),
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

HELP_TEXT = f"""❓ <b>Помощь</b>

<b>Как общаться</b>
Просто пишите боту — сообщение уйдёт всем в активной группе под вашим ником.
Сообщения других ваших групп при этом не приходят — переключайтесь между ними через /groups.

<b>Основное</b>
/newgroup — создать группу (название — следующим сообщением)
/groups — мои группы и переключение между ними
/group — об активной группе
/members — кто в группе
/stats — статистика: сколько сообщений и медиа отправили вы и вся группа
/nick — сменить ник
/leave — выйти из активной группы
/cancel — отменить ввод

Состоять можно максимум в {MAX_GROUPS} группах.

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
/rename — сменить название группы
/panel — защита от пересылки, медиа, название, удаление группы"""

ABOUT_TEXT = f"""ℹ️ <b>О боте</b>

Бот анонимных групп. Вас видят только под ником — ваш Telegram-аккаунт скрыт от всех, включая владельца и модераторов группы.

• Вход только по ссылке-приглашению, ссылку можно обновить
• До {MAX_GROUPS} групп на один аккаунт, между ними можно переключаться (/groups)
• Сообщения приходят только из активной группы — остальные группы «молчат» в фоне, пока вы на них не переключитесь
• Защита от пересылки и сохранения — настройка группы
• Контакты, геопозиция и опросы не передаются, чтобы вас не раскрыть
• Правка и удаление сообщений у других участников не синхронизируются

<b>Что хранится:</b> ник, членство в группах и связка «сообщение → автор» на {RELAY_TTL // 86400} дня (нужна, чтобы модератор мог ответом на сообщение применить /kick или /mute). Текст сообщений не хранится. Тот, кто запустил бота, технически имеет доступ к базе."""

# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    nick TEXT, nick_lc TEXT UNIQUE,
    state TEXT DEFAULT '',            -- '' | 'nick' | 'newgroup' | 'rename:<id группы>'
    pending TEXT DEFAULT '',          -- токен приглашения, ждущий регистрации
    active_group INTEGER DEFAULT 0,   -- группа, в которую уходят сообщения
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
    user_id INTEGER, group_id INTEGER, role TEXT DEFAULT 'member',
    muted_until INTEGER DEFAULT 0, joined INTEGER,
    PRIMARY KEY(user_id, group_id)    -- один человек может состоять в нескольких группах
);
CREATE TABLE IF NOT EXISTS relay(
    chat_id INTEGER, msg_id INTEGER, sender_id INTEGER, group_id INTEGER, ts INTEGER,
    src_chat_id INTEGER, src_msg_id INTEGER,   -- «оригинал»: чат и id исходного сообщения отправителя,
    PRIMARY KEY(chat_id, msg_id)               -- общий для всех копий этого сообщения у получателей
);
CREATE TABLE IF NOT EXISTS stats(
    user_id INTEGER, group_id INTEGER, texts INTEGER DEFAULT 0, media INTEGER DEFAULT 0,
    PRIMARY KEY(user_id, group_id)
);
""")


def migrate():
    """Обновляет базу старой версии (одна группа на человека) до текущей схемы."""
    if "active_group" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
        db.execute("ALTER TABLE users ADD COLUMN active_group INTEGER DEFAULT 0")
    pk = [r["name"] for r in db.execute("PRAGMA table_info(members)") if r["pk"]]
    if pk == ["user_id"]:              # старая схема: PRIMARY KEY только по user_id
        db.executescript("""
        ALTER TABLE members RENAME TO members_old;
        CREATE TABLE members(
            user_id INTEGER, group_id INTEGER, role TEXT DEFAULT 'member',
            muted_until INTEGER DEFAULT 0, joined INTEGER,
            PRIMARY KEY(user_id, group_id)
        );
        INSERT INTO members(user_id, group_id, role, muted_until, joined)
            SELECT user_id, group_id, role, muted_until, joined FROM members_old;
        DROP TABLE members_old;
        """)
    relay_cols = {r["name"] for r in db.execute("PRAGMA table_info(relay)")}
    if "src_chat_id" not in relay_cols:         # старая база: добавляем связку для нативных reply
        db.executescript("""
        ALTER TABLE relay ADD COLUMN src_chat_id INTEGER;
        ALTER TABLE relay ADD COLUMN src_msg_id INTEGER;
        UPDATE relay SET src_chat_id = chat_id, src_msg_id = msg_id WHERE src_chat_id IS NULL;
        """)
    db.executescript("""
    CREATE INDEX IF NOT EXISTS ix_members_group ON members(group_id);
    CREATE INDEX IF NOT EXISTS ix_relay_src ON relay(src_chat_id, src_msg_id);
    UPDATE users SET active_group = (SELECT group_id FROM members WHERE members.user_id = users.user_id)
     WHERE COALESCE(active_group, 0) = 0
       AND (SELECT COUNT(*) FROM members WHERE members.user_id = users.user_id) = 1;
    """)
    db.commit()


migrate()


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


def ensure_user(uid: int):
    run("INSERT OR IGNORE INTO users(user_id, created) VALUES(?,?)", (uid, now()))
    return one("SELECT * FROM users WHERE user_id=?", (uid,))


MEM_SQL = """SELECT m.user_id, m.group_id, m.role, m.muted_until,
                    g.title, g.protect, g.media, g.token
             FROM members m JOIN groups g ON g.id = m.group_id
             WHERE m.user_id=?"""


def get_member(uid: int, gid: Optional[int] = None):
    """Членство человека: в активной группе (gid не указан) или в конкретной группе gid."""
    if gid is not None:
        return one(MEM_SQL + " AND m.group_id=?", (uid, gid))
    u = one("SELECT active_group FROM users WHERE user_id=?", (uid,))
    mem = one(MEM_SQL + " AND m.group_id=?", (uid, u["active_group"])) if u and u["active_group"] else None
    if not mem:
        # активная группа не выбрана или пропала (вышли / исключили / удалили):
        # если осталась ровно одна группа — берём её, если несколько — человек выберет сам (/groups)
        rows = many("SELECT group_id FROM members WHERE user_id=?", (uid,))
        new = rows[0]["group_id"] if len(rows) == 1 else 0
        if u and u["active_group"] != new:
            run("UPDATE users SET active_group=? WHERE user_id=?", (new, uid))
        mem = one(MEM_SQL + " AND m.group_id=?", (uid, new)) if new else None
    return mem


def count_members(gid: int) -> int:
    return one("SELECT COUNT(*) AS c FROM members WHERE group_id=?", (gid,))["c"]


def count_groups(uid: int) -> int:
    return one("SELECT COUNT(*) AS c FROM members WHERE user_id=?", (uid,))["c"]


def new_token(gid: int) -> str:
    token = secrets.token_urlsafe(9)
    run("UPDATE groups SET token=? WHERE id=?", (token, gid))
    return token


def bump_stats(uid: int, gid: int, is_media: bool):
    """Учитывает одно отправленное сообщение — в личную и групповую статистику."""
    run("""INSERT INTO stats(user_id, group_id, texts, media) VALUES(?,?,?,?)
           ON CONFLICT(user_id, group_id) DO UPDATE SET
             texts = texts + excluded.texts, media = media + excluded.media""",
        (uid, gid, 0 if is_media else 1, 1 if is_media else 0))


def personal_stats(uid: int, gid: int):
    r = one("SELECT texts, media FROM stats WHERE user_id=? AND group_id=?", (uid, gid))
    return (r["texts"], r["media"]) if r else (0, 0)


def personal_stats_total(uid: int):
    r = one("SELECT COALESCE(SUM(texts),0) AS t, COALESCE(SUM(media),0) AS m FROM stats WHERE user_id=?", (uid,))
    return (r["t"], r["m"])


def group_stats(gid: int):
    r = one("SELECT COALESCE(SUM(texts),0) AS t, COALESCE(SUM(media),0) AS m FROM stats WHERE group_id=?", (gid,))
    return (r["t"], r["m"])


def drop_member(uid: int, gid: int) -> str:
    """Убирает человека из группы. Если группа была активной — выбирает новую активную.
    Возвращает пояснение для человека (пустое, если активная группа не менялась)."""
    was_active = one("SELECT 1 FROM users WHERE user_id=? AND active_group=?", (uid, gid)) is not None
    run("DELETE FROM members WHERE user_id=? AND group_id=?", (uid, gid))
    if not was_active:
        return ""
    mem = get_member(uid)              # заодно выберет новую активную группу
    if mem:
        return f"\n\n✍️ Теперь сообщения идут в «{esc(mem['title'])}»."
    if count_groups(uid):
        return "\n\n🗂 Выберите, куда писать: /groups"
    return "\n\nВы больше не состоите ни в одной группе. Создать новую: /newgroup"


def parse_title(raw: str):
    """Название группы: схлопывает пробелы и переводы строк. Возвращает (название, ошибка)."""
    title = " ".join((raw or "").split())
    if not title:
        return "", "❌ Название не может быть пустым."
    if len(title) > TITLE_MAX:
        return "", f"✂️ Слишком длинное название: максимум {TITLE_MAX} символов, у вас {len(title)}."
    return title, None


# ───────────────────────── Вспомогательное ─────────────────────────
log = logging.getLogger("anon-bot")
router = Router()
router.message.filter(F.chat.type == "private")
bot: Bot = None  # type: ignore  # создаётся в main()
BOT_USERNAME = ""


class DropState(BaseMiddleware):
    """Любая команда или кнопка меню отменяет ожидание ввода (ника / названия) — иначе следующее
    обычное сообщение ушло бы не в группу, а стало бы «названием»."""

    async def __call__(self, handler, m: Message, data: dict):
        t = m.text or ""
        if (m.chat.type == "private" and m.from_user
                and (t.startswith("/") or t in MENU_BUTTONS)
                and not t.lower().startswith("/cancel")):
            run("UPDATE users SET state='' WHERE user_id=? AND state!='' AND COALESCE(nick,'')!=''",
                (m.from_user.id,))
        return await handler(m, data)


router.message.outer_middleware(DropState())


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    mem = get_member(uid)
    hint = f"Пишу в «{mem['title']}»" if mem else "Написать в группу…"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=B_GROUP), KeyboardButton(text=B_GROUPS)],
            [KeyboardButton(text=B_MEMBERS), KeyboardButton(text=B_STATS)],
            [KeyboardButton(text=B_PANEL), KeyboardButton(text=B_NICK)],
            [KeyboardButton(text=B_HELP), KeyboardButton(text=B_ABOUT)],
        ],
        resize_keyboard=True,
        input_field_placeholder=hint[:64],
    )


def no_group_text(uid: int) -> str:
    if count_groups(uid):
        return "🗂 Сначала выберите группу: /groups"
    return ("Вы пока не в группе.\n• Создать: /newgroup\n"
            "• Или откройте ссылку-приглашение от владельца группы.")


def limit_text() -> str:
    return (f"🚫 Достигнут лимит: не больше {MAX_GROUPS} групп на человека. Переключитесь на ненужную "
            "группу (/groups) и выйдите из неё (/leave); владелец может удалить группу в /panel.")


def tag(uid: int, title: str) -> str:
    """Метка группы для тех, кто состоит в нескольких группах (иначе непонятно, о какой речь)."""
    return f"🗂 <b>{esc(title)}</b>\n" if count_groups(uid) > 1 else ""


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
    gid = mem["group_id"]      # id группы зашит в кнопки: они всегда относятся к «своей» группе
    kb = [[InlineKeyboardButton(text="🔗 Ссылка", callback_data=f"p:link:{gid}"),
           InlineKeyboardButton(text="♻️ Обновить ссылку", callback_data=f"p:newlink:{gid}")]]
    if mem["role"] == "owner":
        kb.append([InlineKeyboardButton(text="✏️ Сменить название", callback_data=f"p:rename:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🛡 Защита от пересылки: {'вкл' if mem['protect'] else 'выкл'}", callback_data=f"p:protect:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🖼 Медиа: {'разрешены' if mem['media'] else 'запрещены'}", callback_data=f"p:media:{gid}")])
        kb.append([InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"p:del:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def groups_text(uid: int) -> str:
    return (f"🗂 <b>Мои группы</b> ({count_groups(uid)}/{MAX_GROUPS})\n"
            "Сообщения уходят и приходят только в группе с отметкой ✅ — остальные молчат в фоне.\n"
            "Нажмите на другую, чтобы переключиться.\n"
            f"{ROLE_ICON['owner']} владелец · {ROLE_ICON['moderator']} модератор · {ROLE_ICON['member']} участник")


def groups_kb(uid: int) -> InlineKeyboardMarkup:
    cur = get_member(uid)
    cur_id = cur["group_id"] if cur else 0
    rows = many("""SELECT m.group_id, m.role, g.title FROM members m JOIN groups g ON g.id = m.group_id
                   WHERE m.user_id=? ORDER BY m.joined, m.group_id""", (uid,))
    kb = [[InlineKeyboardButton(
        text=f"{'✅' if r['group_id'] == cur_id else ROLE_ICON[r['role']]} {r['title']}",
        callback_data=f"g:{r['group_id']}")] for r in rows]
    if len(rows) < MAX_GROUPS:
        kb.append([InlineKeyboardButton(text="➕ Новая группа", callback_data="g:new")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def edit(c: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    """Правит сообщение с кнопками; если нельзя (устарело / ничего не изменилось) — молча пропускает."""
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except (TelegramAPIError, AttributeError):
        pass


async def notify(uid: int, text: str, kb: bool = False):
    """Личное служебное сообщение. kb=True — заодно обновить меню (подсказку «Пишу в …»)."""
    try:
        await bot.send_message(uid, text, reply_markup=main_kb(uid) if kb else None)
    except TelegramAPIError as e:
        log.info("не отправлено %s: %s", uid, e)


def active_recipients(gid: int, exclude=()) -> list:
    """Участники группы, у которых она сейчас активна. Так сообщения и уведомления из группы
    не «фонят» тем, кто сейчас переключён на другую группу — до тех пор, пока не вернутся в неё."""
    rows = many("""SELECT m.user_id FROM members m JOIN users u ON u.user_id = m.user_id
                   WHERE m.group_id=? AND u.active_group=?""", (gid, gid))
    return [r["user_id"] for r in rows if r["user_id"] not in exclude]


async def announce(gid: int, text: str, exclude=(), kb: bool = False):
    """Служебное сообщение тем, у кого эта группа сейчас активна (кто переключён на другую — не отвлекаем)."""
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    if not g:
        return
    for uid in active_recipients(gid, exclude=exclude):
        await notify(uid, tag(uid, g["title"]) + text, kb)
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


async def staff(m: Message, owner_only: bool = False, gid: Optional[int] = None):
    """Вернёт участника, если он владелец/модератор группы (активной или указанной gid)."""
    if not await reg(m):
        return None
    uid = m.from_user.id
    mem = get_member(uid, gid)
    if not mem:
        await m.answer("🚫 Вы уже не состоите в группе этого сообщения." if gid else no_group_text(uid))
        return None
    allowed = ("owner",) if owner_only else ("owner", "moderator")
    if mem["role"] not in allowed:
        await m.answer("🚫 Только для владельца группы." if owner_only
                       else "🚫 Только для владельца и модераторов группы.")
        return None
    return mem


def reply_group(m: Message) -> Optional[int]:
    """Если команда — ответ на пересланное ботом сообщение, вернёт группу, из которой оно пришло."""
    if m.reply_to_message:
        r = one("SELECT group_id FROM relay WHERE chat_id=? AND msg_id=?",
                (m.chat.id, m.reply_to_message.message_id))
        if r:
            return r["group_id"]
    return None


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
    """Общая проверка для /kick /mute /unmute /mod /unmod. Вернёт (я, цель, аргументы) или None.
    При ответе на сообщение действует в той группе, откуда оно пришло, — даже если она не активная."""
    mem = await staff(m, owner_only, reply_group(m))
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


async def deliver(m: Message, chat_id: int, nick: str, protect: bool, label: str = "",
                   reply_to: Optional[int] = None) -> list:
    """Отправляет одно сообщение одному получателю, возвращает id отправленных сообщений.
    label — название группы (подставляется тем, кто состоит в нескольких группах).
    reply_to — id сообщения в чате получателя, на которое нужно ответить нативным Telegram-reply
    (без текстовых вставок вида «в ответ на…»). Если это сообщение у получателя уже не существует
    (например, удалено), allow_sending_without_reply просто отправит как обычное сообщение."""
    head = f"<b>{esc(nick)}</b>" + (f" <i>· {esc(label)}</i>" if label else "")
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    if m.text:
        r = await bot.send_message(chat_id, f"{head}:\n{to_html(m)}", protect_content=protect,
                                   reply_parameters=rp)
        return [r.message_id]
    ctype = str(getattr(m.content_type, "value", m.content_type))
    cap = to_html(m)
    text = head + (f"\n{cap}" if cap else "")
    if ctype in CAPTION_TYPES and len(text) <= 1000:      # ник — в подписи к медиа
        r = await bot.copy_message(chat_id, m.chat.id, m.message_id, caption=text, protect_content=protect,
                                   reply_parameters=rp)
        return [r.message_id]
    h = await bot.send_message(chat_id, f"{head}:", protect_content=protect,
                               reply_parameters=rp)         # стикеры/кружки: ник отдельной строкой, реплай — на неё
    r = await bot.copy_message(chat_id, m.chat.id, m.message_id, protect_content=protect)
    return [h.message_id, r.message_id]


async def relay(m: Message, u, mem):
    """Рассылает сообщение только тем участникам группы, у кого она сейчас активна —
    остальные (переключённые на другую группу) сообщения из этой группы не получают.
    Если это ответ на ранее пересланное сообщение — у каждого получателя оно уходит тоже
    нативным Telegram-reply на его копию того же сообщения (без текстовых вставок вида
    «в ответ на…»)."""
    protect = bool(mem["protect"])
    gid = mem["group_id"]
    src_chat_id, src_msg_id = m.chat.id, m.message_id

    reply_src = None
    if m.reply_to_message:
        r = one("SELECT src_chat_id, src_msg_id FROM relay WHERE chat_id=? AND msg_id=?",
                (m.chat.id, m.reply_to_message.message_id))
        if r:
            reply_src = (r["src_chat_id"], r["src_msg_id"])

    # «корень»: запоминаем само это сообщение, чтобы дальше на него можно было ответить
    # (в т.ч. самому отправителю — на своё же сообщение в своём чате)
    db.execute("INSERT OR REPLACE INTO relay VALUES(?,?,?,?,?,?,?)",
               (src_chat_id, src_msg_id, u["user_id"], gid, now(), src_chat_id, src_msg_id))

    for rid in active_recipients(gid, exclude=(u["user_id"],)):
        reply_to = None
        if reply_src:
            rr = one("""SELECT msg_id FROM relay WHERE chat_id=? AND src_chat_id=? AND src_msg_id=?
                        ORDER BY msg_id DESC LIMIT 1""", (rid, reply_src[0], reply_src[1]))
            reply_to = rr["msg_id"] if rr else None
        try:
            for mid in await deliver(m, rid, u["nick"], protect, reply_to=reply_to):
                db.execute("INSERT OR REPLACE INTO relay VALUES(?,?,?,?,?,?,?)",
                           (rid, mid, u["user_id"], gid, now(), src_chat_id, src_msg_id))
        except TelegramAPIError as e:
            log.warning("не доставлено %s: %s", rid, e)
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
        await m.answer(f"✅ Ник изменён: <b>{esc(nick)}</b>", reply_markup=main_kb(uid))
        for r in many("SELECT group_id FROM members WHERE user_id=?", (uid,)):   # ник общий для всех групп
            await announce(r["group_id"], f"✏️ <i>{esc(old)} теперь {esc(nick)}</i>", exclude=(uid,))
        return
    await m.answer(f"✅ Ник <b>{esc(nick)}</b> сохранён!\n\n"
                   "Создайте группу: /newgroup\n"
                   "или откройте ссылку-приглашение от владельца группы.\n"
                   "Меню — внизу 👇", reply_markup=main_kb(uid))
    if token:
        await join_group(m, token)


async def join_group(m: Message, token: str):
    uid = m.from_user.id
    u = ensure_user(uid)
    g = one("SELECT * FROM groups WHERE token=?", (token,))
    if not g:
        await m.answer("❌ Ссылка недействительна или устарела — попросите у владельца новую.",
                       reply_markup=main_kb(uid))
        return
    if get_member(uid, g["id"]):
        await m.answer(f"Вы уже в группе «{esc(g['title'])}». Переключиться на неё — /groups",
                       reply_markup=main_kb(uid))
        return
    if count_groups(uid) >= MAX_GROUPS:
        await m.answer(limit_text(), reply_markup=main_kb(uid))
        return
    run("INSERT INTO members(user_id, group_id, role, joined) VALUES(?,?,?,?)",
        (uid, g["id"], "member", now()))
    run("UPDATE users SET active_group=? WHERE user_id=?", (g["id"], uid))
    extra = "\nЭта группа теперь активна, переключаться между группами — /groups." if count_groups(uid) > 1 else ""
    await m.answer(f"✅ Вы в группе «{esc(g['title'])}».\n"
                   f"Пишите сюда — сообщения увидят все участники под ником <b>{esc(u['nick'])}</b>." + extra,
                   reply_markup=main_kb(uid))
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
    if mem:
        where = f"Сообщения идут в группу «{esc(mem['title'])}». Другие группы — /groups"
    elif count_groups(u["user_id"]):
        where = "Выберите, куда писать: /groups"
    else:
        where = "Создайте группу: /newgroup или откройте ссылку-приглашение."
    await m.answer(f"👋 Привет, <b>{esc(u['nick'])}</b>!\n{where}", reply_markup=main_kb(u["user_id"]))


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


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    u = ensure_user(m.from_user.id)
    if not u["nick"]:                  # без ника дальше никак
        run("UPDATE users SET state='nick' WHERE user_id=?", (u["user_id"],))
        await m.answer("✏️ Сначала придумайте ник — отправьте его сообщением.")
    elif u["state"]:
        run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
        await m.answer("Отменено.", reply_markup=main_kb(u["user_id"]))
    else:
        await m.answer("Отменять нечего.")


# ───────────────────────── Группы ─────────────────────────
async def begin_newgroup(uid: int, say):
    """Первый шаг создания группы: проверяем лимит и просим прислать название."""
    if count_groups(uid) >= MAX_GROUPS:
        await say(limit_text())
        return
    run("UPDATE users SET state='newgroup' WHERE user_id=?", (uid,))
    await say(f"📝 Напишите название группы одним сообщением (до {TITLE_MAX} символов).\nОтмена — /cancel")


async def create_group(m: Message, u, raw_title: str):
    uid = u["user_id"]
    if count_groups(uid) >= MAX_GROUPS:
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer(limit_text())
        return
    title, err = parse_title(raw_title)
    if err:                            # остаёмся в режиме ввода — можно прислать другое название
        run("UPDATE users SET state='newgroup' WHERE user_id=?", (uid,))
        await m.answer(f"{err}\nНапишите название ещё раз или /cancel.")
        return
    token = secrets.token_urlsafe(9)
    gid = run("INSERT INTO groups(title, owner_id, token, created) VALUES(?,?,?,?)",
              (title, uid, token, now())).lastrowid
    run("INSERT INTO members(user_id, group_id, role, joined) VALUES(?,?,'owner',?)", (uid, gid, now()))
    run("UPDATE users SET active_group=?, state='' WHERE user_id=?", (gid, uid))
    await m.answer(f"🎉 Группа «{esc(title)}» создана — сообщения теперь идут в неё.\n\n"
                   f"{link_text(token, title)}\n\n"
                   "Настройки — /panel, команды — /help", reply_markup=main_kb(uid))


@router.message(Command("newgroup"))
async def cmd_newgroup(m: Message, command: CommandObject):
    u = await reg(m)
    if not u:
        return
    if (command.args or "").strip():
        await create_group(m, u, command.args)         # можно и сразу: /newgroup Название
    else:
        await begin_newgroup(u["user_id"], m.answer)   # или в два шага: /newgroup → название


@router.message(Command("groups"))
@router.message(F.text == B_GROUPS)
async def cmd_groups(m: Message):
    u = await reg(m)
    if not u:
        return
    if not count_groups(u["user_id"]):
        await m.answer(no_group_text(u["user_id"]))
        return
    await m.answer(groups_text(u["user_id"]), reply_markup=groups_kb(u["user_id"]))


@router.callback_query(F.data.startswith("g:"))
async def groups_cb(c: CallbackQuery):
    uid = c.from_user.id
    arg = c.data[2:]
    if arg == "new":
        await begin_newgroup(uid, lambda t: bot.send_message(uid, t))
        await c.answer()
        return
    try:
        gid = int(arg)
    except ValueError:
        await c.answer("Кнопка устарела — откройте /groups заново", show_alert=True)
        return
    mem = get_member(uid, gid)
    if not mem:
        await c.answer("Вы уже не в этой группе", show_alert=True)
        await edit(c, groups_text(uid), groups_kb(uid))
        return
    cur = get_member(uid)
    if cur and cur["group_id"] == gid:
        await c.answer("Эта группа уже активна")
        return
    run("UPDATE users SET active_group=? WHERE user_id=?", (gid, uid))
    await c.answer("Переключено")
    await edit(c, groups_text(uid), groups_kb(uid))
    await bot.send_message(uid, f"✍️ Теперь сообщения идут в «{esc(mem['title'])}».", reply_markup=main_kb(uid))


@router.message(Command("group"))
@router.message(F.text == B_GROUP)
async def cmd_group(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer(no_group_text(u["user_id"]))
        return
    await m.answer(
        f"👥 <b>{esc(mem['title'])}</b>\n"
        f"Ваш ник: <b>{esc(u['nick'])}</b> · роль: {ROLE_NAME[mem['role']]}\n"
        f"Участников: {count_members(mem['group_id'])}\n"
        f"Защита от пересылки: {'вкл' if mem['protect'] else 'выкл'}\n"
        f"Медиа: {'разрешены' if mem['media'] else 'запрещены'}\n"
        f"Ваших групп: {count_groups(u['user_id'])} из {MAX_GROUPS} — переключение: /groups")


@router.message(Command("members"))
@router.message(F.text == B_MEMBERS)
async def cmd_members(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer(no_group_text(u["user_id"]))
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


@router.message(Command("stats"))
@router.message(F.text == B_STATS)
async def cmd_stats(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer(no_group_text(u["user_id"]))
        return
    p_text, p_media = personal_stats(u["user_id"], mem["group_id"])
    g_text, g_media = group_stats(mem["group_id"])
    lines = [
        f"📊 <b>Статистика «{esc(mem['title'])}»</b>",
        "",
        "👤 <b>Вы в этой группе</b>",
        f"Текст: {p_text} · Медиа: {p_media}",
        "",
        "👥 <b>Вся группа</b>",
        f"Текст: {g_text} · Медиа: {g_media}",
    ]
    if count_groups(u["user_id"]) > 1:
        t_text, t_media = personal_stats_total(u["user_id"])
        lines += ["", "🌐 <b>Вы во всех группах</b>", f"Текст: {t_text} · Медиа: {t_media}"]
    await m.answer("\n".join(lines))


@router.message(Command("leave"))
async def cmd_leave(m: Message):
    u = await reg(m)
    if not u:
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer(no_group_text(u["user_id"]))
        return
    if mem["role"] == "owner":
        await m.answer("👑 Владелец не может выйти. Удалите группу в /panel.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выйти", callback_data=f"l:yes:{mem['group_id']}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="l:no")]])
    await m.answer(f"Выйти из группы «{esc(mem['title'])}»? Вернуться можно будет только по ссылке-приглашению.",
                   reply_markup=kb)


@router.callback_query(F.data.startswith("l:"))
async def leave_cb(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":")
    if parts[1] == "no":
        await edit(c, "Отменено.")
        await c.answer()
        return
    try:
        gid = int(parts[2])
    except (IndexError, ValueError):
        await c.answer("Кнопка устарела — вызовите /leave заново", show_alert=True)
        return
    mem = get_member(uid, gid)
    if not mem:
        await c.answer("Вы уже не в этой группе", show_alert=True)
        return
    if mem["role"] == "owner":
        await c.answer("Владелец не может выйти — удалите группу в /panel", show_alert=True)
        return
    nick = ensure_user(uid)["nick"]
    note = drop_member(uid, gid)
    await edit(c, f"👋 Вы вышли из группы «{esc(mem['title'])}».")
    if note:
        await notify(uid, note.strip(), kb=True)
    await announce(gid, f"👋 <i>Участник {esc(nick)} вышел из группы</i>")
    await c.answer()


# ───────────────────────── Название группы ─────────────────────────
async def finish_rename(m: Message, u, gid: int, raw_title: str):
    uid = u["user_id"]
    mem = get_member(uid, gid)
    if not mem or mem["role"] != "owner":
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer("🚫 Переименовать группу может только её владелец.")
        return
    title, err = parse_title(raw_title)
    if err:                            # остаёмся в режиме ввода — можно прислать другое название
        run("UPDATE users SET state=? WHERE user_id=?", (f"rename:{gid}", uid))
        await m.answer(f"{err}\nНапишите название ещё раз или /cancel.")
        return
    run("UPDATE users SET state='' WHERE user_id=?", (uid,))
    if title == mem["title"]:
        await m.answer("Это и так текущее название.", reply_markup=main_kb(uid))
        return
    run("UPDATE groups SET title=? WHERE id=?", (title, gid))
    await m.answer(f"✅ Группа теперь называется «{esc(title)}».", reply_markup=main_kb(uid))
    await announce(gid, f"✏️ <i>Группа переименована: «{esc(mem['title'])}» → «{esc(title)}»</i>",
                   exclude=(uid,), kb=True)


@router.message(Command("rename"))
async def cmd_rename(m: Message, command: CommandObject):
    mem = await staff(m, owner_only=True)
    if not mem:
        return
    uid = m.from_user.id
    if (command.args or "").strip():
        await finish_rename(m, ensure_user(uid), mem["group_id"], command.args)   # /rename Новое название
        return
    run("UPDATE users SET state=? WHERE user_id=?", (f"rename:{mem['group_id']}", uid))
    await m.answer(f"✏️ Напишите новое название группы «{esc(mem['title'])}» одним сообщением "
                   f"(до {TITLE_MAX} символов).\nОтмена — /cancel")


# ───────────────────────── Модерация ─────────────────────────
@router.message(Command("kick"))
async def cmd_kick(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command)
    if not ctx:
        return
    mem, t, _ = ctx
    note = drop_member(t["user_id"], mem["group_id"])
    await m.answer(f"🚪 {esc(t['nick'])} исключён из группы «{esc(mem['title'])}».")
    await notify(t["user_id"], f"🚪 Вас исключили из группы «{esc(mem['title'])}». "
                               "Вернуться можно только по действующей ссылке-приглашению." + note, kb=bool(note))
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
    run("UPDATE members SET muted_until=? WHERE user_id=? AND group_id=?",
        (now() + minutes * 60, t["user_id"], mem["group_id"]))
    title = esc(mem["title"])
    await m.answer(f"🔇 {esc(t['nick'])} в муте на {minutes} мин. (группа «{title}»)")
    await notify(t["user_id"], f"🔇 В группе «{title}» вы в муте на {minutes} мин.: читать можно, писать нельзя.")
    await announce(mem["group_id"], f"🔇 <i>Участник {esc(t['nick'])} в муте на {minutes} мин.</i>",
                   exclude=(t["user_id"], mem["user_id"]))


@router.message(Command("unmute"))
async def cmd_unmute(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command)
    if not ctx:
        return
    mem, t, _ = ctx
    run("UPDATE members SET muted_until=0 WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
    title = esc(mem["title"])
    await m.answer(f"🔊 Мут с {esc(t['nick'])} снят (группа «{title}»).")
    await notify(t["user_id"], f"🔊 В группе «{title}» мут снят — можно писать.")


@router.message(Command("mod"))
async def cmd_mod(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command, owner_only=True)
    if not ctx:
        return
    mem, t, _ = ctx
    if t["role"] == "moderator":
        await m.answer("Он уже модератор.")
        return
    run("UPDATE members SET role='moderator' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
    title = esc(mem["title"])
    await m.answer(f"🛡 {esc(t['nick'])} теперь модератор группы «{title}».")
    await notify(t["user_id"], f"🛡 Вас назначили модератором группы «{title}». Команды — /help, панель — /panel.")


@router.message(Command("unmod"))
async def cmd_unmod(m: Message, command: CommandObject):
    ctx = await mod_ctx(m, command, owner_only=True)
    if not ctx:
        return
    mem, t, _ = ctx
    if t["role"] != "moderator":
        await m.answer("Он не модератор.")
        return
    run("UPDATE members SET role='member' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
    title = esc(mem["title"])
    await m.answer(f"{esc(t['nick'])} больше не модератор группы «{title}».")
    await notify(t["user_id"], f"С вас сняли права модератора в группе «{title}».")


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


@router.callback_query(F.data.startswith("p:"))
async def panel_cb(c: CallbackQuery):
    uid = c.from_user.id
    try:
        _, act, g = c.data.split(":")
        gid = int(g)
    except ValueError:
        await c.answer("Кнопка устарела — откройте /panel заново", show_alert=True)
        return
    mem = get_member(uid, gid)         # группа берётся из кнопки, а не из «активной»
    if not mem or mem["role"] not in ("owner", "moderator"):
        await c.answer("Нет доступа", show_alert=True)
        return
    owner = mem["role"] == "owner"

    if act == "link":
        await c.message.answer(link_text(mem["token"], mem["title"]))
    elif act == "newlink":
        token = new_token(gid)
        await c.message.answer("♻️ Ссылка обновлена, старая больше не работает.\n\n"
                               + link_text(token, mem["title"]))
    elif act == "cancel":
        await edit(c, panel_text(mem), panel_kb(mem))
    elif owner and act == "rename":
        run("UPDATE users SET state=? WHERE user_id=?", (f"rename:{gid}", uid))
        await bot.send_message(uid, f"✏️ Напишите новое название группы «{esc(mem['title'])}» одним сообщением "
                                    f"(до {TITLE_MAX} символов).\nОтмена — /cancel")
    elif owner and act in ("protect", "media"):
        run(f"UPDATE groups SET {act}=1-{act} WHERE id=?", (gid,))
        mem = get_member(uid, gid)
        on = bool(mem[act])
        if act == "protect":
            text = ("🛡 Защита включена: сообщения нельзя пересылать и сохранять." if on
                    else "🛡 Защита выключена: сообщения можно пересылать.")
        else:
            text = "🖼 Медиа разрешены." if on else "🖼 Медиа запрещены — только текст."
        await announce(gid, f"<i>{text}</i>", exclude=(uid,))
        await edit(c, panel_text(mem), panel_kb(mem))
    elif owner and act == "del":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"p:delyes:{gid}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"p:cancel:{gid}")]])
        await edit(c, f"🗑 Удалить группу «{esc(mem['title'])}»? "
                      "Все участники будут исключены. Это необратимо.", kb)
    elif owner and act == "delyes":
        ids = [r["user_id"] for r in many("SELECT user_id FROM members WHERE group_id=?", (gid,))]
        notes = {i: drop_member(i, gid) for i in ids}
        run("DELETE FROM relay WHERE group_id=?", (gid,))
        run("DELETE FROM groups WHERE id=?", (gid,))
        await edit(c, "🗑 Группа удалена.")
        if notes.get(uid):
            await notify(uid, notes[uid].strip(), kb=True)
        for i in ids:
            if i != uid:
                await notify(i, f"🗑 Группа «{esc(mem['title'])}» удалена владельцем." + notes[i], kb=bool(notes[i]))
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


# ───────────────────────── Ввод ника / названия и обычные сообщения ─────────────────────────
async def has_state(m: Message) -> bool:
    r = one("SELECT state FROM users WHERE user_id=?", (m.from_user.id,))
    return bool(r and r["state"])


@router.message(F.text, has_state)
async def on_input_text(m: Message):
    u = ensure_user(m.from_user.id)
    st = u["state"]
    if st == "nick":
        await finish_nick(m, u, m.text)
    elif st == "newgroup":
        await create_group(m, u, m.text)
    elif st.startswith("rename:"):
        await finish_rename(m, u, int(st[7:]), m.text)


@router.message(F.text.startswith("/"))
async def unknown_command(m: Message):
    await m.answer("🤷 Неизвестная команда. Список — /help")


@router.message()
async def on_message(m: Message):
    u = await reg(m)
    if not u:
        return
    if u["state"]:                     # ждём текст (ник / название), а прислали не текст
        what = "ник" if u["state"] == "nick" else "название группы"
        await m.answer(f"✍️ Сейчас я жду {what} текстом. Отмена — /cancel")
        return
    mem = get_member(u["user_id"])
    if not mem:
        await m.answer(no_group_text(u["user_id"]))
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
    elif not mem["media"]:
        await m.answer("🖼 В этой группе медиа запрещены — только текст.")
        return
    bump_stats(u["user_id"], mem["group_id"], is_media=not bool(m.text))
    await relay(m, u, mem)


# ───────────────────────── Запуск ─────────────────────────
async def cleanup_loop():
    while True:
        run("DELETE FROM relay WHERE ts<?", (now() - RELAY_TTL,))
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
