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
В группе не может быть больше MAX_MEMBERS участников. Группы можно делать публичными —
такие видны всем в каталоге /catalog и через /search_group. Роли: владелец → администратор
→ модератор → участник; администратор обладает всеми правами владельца, кроме удаления группы.
База старой версии (одна группа на человека / без каталога и ролей) обновляется автоматически
при запуске.
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
MAX_MEMBERS = 50                           # максимум участников в одной группе
TITLE_MAX = 40                             # максимальная длина названия группы
DESC_MAX = 200                             # максимальная длина описания группы
CATALOG_PAGE_SIZE = 5                      # групп на страницу каталога
RELAY_TTL = 3 * 24 * 3600                  # сколько хранить связку «сообщение → автор» (для модерации ответом)
DEFAULT_MUTE_MIN = 10
MAX_MUTE_MIN = 7 * 24 * 60
MAX_TEXT_LEN = 3500

# ── жалобы (/report) ──
REPORT_MIN_DIALOG_SEC = 60             # нельзя жаловаться на сообщение младше 1 минуты
REPORT_SAME_TARGET_COOLDOWN = 3600     # на одного и того же не чаще раза в час
REPORT_MAX_PER_DAY = 3                 # максимум жалоб от одного человека в сутки
REPORT_WINDOW_SEC = 24 * 3600          # окно подсчёта жалоб для бана
REPORT_TTL = 7 * 24 * 3600             # сколько хранить жалобы
BAN_THRESHOLD_SHORT, BAN_SHORT_SEC = 5, 3600           # 5–9 жалоб → бан на 1 час
BAN_THRESHOLD_LONG, BAN_LONG_SEC = 10, 24 * 3600        # 10+ жалоб → бан на 24 часа
BAN_MESSAGE = "🚫 Вы забанены за спам/оскорбления."

NICK_RE = re.compile(r"[\w-]{3,20}")
# Простая проверка «эмодзи» для названий групп: основные эмодзи-блоки + вариационный селектор.
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2300-\u23FF\u2B00-\u2BFF\uFE0F]+"
)
CAPTION_TYPES = ("photo", "video", "document", "audio", "voice", "animation")
ALLOWED_TYPES = CAPTION_TYPES + ("text", "sticker", "video_note")
ROLE_ICON = {"owner": "👑", "admin": "⭐", "moderator": "🛡", "member": "•"}
ROLE_NAME = {"owner": "владелец", "admin": "администратор", "moderator": "модератор", "member": "участник"}
ROLE_LEVEL = {"owner": 3, "admin": 2, "moderator": 1, "member": 0}

B_GROUP, B_GROUPS, B_MEMBERS = "👥 Группа", "🗂 Мои группы", "📋 Участники"
B_NICK, B_PANEL = "✏️ Сменить ник", "⚙️ Управление"
B_STATS = "📊 Статистика"
B_CATALOG = "📂 Каталог"
B_HELP, B_ABOUT = "❓ Помощь", "ℹ️ О боте"
MENU_BUTTONS = {B_GROUP, B_GROUPS, B_MEMBERS, B_NICK, B_PANEL, B_STATS, B_CATALOG, B_HELP, B_ABOUT}

COMMANDS = [
    ("start", "Начало / регистрация"),
    ("newgroup", "Создать группу"),
    ("groups", "Мои группы и переключение"),
    ("group", "Активная группа"),
    ("members", "Участники"),
    ("stats", "Статистика (личная и по группе)"),
    ("catalog", "Каталог публичных групп"),
    ("search_group", "Поиск группы по названию/описанию"),
    ("nick", "Сменить ник"),
    ("leave", "Выйти из группы"),
    ("cancel", "Отменить ввод"),
    ("report", "Пожаловаться на сообщение (ответом)"),
    ("panel", "Управление группой (модератор)"),
    ("rename", "Сменить название группы (администратор)"),
    ("description", "Сменить описание группы (администратор)"),
    ("kick", "Исключить (модератор)"),
    ("mute", "Заглушить (модератор)"),
    ("unmute", "Снять мут (модератор)"),
    ("link", "Ссылка-приглашение (модератор)"),
    ("newlink", "Обновить ссылку (модератор)"),
    ("adm", "Назначить администратора / модератора"),
    ("unadm", "Снять администратора / модератора"),
    ("close_group", "Закрыть вход в группу (администратор)"),
    ("open_group", "Открыть вход в группу (администратор)"),
    ("mod", "Назначить модератора (владелец/администратор)"),
    ("unmod", "Снять модератора (владелец/администратор)"),
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
/catalog — каталог публичных групп
/search_group название — поиск группы по названию или описанию
/nick — сменить ник
/leave — выйти из активной группы
/cancel — отменить ввод
/report — свайпните на сообщение нарушителя и отправьте эту команду

Состоять можно максимум в {MAX_GROUPS} группах, участников в одной группе — максимум {MAX_MEMBERS}.

<b>Модераторы</b> (ответьте командой на сообщение или укажите ник)
/kick ник — исключить
/mute ник 30 — заглушить на 30 минут
/unmute ник — снять мут
/link — ссылка-приглашение
/newlink — обновить ссылку (старая перестанет работать)
/panel — панель управления

<b>Администраторы и владелец</b> (те же права, что у владельца, кроме удаления группы)
/adm ник — владелец назначает администратора; администратор той же командой назначает модератора
/unadm ник — соответствующее снятие прав
/rename — сменить название группы
/description — сменить описание группы (видно в каталоге)
/close_group — закрыть вход (новые участники не смогут войти)
/open_group — снова открыть вход
/panel — публичность, защита от пересылки, медиа, название, описание, вход, удаление группы

<b>Только владелец</b>
/panel → «Удалить группу»"""

ABOUT_TEXT = f"""ℹ️ <b>О боте</b>

Бот анонимных групп. Вас видят только под ником — ваш Telegram-аккаунт скрыт от всех, включая владельца и модераторов группы.

• Вход по ссылке-приглашению или через каталог публичных групп (/catalog)
• До {MAX_GROUPS} групп на один аккаунт, между ними можно переключаться (/groups)
• До {MAX_MEMBERS} участников в одной группе
• Сообщения приходят только из активной группы — остальные группы «молчат» в фоне, пока вы на них не переключитесь
• Защита от пересылки и сохранения — настройка группы
• Контакты, геопозиция и опросы не передаются, чтобы вас не раскрыть
• Правка и удаление сообщений у других участников не синхронизируются"""

# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    nick TEXT, nick_lc TEXT UNIQUE,
    state TEXT DEFAULT '',            -- '' | 'nick' | 'newgroup' | 'rename:<id>' | 'desc:<id>'
    pending TEXT DEFAULT '',          -- токен приглашения, ждущий регистрации
    active_group INTEGER DEFAULT 0,   -- группа, в которую уходят сообщения
    banned_until INTEGER DEFAULT 0,   -- до какого времени (unix) человек в бане за жалобы
    created INTEGER
);
CREATE TABLE IF NOT EXISTS groups(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, owner_id INTEGER, token TEXT UNIQUE,
    protect INTEGER DEFAULT 1,        -- запрет пересылки и сохранения
    media INTEGER DEFAULT 1,          -- разрешены ли медиа
    is_public INTEGER DEFAULT 0,      -- видна ли группа в каталоге /catalog
    is_closed INTEGER DEFAULT 0,      -- закрыт ли вход новым участникам
    description TEXT DEFAULT '',      -- описание для каталога
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
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER, offender_id INTEGER, group_id INTEGER, date INTEGER
);
CREATE INDEX IF NOT EXISTS ix_reports_offender ON reports(offender_id);
CREATE INDEX IF NOT EXISTS ix_reports_date ON reports(date);
""")


def migrate():
    """Обновляет базу старой версии до текущей схемы. Таблица groups никогда не удаляется —
    существующие группы и их участники сохраняются."""
    ucols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "active_group" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN active_group INTEGER DEFAULT 0")
    if "banned_until" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN banned_until INTEGER DEFAULT 0")

    gcols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "is_public" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN is_public INTEGER DEFAULT 0")
    if "is_closed" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN is_closed INTEGER DEFAULT 0")
    if "description" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN description TEXT DEFAULT ''")

    db.executescript("""
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER, offender_id INTEGER, group_id INTEGER, date INTEGER
    );
    CREATE INDEX IF NOT EXISTS ix_reports_offender ON reports(offender_id);
    CREATE INDEX IF NOT EXISTS ix_reports_date ON reports(date);
    """)
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
    CREATE INDEX IF NOT EXISTS ix_groups_public ON groups(is_public);
    CREATE INDEX IF NOT EXISTS ix_groups_closed ON groups(is_closed);
    CREATE INDEX IF NOT EXISTS ix_groups_title ON groups(title);
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
                    g.title, g.protect, g.media, g.token,
                    g.is_public, g.is_closed, g.description
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


def is_banned(u) -> bool:
    return bool(u["banned_until"]) and u["banned_until"] > now()


def ban_user(uid: int, seconds: int):
    """Продлевает бан минимум до now()+seconds (никогда не сокращает уже действующий бан)."""
    until = now() + seconds
    run("UPDATE users SET banned_until=MAX(banned_until, ?) WHERE user_id=?", (until, uid))


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
    """Название группы: схлопывает пробелы и переводы строк, без эмодзи. Возвращает (название, ошибка)."""
    title = " ".join((raw or "").split())
    if not title:
        return "", "❌ Название не может быть пустым."
    if EMOJI_RE.search(title):
        return "", "🚫 В названии группы нельзя использовать эмодзи."
    if len(title) > TITLE_MAX:
        return "", f"✂️ Слишком длинное название: максимум {TITLE_MAX} символов, у вас {len(title)}."
    return title, None


def parse_desc(raw: str):
    """Описание группы (для каталога): схлопывает пробелы, может быть пустым. Возвращает (текст, ошибка)."""
    desc = " ".join((raw or "").split())
    if len(desc) > DESC_MAX:
        return "", f"✂️ Слишком длинное описание: максимум {DESC_MAX} символов, у вас {len(desc)}."
    return desc, None


def group_locked(g) -> bool:
    """Верно, если в группу больше нельзя войти: заполнена или вход закрыт владельцем/администратором."""
    return bool(g["is_closed"]) or count_members(g["id"]) >= MAX_MEMBERS


# ───────────────────────── Вспомогательное ─────────────────────────
log = logging.getLogger("anon-bot")
router = Router()
router.message.filter(F.chat.type == "private")
bot: Bot = None  # type: ignore  # создаётся в main()
BOT_USERNAME = ""


class DropState(BaseMiddleware):
    """Любая команда или кнопка меню отменяет ожидание ввода (ника / названия / описания) — иначе
    следующее обычное сообщение ушло бы не в группу, а стало бы этим вводом."""

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
            [KeyboardButton(text=B_CATALOG), KeyboardButton(text=B_PANEL)],
            [KeyboardButton(text=B_NICK), KeyboardButton(text=B_HELP)],
            [KeyboardButton(text=B_ABOUT)],
        ],
        resize_keyboard=True,
        input_field_placeholder=hint[:64],
    )


def no_group_text(uid: int) -> str:
    if count_groups(uid):
        return "🗂 Сначала выберите группу: /groups"
    return ("Вы пока не в группе.\n• Создать: /newgroup\n"
            "• Посмотреть каталог: /catalog\n"
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
    desc = f"\nОписание: {esc(mem['description'])}" if mem["description"] else ""
    return (f"⚙️ <b>Управление группой «{esc(mem['title'])}»</b>\n"
            f"Участников: {count_members(mem['group_id'])}/{MAX_MEMBERS}\n"
            f"В каталоге: {'да' if mem['is_public'] else 'нет'} · "
            f"Вход: {'закрыт' if mem['is_closed'] else 'открыт'}"
            f"{desc}\n\n"
            "🛡 Защита — сообщения нельзя пересылать и сохранять\n"
            "🖼 Медиа — можно ли слать фото, видео и файлы")


def panel_kb(mem) -> InlineKeyboardMarkup:
    gid = mem["group_id"]      # id группы зашит в кнопки: они всегда относятся к «своей» группе
    kb = [[InlineKeyboardButton(text="🔗 Ссылка", callback_data=f"p:link:{gid}"),
           InlineKeyboardButton(text="♻️ Обновить ссылку", callback_data=f"p:newlink:{gid}")]]
    if ROLE_LEVEL[mem["role"]] >= 2:       # владелец или администратор
        kb.append([InlineKeyboardButton(text="✏️ Название", callback_data=f"p:rename:{gid}"),
                   InlineKeyboardButton(text="📝 Описание", callback_data=f"p:desc:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🛡 Защита от пересылки: {'вкл' if mem['protect'] else 'выкл'}", callback_data=f"p:protect:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🖼 Медиа: {'разрешены' if mem['media'] else 'запрещены'}", callback_data=f"p:media:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🌐 В каталоге: {'да' if mem['is_public'] else 'нет'}", callback_data=f"p:public:{gid}")])
        kb.append([InlineKeyboardButton(
            text=f"🚪 Вход: {'закрыт' if mem['is_closed'] else 'открыт'}", callback_data=f"p:closed:{gid}")])
    if mem["role"] == "owner":
        kb.append([InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"p:del:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def groups_text(uid: int) -> str:
    return (f"🗂 <b>Мои группы</b> ({count_groups(uid)}/{MAX_GROUPS})\n"
            "Сообщения уходят и приходят только в группе с отметкой ✅ — остальные молчат в фоне.\n"
            "Нажмите на другую, чтобы переключиться.\n"
            f"{ROLE_ICON['owner']} владелец · {ROLE_ICON['admin']} администратор · "
            f"{ROLE_ICON['moderator']} модератор · {ROLE_ICON['member']} участник")


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


# ───────────────────────── Каталог публичных групп ─────────────────────────
def catalog_count(q: str = "") -> int:
    if q:
        like = f"%{q}%"
        return one("SELECT COUNT(*) AS c FROM groups WHERE is_public=1 AND (title LIKE ? OR description LIKE ?)",
                   (like, like))["c"]
    return one("SELECT COUNT(*) AS c FROM groups WHERE is_public=1")["c"]


def catalog_page(offset: int, q: str = ""):
    if q:
        like = f"%{q}%"
        return many("""SELECT id, title, description, is_closed FROM groups
                       WHERE is_public=1 AND (title LIKE ? OR description LIKE ?)
                       ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?""",
                    (like, like, CATALOG_PAGE_SIZE, offset))
    return many("""SELECT id, title, description, is_closed FROM groups WHERE is_public=1
                   ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?""", (CATALOG_PAGE_SIZE, offset))


def catalog_text(rows, offset: int, total: int, header: str = "📂 <b>Каталог групп</b>") -> str:
    if not total:
        return "📂 Публичных групп пока нет." if "Каталог" in header else "🔎 Ничего не найдено."
    lines = [f"{header} ({total})", ""]
    for r in rows:
        cnt = count_members(r["id"])
        desc = esc(r["description"]) if r["description"] else "без описания"
        note = " · вход закрыт" if r["is_closed"] or cnt >= MAX_MEMBERS else ""
        lines.append(f"<b>{esc(r['title'])}</b> ({cnt}/{MAX_MEMBERS}){note}\n{desc}")
    return "\n\n".join(lines)


def catalog_kb(rows, offset: int, total: int, prefix: str = "cat") -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    kb = [[InlineKeyboardButton(text=f"Войти: {r['title'][:24]}", callback_data=f"cj:{r['id']}")] for r in rows]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{prefix}:{max(0, offset - CATALOG_PAGE_SIZE)}"))
    if offset + CATALOG_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Дальше ▶️", callback_data=f"{prefix}:{offset + CATALOG_PAGE_SIZE}"))
    if nav:
        kb.append(nav)
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
    """Вернёт участника, если он модератор+ (или владелец/администратор при owner_only) группы
    (активной или указанной gid). owner_only=True требует роль владелец или администратор —
    администратор обладает всеми правами владельца, кроме удаления группы."""
    if not await reg(m):
        return None
    uid = m.from_user.id
    mem = get_member(uid, gid)
    if not mem:
        await m.answer("🚫 Вы уже не состоите в группе этого сообщения." if gid else no_group_text(uid))
        return None
    min_level = 2 if owner_only else 1
    if ROLE_LEVEL[mem["role"]] < min_level:
        await m.answer("🚫 Только для владельца и администраторов группы." if owner_only
                       else "🚫 Только для владельца, администраторов и модераторов группы.")
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
    При ответе на сообщение действует в той группе, откуда оно пришло, — даже если она не активная.
    Нельзя действовать против участника с такой же или более высокой ролью (кроме владельца)."""
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
    elif ROLE_LEVEL[t["role"]] >= ROLE_LEVEL[mem["role"]] and mem["role"] != "owner":
        err = "🚫 Нельзя действовать против участника с такой же или более высокой ролью."
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
                   "Посмотрите каталог: /catalog\n"
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
    if group_locked(g):
        await m.answer("Вход в группу закрыт.", reply_markup=main_kb(uid))
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
        where = "Создайте группу: /newgroup, посмотрите каталог /catalog или откройте ссылку-приглашение."
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
    await say(f"📝 Напишите название группы одним сообщением (до {TITLE_MAX} символов, без эмодзи).\n"
              "Отмена — /cancel")


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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"cg:pub:{gid}:1"),
        InlineKeyboardButton(text="🚫 Нет", callback_data=f"cg:pub:{gid}:0")]])
    await m.answer("Сделать группу публичной? Она появится в каталоге /catalog, и вступить в неё "
                   "сможет любой человек — без ссылки-приглашения.", reply_markup=kb)


@router.message(Command("newgroup"))
async def cmd_newgroup(m: Message, command: CommandObject):
    u = await reg(m)
    if not u:
        return
    if (command.args or "").strip():
        await create_group(m, u, command.args)         # можно и сразу: /newgroup Название
    else:
        await begin_newgroup(u["user_id"], m.answer)   # или в два шага: /newgroup → название


@router.callback_query(F.data.startswith("cg:pub:"))
async def create_pub_cb(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":")
    try:
        gid, val = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await c.answer("Кнопка устарела", show_alert=True)
        return
    mem = get_member(uid, gid)
    if not mem or mem["role"] != "owner":
        await c.answer("Нет доступа", show_alert=True)
        return
    run("UPDATE groups SET is_public=? WHERE id=?", (val, gid))
    await edit(c, "✅ Группа публичная — видна в /catalog." if val
              else "Группа приватная — вход только по ссылке-приглашению (изменить можно в /panel).")
    await c.answer()


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
        f"Участников: {count_members(mem['group_id'])}/{MAX_MEMBERS}\n"
        f"В каталоге: {'да' if mem['is_public'] else 'нет'}\n"
        f"Вход: {'закрыт' if mem['is_closed'] else 'открыт'}\n"
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
                     ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
                                          WHEN 'moderator' THEN 2 ELSE 3 END, u.nick_lc""",
                  (mem["group_id"],)):
        tags = (" (вы)" if r["user_id"] == u["user_id"] else "") + (" 🔇" if r["muted_until"] > now() else "")
        lines.append(f"{ROLE_ICON[r['role']]} {esc(r['nick'])}{tags}")
    await m.answer(f"📋 <b>Участники «{esc(mem['title'])}»</b> ({count_members(mem['group_id'])}/{MAX_MEMBERS})\n"
                   + "\n".join(lines)[:3700])


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
    await m.answer(f"Выйти из группы «{esc(mem['title'])}»? Вернуться можно будет только по ссылке-приглашению "
                   "или через каталог, если группа публичная и открыта.",
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


# ───────────────────────── Название и описание группы ─────────────────────────
async def finish_rename(m: Message, u, gid: int, raw_title: str):
    uid = u["user_id"]
    mem = get_member(uid, gid)
    if not mem or ROLE_LEVEL[mem["role"]] < 2:
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer("🚫 Переименовать группу может только владелец или администратор.")
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
                   f"(до {TITLE_MAX} символов, без эмодзи).\nОтмена — /cancel")


async def finish_desc(m: Message, u, gid: int, raw_desc: str):
    uid = u["user_id"]
    mem = get_member(uid, gid)
    if not mem or ROLE_LEVEL[mem["role"]] < 2:
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer("🚫 Изменить описание может только владелец или администратор.")
        return
    desc, err = parse_desc(raw_desc)
    if err:
        run("UPDATE users SET state=? WHERE user_id=?", (f"desc:{gid}", uid))
        await m.answer(f"{err}\nНапишите описание ещё раз или /cancel.")
        return
    run("UPDATE users SET state='' WHERE user_id=?", (uid,))
    run("UPDATE groups SET description=? WHERE id=?", (desc, gid))
    await m.answer("✅ Описание обновлено." if desc else "✅ Описание очищено.", reply_markup=main_kb(uid))


@router.message(Command("description"))
async def cmd_description(m: Message, command: CommandObject):
    mem = await staff(m, owner_only=True)
    if not mem:
        return
    uid = m.from_user.id
    if (command.args or "").strip():
        await finish_desc(m, ensure_user(uid), mem["group_id"], command.args)
        return
    run("UPDATE users SET state=? WHERE user_id=?", (f"desc:{mem['group_id']}", uid))
    await m.answer(f"📝 Напишите описание группы «{esc(mem['title'])}» одним сообщением "
                   f"(до {DESC_MAX} символов; видно в каталоге /catalog; можно оставить пустым, "
                   "чтобы очистить).\nОтмена — /cancel")


# ───────────────────────── Закрытие / открытие входа ─────────────────────────
@router.message(Command("close_group"))
async def cmd_close_group(m: Message):
    mem = await staff(m, owner_only=True)
    if not mem:
        return
    run("UPDATE groups SET is_closed=1 WHERE id=?", (mem["group_id"],))
    await m.answer(f"🔒 Вход в группу «{esc(mem['title'])}» закрыт. Новые участники не смогут войти, "
                   "текущие остаются.")


@router.message(Command("open_group"))
async def cmd_open_group(m: Message):
    mem = await staff(m, owner_only=True)
    if not mem:
        return
    run("UPDATE groups SET is_closed=0 WHERE id=?", (mem["group_id"],))
    await m.answer(f"🔓 Вход в группу «{esc(mem['title'])}» снова открыт.")


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
                               "Вернуться можно только по действующей ссылке-приглашению "
                               "(или через каталог, если группа публичная)." + note, kb=bool(note))
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


@router.message(Command("adm"))
async def cmd_adm(m: Message, command: CommandObject):
    """Владелец назначает администратора; администратор той же командой назначает модератора.
    Администратор обладает всеми правами владельца, кроме удаления группы."""
    if not await reg(m):
        return
    mem = get_member(m.from_user.id, reply_group(m))
    if not mem or ROLE_LEVEL[mem["role"]] < 2:
        await m.answer("🚫 Команда доступна владельцу и администраторам группы.")
        return
    t, _ = find_target(m, (command.args or "").split(), mem)
    if not t:
        await m.answer("Не нашёл участника. Ответьте командой на его сообщение "
                       "или укажите ник, например <code>/adm ник</code>.")
        return
    if t["user_id"] == mem["user_id"]:
        await m.answer("🙂 К себе это применить нельзя.")
        return
    if t["role"] == "owner":
        await m.answer("🚫 Владельца трогать нельзя.")
        return
    title = esc(mem["title"])
    if mem["role"] == "owner":
        if t["role"] == "admin":
            await m.answer("Он уже администратор.")
            return
        run("UPDATE members SET role='admin' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
        await m.answer(f"⭐ {esc(t['nick'])} теперь администратор группы «{title}».")
        await notify(t["user_id"], f"⭐ Вас назначили администратором группы «{title}». "
                                   "У вас те же права, что у владельца, кроме удаления группы.")
    else:   # mem["role"] == "admin"
        if t["role"] == "admin":
            await m.answer("🚫 Изменить права другого администратора может только владелец.")
            return
        if t["role"] == "moderator":
            await m.answer("Он уже модератор.")
            return
        run("UPDATE members SET role='moderator' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
        await m.answer(f"🛡 {esc(t['nick'])} теперь модератор группы «{title}».")
        await notify(t["user_id"], f"🛡 Вас назначили модератором группы «{title}». Команды — /help, панель — /panel.")


@router.message(Command("unadm"))
async def cmd_unadm(m: Message, command: CommandObject):
    """Снимает права, выданные через /adm: владелец снимает администратора, администратор — модератора."""
    if not await reg(m):
        return
    mem = get_member(m.from_user.id, reply_group(m))
    if not mem or ROLE_LEVEL[mem["role"]] < 2:
        await m.answer("🚫 Команда доступна владельцу и администраторам группы.")
        return
    t, _ = find_target(m, (command.args or "").split(), mem)
    if not t:
        await m.answer("Не нашёл участника. Ответьте командой на его сообщение "
                       "или укажите ник, например <code>/unadm ник</code>.")
        return
    if t["user_id"] == mem["user_id"]:
        await m.answer("🙂 К себе это применить нельзя.")
        return
    title = esc(mem["title"])
    if mem["role"] == "owner":
        if t["role"] != "admin":
            await m.answer("Он не администратор.")
            return
        run("UPDATE members SET role='member' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
        await m.answer(f"{esc(t['nick'])} больше не администратор группы «{title}».")
        await notify(t["user_id"], f"С вас сняли права администратора в группе «{title}».")
    else:   # mem["role"] == "admin"
        if t["role"] == "admin":
            await m.answer("🚫 Снять права администратора может только владелец.")
            return
        if t["role"] != "moderator":
            await m.answer("Он не модератор.")
            return
        run("UPDATE members SET role='member' WHERE user_id=? AND group_id=?", (t["user_id"], mem["group_id"]))
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


# ───────────────────────── Каталог: команды ─────────────────────────
@router.message(Command("catalog"))
@router.message(F.text == B_CATALOG)
async def cmd_catalog(m: Message):
    u = await reg(m)
    if not u:
        return
    total = catalog_count()
    rows = catalog_page(0)
    await m.answer(catalog_text(rows, 0, total), reply_markup=catalog_kb(rows, 0, total))


@router.callback_query(F.data.startswith("cat:"))
async def catalog_nav_cb(c: CallbackQuery):
    try:
        offset = int(c.data[4:])
    except ValueError:
        offset = 0
    total = catalog_count()
    rows = catalog_page(offset)
    await edit(c, catalog_text(rows, offset, total), catalog_kb(rows, offset, total))
    await c.answer()


@router.message(Command("search_group"))
async def cmd_search_group(m: Message, command: CommandObject):
    u = await reg(m)
    if not u:
        return
    q = (command.args or "").strip()
    if not q:
        await m.answer("Использование: <code>/search_group название или слово из описания</code>")
        return
    total = catalog_count(q)
    rows = catalog_page(0, q)
    await m.answer(catalog_text(rows, 0, total, header=f"🔎 <b>Поиск: «{esc(q)}»</b>"),
                   reply_markup=catalog_kb(rows, 0, total, prefix=f"cats:{q}"))


@router.callback_query(F.data.startswith("cats:"))
async def catalog_search_nav_cb(c: CallbackQuery):
    try:
        _, q, off = c.data.split(":", 2)
        offset = int(off)
    except ValueError:
        await c.answer("Кнопка устарела", show_alert=True)
        return
    total = catalog_count(q)
    rows = catalog_page(offset, q)
    await edit(c, catalog_text(rows, offset, total, header=f"🔎 <b>Поиск: «{esc(q)}»</b>"),
              catalog_kb(rows, offset, total, prefix=f"cats:{q}"))
    await c.answer()


@router.callback_query(F.data.startswith("cj:"))
async def catalog_join_cb(c: CallbackQuery):
    uid = c.from_user.id
    try:
        gid = int(c.data[3:])
    except ValueError:
        await c.answer("Кнопка устарела", show_alert=True)
        return
    u = ensure_user(uid)
    if not u["nick"]:
        await c.answer("Сначала придумайте ник — напишите боту /start", show_alert=True)
        return
    g = one("SELECT * FROM groups WHERE id=? AND is_public=1", (gid,))
    if not g:
        await c.answer("Этой группы больше нет в каталоге", show_alert=True)
        return
    if get_member(uid, gid):
        await c.answer("Вы уже в этой группе", show_alert=True)
        return
    if group_locked(g):
        await c.answer("Вход в группу закрыт", show_alert=True)
        return
    if count_groups(uid) >= MAX_GROUPS:
        await c.answer(limit_text(), show_alert=True)
        return
    run("INSERT INTO members(user_id, group_id, role, joined) VALUES(?,?,?,?)", (uid, gid, "member", now()))
    run("UPDATE users SET active_group=? WHERE user_id=?", (gid, uid))
    await c.answer("Вы вступили в группу")
    extra = "\nЭта группа теперь активна, переключаться между группами — /groups." if count_groups(uid) > 1 else ""
    await bot.send_message(uid, f"✅ Вы в группе «{esc(g['title'])}».\n"
                                f"Пишите сюда — сообщения увидят все участники под ником "
                                f"<b>{esc(u['nick'])}</b>." + extra,
                           reply_markup=main_kb(uid))
    await announce(gid, f"➕ <i>Участник {esc(u['nick'])} теперь в группе</i>", exclude=(uid,))


# ───────────────────────── Жалобы (/report) ─────────────────────────
def reports_last_24h_on(offender_id: int) -> int:
    return one("SELECT COUNT(*) AS c FROM reports WHERE offender_id=? AND date>?",
               (offender_id, now() - REPORT_WINDOW_SEC))["c"]


def reports_last_24h_by(reporter_id: int) -> int:
    return one("SELECT COUNT(*) AS c FROM reports WHERE reporter_id=? AND date>?",
               (reporter_id, now() - REPORT_WINDOW_SEC))["c"]


def recent_report_on_same_target(reporter_id: int, offender_id: int) -> bool:
    return one("SELECT 1 FROM reports WHERE reporter_id=? AND offender_id=? AND date>?",
               (reporter_id, offender_id, now() - REPORT_SAME_TARGET_COOLDOWN)) is not None


def reciprocal_report_exists(reporter_id: int, offender_id: int) -> bool:
    """Верно, если offender уже раньше пожаловался на reporter (взаимные жалобы)."""
    return one("SELECT 1 FROM reports WHERE reporter_id=? AND offender_id=?",
               (offender_id, reporter_id)) is not None


def discard_mutual_reports(reporter_id: int, offender_id: int):
    """Удаляет все жалобы между этими двумя людьми друг на друга (в обе стороны)."""
    run("""DELETE FROM reports WHERE (reporter_id=? AND offender_id=?)
           OR (reporter_id=? AND offender_id=?)""",
        (reporter_id, offender_id, offender_id, reporter_id))


@router.message(Command("report"))
async def cmd_report(m: Message):
    u = await reg(m)
    if not u:
        return
    if is_banned(u):
        return
    if not m.reply_to_message:
        await m.answer("Свайпните на сообщение нарушителя и напишите /report")
        return

    r = one("SELECT sender_id, group_id, ts FROM relay WHERE chat_id=? AND msg_id=?",
            (m.chat.id, m.reply_to_message.message_id))
    if not r:
        await m.answer("Свайпните на сообщение нарушителя и напишите /report")
        return

    reporter_id = u["user_id"]
    offender_id, gid, msg_ts = r["sender_id"], r["group_id"], r["ts"]

    if offender_id == reporter_id:
        await m.answer("🙂 Нельзя пожаловаться на собственное сообщение.")
        return
    if now() - msg_ts < REPORT_MIN_DIALOG_SEC:
        await m.answer("⏳ Пока рано — подождите немного перед тем, как жаловаться на это сообщение.")
        return
    if reports_last_24h_by(reporter_id) >= REPORT_MAX_PER_DAY:
        await m.answer("🚫 Вы уже подали максимум жалоб за сутки.")
        return
    if recent_report_on_same_target(reporter_id, offender_id):
        await m.answer("Вы уже недавно жаловались на этого участника — попробуйте позже.")
        return

    run("INSERT INTO reports(reporter_id, offender_id, group_id, date) VALUES(?,?,?,?)",
        (reporter_id, offender_id, gid, now()))

    if reciprocal_report_exists(reporter_id, offender_id):
        # оба пожаловались друг на друга — обе жалобы аннулируются и не считаются
        discard_mutual_reports(reporter_id, offender_id)
        await m.answer("Жалоба отправлена")
        return

    await m.answer("Жалоба отправлена")

    cnt = reports_last_24h_on(offender_id)
    if cnt >= BAN_THRESHOLD_LONG:
        ban_user(offender_id, BAN_LONG_SEC)
        await notify(offender_id, BAN_MESSAGE)
    elif cnt >= BAN_THRESHOLD_SHORT:
        ban_user(offender_id, BAN_SHORT_SEC)
        await notify(offender_id, BAN_MESSAGE)


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
    if not mem or ROLE_LEVEL[mem["role"]] < 1:
        await c.answer("Нет доступа", show_alert=True)
        return
    owner = mem["role"] == "owner"
    priv = ROLE_LEVEL[mem["role"]] >= 2        # владелец или администратор

    if act == "link":
        await c.message.answer(link_text(mem["token"], mem["title"]))
    elif act == "newlink":
        token = new_token(gid)
        await c.message.answer("♻️ Ссылка обновлена, старая больше не работает.\n\n"
                               + link_text(token, mem["title"]))
    elif act == "cancel":
        await edit(c, panel_text(mem), panel_kb(mem))
    elif priv and act == "rename":
        run("UPDATE users SET state=? WHERE user_id=?", (f"rename:{gid}", uid))
        await bot.send_message(uid, f"✏️ Напишите новое название группы «{esc(mem['title'])}» одним сообщением "
                                    f"(до {TITLE_MAX} символов, без эмодзи).\nОтмена — /cancel")
    elif priv and act == "desc":
        run("UPDATE users SET state=? WHERE user_id=?", (f"desc:{gid}", uid))
        await bot.send_message(uid, f"📝 Напишите описание группы «{esc(mem['title'])}» одним сообщением "
                                    f"(до {DESC_MAX} символов, можно оставить пустым).\nОтмена — /cancel")
    elif priv and act in ("protect", "media"):
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
    elif priv and act == "public":
        run("UPDATE groups SET is_public=1-is_public WHERE id=?", (gid,))
        mem = get_member(uid, gid)
        text = "🌐 Группа теперь в каталоге /catalog." if mem["is_public"] else "🌐 Группа скрыта из каталога."
        await announce(gid, f"<i>{text}</i>", exclude=(uid,))
        await edit(c, panel_text(mem), panel_kb(mem))
    elif priv and act == "closed":
        run("UPDATE groups SET is_closed=1-is_closed WHERE id=?", (gid,))
        mem = get_member(uid, gid)
        text = ("🔒 Вход в группу закрыт — новые участники не смогут войти." if mem["is_closed"]
                else "🔓 Вход в группу снова открыт.")
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
        run("DELETE FROM stats WHERE group_id=?", (gid,))
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


# ───────────────────────── Ввод ника / названия / описания и обычные сообщения ─────────────────────────
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
    elif st.startswith("desc:"):
        await finish_desc(m, u, int(st[5:]), m.text)


@router.message(F.text.startswith("/"))
async def unknown_command(m: Message):
    await m.answer("🤷 Неизвестная команда. Список — /help")


@router.message()
async def on_message(m: Message):
    u = await reg(m)
    if not u:
        return
    if is_banned(u):                   # в бане за жалобы — молча игнорируем любые сообщения
        return
    if u["state"]:                     # ждём текст (ник / название / описание), а прислали не текст
        if u["state"] == "nick":
            what = "ник"
        elif u["state"] == "newgroup":
            what = "название группы"
        elif u["state"].startswith("rename:"):
            what = "новое название группы"
        else:
            what = "описание группы"
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
        run("DELETE FROM reports WHERE date<?", (now() - REPORT_TTL,))
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
