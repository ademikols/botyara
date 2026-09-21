"""
Бот анонимных групп — aiogram 3, HTML-разметка, SQLite, всё в одном файле.

Запуск:
    pip install -U aiogram
    export BOT_TOKEN="123456:ABC..."      # токен от @BotFather
    export ADMIN_IDS="111111,222222"      # Telegram ID администраторов бота (через запятую)
    python anon_groups_bot_v3.py

Как это работает: участники пишут боту в личку, бот пересылает сообщение
всем остальным в активной группе от своего имени с ником отправителя.
Один человек может состоять максимум в MAX_GROUPS группах и переключаться между ними (/groups).
Сообщения из группы доходят только тем, у кого она сейчас активна — если человек сидит
в другой группе, сообщения из фоновой группы к нему не приходят, пока он не переключится.
В группе не может быть больше MAX_MEMBERS участников. Группы можно делать публичными —
такие видны всем в каталоге /catalog и через /search_group. Роли: владелец → администратор
→ модератор → участник; администратор обладает всеми правами владельца, кроме удаления группы
и передачи владения. Владелец может передать группу другому участнику: /transfer ник
(или кнопка «Передать владение» в /panel) — бывший владелец становится администратором.
Ссылки, e-mail и @юзернеймы в сообщениях обезвреживаются (дефанг): текст остаётся, но некликабелен.
Группы без сообщений INACTIVE_DAYS дней удаляются автоматически.
/rules — правила, /support — связь с администрацией (раз в сутки, 30–500 символов).

Скрытая админ-панель бота (/admin) доступна только ID из ADMIN_IDS; остальным её команды не
отвечают ничем. В BotFather их регистрировать не нужно. В панели:
  • статистика, онлайн и активные группы за 15 минут (/activity);
  • группы: список, закрытие входа, удаление (/groups_list);
  • бан / разбан / профиль пользователя (/ban, /unban, /find — по ID или нику);
  • участники любой группы с их ID и кнопкой «Бан» (/groups_list → «Участники», /gmembers ID_группы);
  • список всех пользователей с ID (/users) и список забаненных с разбаном (/banned);
  • настройки прямо из бота, без перезапуска: лимит групп на человека, участников в группе, срок
    автоудаления + переключатели (регистрация, создание групп, пауза пересылки) — /settings, /limit;
  • рассылка всем (/broadcast), ответ на обращения из /support, бэкап базы (/backup).
База старой версии (одна группа на человека / без каталога и ролей) обновляется автоматически
при запуске.
"""
import asyncio
import logging
import os
import re
import secrets
import sqlite3
import struct
import tempfile
import time
from datetime import datetime, timezone
from html import escape as esc
from typing import NamedTuple, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyParameters,
)
from aiogram.utils.text_decorations import html_decoration

# ───────────────────────── Настройки ─────────────────────────
# Значения ниже — «заводские». Часть из них (см. SETTINGS) администратор бота меняет прямо в боте:
# /admin → ⚙️ Настройки. Изменённое хранится в базе и переживает перезапуск.
TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
# ВАЖНО для хостинга с редеплоями: DB_PATH должен указывать на диск, который переживает
# передеплой (persistent volume / persistent disk). Если оставить путь внутри папки с кодом,
# при каждом обновлении из GitHub хостинг может пересоздавать эту папку и база будет стираться.
os.makedirs("/app/data", exist_ok=True)
DB_PATH = os.getenv("DB_PATH", "/app/data/anon_groups.db")
MAX_GROUPS = 5                             # максимум групп на одного человека
MAX_MEMBERS = 50                           # максимум участников в одной группе
TITLE_MAX = 40                             # максимальная длина названия группы
DESC_MAX = 200                             # максимальная длина описания группы
CATALOG_PAGE_SIZE = 5                      # групп на страницу каталога
RELAY_TTL = 3 * 24 * 3600                  # сколько хранить связку «сообщение → автор» (для модерации ответом)
DEFAULT_MUTE_MIN = 10
MAX_MUTE_MIN = 7 * 24 * 60
MAX_TEXT_LEN = 3500

# ── смена ника ──
NICK_CHANGES_PER_DAY = 3                   # сколько раз можно сменить ник за окно (меняется в /admin → ⚙️ Настройки)
NICK_CHANGE_WINDOW = 24 * 3600             # окно подсчёта — 24 часа (скользящее)

# ── неактивные группы ──
INACTIVE_DAYS = 7                          # группа без сообщений столько дней удаляется
INACTIVE_TTL = INACTIVE_DAYS * 24 * 3600

# ── /support ──
SUPPORT_MIN, SUPPORT_MAX = 30, 500         # длина обращения в символах
SUPPORT_COOLDOWN_H = 24                    # не чаще одного обращения раз в столько часов
SUPPORT_COOLDOWN = SUPPORT_COOLDOWN_H * 3600
SUPPORT_TTL = 30 * 24 * 3600               # сколько хранить записи об обращениях

# ── скрытая админ-панель ──
def _parse_admin_ids() -> set:
    ids = set()
    for part in os.getenv("ADMIN_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_admin_ids()
FOREVER = 4102444800                       # «навсегда» (2100 год)
MAX_BAN_HOURS = 24 * 365
ADMIN_PAGE_SIZE = 5                        # групп на страницу в /groups_list
ACTIVE_WINDOW_MIN = 15                     # окно «онлайн / группа активна» в аналитике, минут

# ── переключатели процессов (1 — включено, 0 — выключено) ──
REG_OPEN = 1                               # можно ли регистрироваться новым пользователям
NEWGROUP_OPEN = 1                          # можно ли создавать новые группы
RELAY_ON = 1                               # пересылка сообщений (0 — пауза для всех, кроме админов)

# ── жалобы (/report) ──
REPORT_MIN_DIALOG_SEC = 60             # нельзя жаловаться на сообщение младше 1 минуты
REPORT_SAME_TARGET_COOLDOWN = 3600     # на одного и того же не чаще раза в час
REPORT_MAX_PER_DAY = 3                 # максимум жалоб от одного человека в сутки
REPORT_WINDOW_SEC = 24 * 3600          # окно подсчёта жалоб для бана
REPORT_TTL = 7 * 24 * 3600             # сколько хранить жалобы
BAN_THRESHOLD_SHORT, BAN_SHORT_SEC = 5, 3600           # 5–9 жалоб → бан на 1 час
BAN_THRESHOLD_LONG, BAN_LONG_SEC = 10, 24 * 3600        # 10+ жалоб → бан на 24 часа
BAN_MESSAGE = "🚫 Вы забанены за спам/оскорбления."


class Setting(NamedTuple):
    default: int
    lo: int
    hi: int
    label: str                             # полное название — в тексте панели
    short: str                             # короткое — на кнопке
    kind: str = "int"                      # "int" — число, "bool" — переключатель вкл/выкл


# Всё, что администратор может менять прямо в боте. Ключ = имя глобальной переменной.
SETTINGS = {
    "MAX_GROUPS": Setting(MAX_GROUPS, 1, 100, "Лимит групп на одного человека", "Групп на человека"),
    "MAX_MEMBERS": Setting(MAX_MEMBERS, 2, 5000, "Максимум участников в группе", "Участников в группе"),
    "INACTIVE_DAYS": Setting(INACTIVE_DAYS, 1, 365, "Дней без сообщений до автоудаления группы", "Автоудаление, дней"),
    "NICK_CHANGES_PER_DAY": Setting(NICK_CHANGES_PER_DAY, 1, 50, "Смен ника за 24 часа", "Смен ника / сутки"),
    "REG_OPEN": Setting(1, 0, 1, "Регистрация новых пользователей", "Регистрация", "bool"),
    "NEWGROUP_OPEN": Setting(1, 0, 1, "Создание новых групп", "Создание групп", "bool"),
    "RELAY_ON": Setting(1, 0, 1, "Пересылка сообщений (выкл = пауза)", "Пересылка", "bool"),
}

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

# Только публичные команды. Админские (/admin, /statistics, /ban, /unban, /find, /groups_list и др.)
# сюда НЕ добавляются — они скрыты и работают только для ADMIN_IDS.
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
    ("rules", "Правила бота"),
    ("support", "Написать администрации"),
    ("panel", "Управление группой"),
    ("rename", "Сменить название группы"),
    ("description", "Сменить описание группы"),
    ("kick", "Исключить навсегда"),
    ("unkick", "Разблокировать участника"),
    ("mute", "Заглушить"),
    ("unmute", "Снять мут"),
    ("link", "Ссылка-приглашение"),
    ("newlink", "Обновить ссылку"),
    ("adm", "Назначить администратора / модератора"),
    ("unadm", "Снять администратора / модератора"),
    ("close_group", "Закрыть вход в группу"),
    ("open_group", "Открыть вход в группу"),
    ("mod", "Назначить модератора"),
    ("unmod", "Снять модератора"),
    ("transfer", "Передать владение группой"),
    ("help", "Помощь"),
    ("about", "О боте"),
]


# Тексты — функции, а не константы: лимиты меняются из админ-панели, тексты должны подхватывать новые значения.
def rules_text() -> str:
    return f"""📜 <b>Правила бота:</b>

1. Уважай собеседников — без мата, оскорблений и травли.
2. Не спамь и не рекламируй — ссылки, каналы, боты запрещены.
3. Без 18+ — порно, жестокость, шок-контент = бан навсегда.
4. Жалоба — свайп на сообщение + /report. Ложные жалобы наказуемы.
5. Максимум {MAX_MEMBERS} человек в группе, лимит {MAX_GROUPS} групп.
6. Неактивные группы ({INACTIVE_DAYS} дней) удаляются.
7. Связь с администрацией — команда /support. Лимит: 1 сообщение в {SUPPORT_COOLDOWN_H} ч, от {SUPPORT_MIN} до {SUPPORT_MAX} символов.
8. Администрация может изменять правила без предупреждения. Незнание правил не освобождает от бана."""


def help_text() -> str:
    return f"""❓ <b>Помощь</b>

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
/nick — сменить ник (не больше {NICK_CHANGES_PER_DAY} смен за 24 часа)
/leave — выйти из активной группы
/cancel — отменить ввод
/report — свайпните на сообщение нарушителя и отправьте эту команду
/rules — правила бота
/support — написать администрации бота ({SUPPORT_MIN}–{SUPPORT_MAX} символов, 1 раз в {SUPPORT_COOLDOWN_H} ч)

Состоять можно максимум в {MAX_GROUPS} группах, участников в одной группе — максимум {MAX_MEMBERS}.
Группы без сообщений {INACTIVE_DAYS} дней удаляются автоматически.

<b>Модераторы</b> (ответьте командой на сообщение или укажите ник)
/kick ник — исключить навсегда: не сможет войти снова ни по ссылке-приглашению, ни через каталог
/unkick ник — снять блокировку, поставленную /kick — участник снова сможет войти
/mute ник 30 — заглушить на 30 минут
/unmute ник — снять мут
/link — ссылка-приглашение
/newlink — обновить ссылку (старая перестанет работать)
/panel — панель управления

<b>Администраторы и владелец</b> (те же права, что у владельца, кроме удаления группы и передачи владения)
/adm ник — владелец назначает администратора; администратор той же командой назначает модератора
/unadm ник — соответствующее снятие прав
/rename — сменить название группы
/description — сменить описание группы (видно в каталоге)
/close_group — закрыть вход (новые участники не смогут войти)
/open_group — снова открыть вход
/panel — публичность, защита от пересылки, медиа, название, описание, вход, удаление группы

<b>Только владелец</b>
/transfer ник — передать владение группой другому участнику (вы станете администратором)
/panel → «Передать владение», «Удалить группу»"""


def about_text() -> str:
    return f"""ℹ️ <b>О боте</b>

Бот анонимных групп. Вас видят только под ником — ваш Telegram-аккаунт скрыт от всех, включая владельца и модераторов группы.

• Вход по ссылке-приглашению или через каталог публичных групп (/catalog)
• До {MAX_GROUPS} групп на один аккаунт, между ними можно переключаться (/groups)
• До {MAX_MEMBERS} участников в одной группе
• Сообщения приходят только из активной группы — остальные группы «молчат» в фоне, пока вы на них не переключитесь
• Защита от пересылки и сохранения — настройка группы
• Ссылки, почта и @юзернеймы в сообщениях обезвреживаются: текст виден, но нажать на него нельзя
• Группы без сообщений {INACTIVE_DAYS} дней удаляются
• Контакты, геопозиция и опросы не передаются, чтобы вас не раскрыть
• Правка и удаление сообщений у других участников не синхронизируются

Правила — /rules · Связь с администрацией — /support"""


# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    nick TEXT, nick_lc TEXT UNIQUE,
    state TEXT DEFAULT '',            -- '' | 'nick' | 'newgroup' | 'support' | 'rename:<id>' | 'desc:<id>' | 'transfer:<id>' | 'adm:<действие>'
    pending TEXT DEFAULT '',          -- токен приглашения, ждущий регистрации
    active_group INTEGER DEFAULT 0,   -- группа, в которую уходят сообщения
    banned_until INTEGER DEFAULT 0,   -- до какого времени (unix) человек в бане
    last_seen INTEGER DEFAULT 0,      -- последнее обращение к боту (для «онлайн» в админ-аналитике)
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
    created INTEGER,
    last_active INTEGER DEFAULT 0     -- время последнего сообщения (для автоудаления неактивных групп)
);
CREATE TABLE IF NOT EXISTS members(
    user_id INTEGER, group_id INTEGER, role TEXT DEFAULT 'member',
    muted_until INTEGER DEFAULT 0, joined INTEGER,
    PRIMARY KEY(user_id, group_id)    -- один человек может состоять в нескольких группах
);
CREATE TABLE IF NOT EXISTS kicked_members(
    user_id INTEGER, group_id INTEGER, kicked_at INTEGER,
    PRIMARY KEY(user_id, group_id)    -- навсегда исключённые из группы через /kick: не могут войти
                                       -- обратно ни по ссылке, ни через каталог, пока не будет /unkick
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
CREATE TABLE IF NOT EXISTS support(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, text TEXT, date INTEGER
);
CREATE INDEX IF NOT EXISTS ix_support_user ON support(user_id, date);
CREATE TABLE IF NOT EXISTS settings(              -- значения, изменённые администратором в боте
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS nick_changes(          -- журнал смен ника (для лимита «N раз за 24 часа»)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, date INTEGER
);
CREATE INDEX IF NOT EXISTS ix_nick_changes_user ON nick_changes(user_id, date);
""")


def migrate():
    """Обновляет базу старой версии до текущей схемы. Таблица groups никогда не удаляется —
    существующие группы и их участники сохраняются."""
    ucols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "active_group" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN active_group INTEGER DEFAULT 0")
    if "banned_until" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN banned_until INTEGER DEFAULT 0")
    if "last_seen" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER DEFAULT 0")

    gcols = {r["name"] for r in db.execute("PRAGMA table_info(groups)")}
    if "is_public" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN is_public INTEGER DEFAULT 0")
    if "is_closed" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN is_closed INTEGER DEFAULT 0")
    if "description" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN description TEXT DEFAULT ''")
    if "last_active" not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN last_active INTEGER DEFAULT 0")
    # Старым группам ставим «активность = сейчас», чтобы они не удалились сразу после обновления.
    db.execute("UPDATE groups SET last_active=? WHERE COALESCE(last_active,0)=0", (int(time.time()),))

    db.executescript("""
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER, offender_id INTEGER, group_id INTEGER, date INTEGER
    );
    CREATE INDEX IF NOT EXISTS ix_reports_offender ON reports(offender_id);
    CREATE INDEX IF NOT EXISTS ix_reports_date ON reports(date);
    CREATE TABLE IF NOT EXISTS kicked_members(
        user_id INTEGER, group_id INTEGER, kicked_at INTEGER,
        PRIMARY KEY(user_id, group_id)
    );
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
    CREATE INDEX IF NOT EXISTS ix_groups_active ON groups(last_active);
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


# ───────────────────────── Настройки, меняемые из бота ─────────────────────────
def cfg(key: str) -> int:
    """Текущее значение настройки из SETTINGS."""
    return globals()[key]


def load_settings():
    """Читает сохранённые в базе значения и применяет их к глобальным настройкам бота — на лету."""
    global INACTIVE_TTL
    saved = {r["key"]: r["value"] for r in many("SELECT key, value FROM settings")}
    for key, s in SETTINGS.items():
        val = s.default
        if key in saved:
            try:
                val = min(s.hi, max(s.lo, int(saved[key])))
            except ValueError:
                pass
        globals()[key] = val
    INACTIVE_TTL = INACTIVE_DAYS * 24 * 3600


def save_setting(key: str, value: int):
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(int(value))))
    load_settings()


load_settings()

_seen_cache: dict = {}


def touch(uid: int):
    """Помечает, что человек сейчас пользуется ботом (для аналитики «онлайн»). Не чаще раза в 20 секунд."""
    t = now()
    if t - _seen_cache.get(uid, 0) >= 20:
        if run("UPDATE users SET last_seen=? WHERE user_id=?", (t, uid)).rowcount:
            _seen_cache[uid] = t


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


def is_kicked(uid: int, gid: int) -> bool:
    """Верно, если человека навсегда исключили из этой группы через /kick и это не снято /unkick."""
    return one("SELECT 1 FROM kicked_members WHERE user_id=? AND group_id=?", (uid, gid)) is not None


def kick_forever(uid: int, gid: int):
    run("INSERT OR REPLACE INTO kicked_members(user_id, group_id, kicked_at) VALUES(?,?,?)",
        (uid, gid, now()))


def unkick(uid: int, gid: int) -> bool:
    """Снимает блокировку /kick. Возвращает True, если блокировка действительно была."""
    return run("DELETE FROM kicked_members WHERE user_id=? AND group_id=?", (uid, gid)).rowcount > 0


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


def wipe_group(gid: int):
    """Удаляет саму группу и всё, что с ней связано (участников — отдельно, через drop_member)."""
    run("DELETE FROM relay WHERE group_id=?", (gid,))
    run("DELETE FROM stats WHERE group_id=?", (gid,))
    run("DELETE FROM kicked_members WHERE group_id=?", (gid,))
    run("DELETE FROM groups WHERE id=?", (gid,))


# ───────────────────────── Дефанг ссылок, e-mail и юзернеймов ─────────────────────────
# Ссылки, домены, IP, e-mail и @юзернеймы не удаляются, а «обезвреживаются»:
#   https://t.me/bot → hxxps[:]//t[.]me/bot,  @bot → [@]bot,  a@b.com → a[@]b[.]com.
# Telegram такой текст не превращает в кликабельные ссылки.
DEFANG_ANY_TLD = False   # True — ломать вообще любое «слово.слово» (надёжнее, но заденет и опечатки без пробела)

_TLDS = ("com|net|org|info|biz|xyz|top|site|online|club|link|store|shop|live|life|world|fun|vip|app|dev|"
         "pro|one|tech|cloud|space|website|click|download|games|today|news|blog|wiki|art|network|agency|"
         "media|team|zone|works|page|ink|icu|host|press|email|chat|social|group|lol|wtf|xxx|"
         "рф|онлайн|сайт")
_TLD_PART = r"[a-z]{2,24}" if DEFANG_ANY_TLD else r"(?:" + _TLDS + r"|[a-z]{2})"
_PATH = r"(?::\d{1,5})?(?:[/?#][^\s<>\"']*)?"
_URL = (r"(?:(?:https?|ftp|tg|ton)://[^\s<>\"']+"                          # со схемой
        r"|(?:[\w\-]+\.)+" + _TLD_PART + r"(?![\w\-])" + _PATH +           # домен.зона[/путь]
        r"|\d{1,3}(?:\.\d{1,3}){3}(?![\w\-])" + _PATH + r")")              # IPv4
_ANY_RE = re.compile(
    r"(?i)(?P<email>(?<![\w.+\-])[\w.+\-]+@(?:[\w\-]+\.)+[\w\-]{2,})"
    r"|(?P<url>(?<![\w.\-])" + _URL + r")"
    r"|(?P<mention>(?<![\w@])@(?=[A-Za-z]))"
)
_TRAIL_PUNCT = ".,;:!?)»…"


def _plan(text: str) -> list:
    """chunks[i] — то, на что заменяется i-й символ текста (обычно он сам)."""
    chunks = list(text)
    for mo in _ANY_RE.finditer(text):
        a, b = mo.span()
        if mo.lastgroup == "mention":
            chunks[a] = "[@]"
            continue
        while b > a and text[b - 1] in _TRAIL_PUNCT:      # хвостовая пунктуация — не часть ссылки
            b -= 1
        seg = text[a:b]
        sch = re.match(r"(?i)(https?|ftp)://", seg)
        if sch:
            if sch.group(1).lower() == "ftp":
                chunks[a + 1] = "x"
            else:
                chunks[a + 1] = chunks[a + 2] = "x"
        k = seg.find("://")
        if k >= 0:
            chunks[a + k] = "[:]"
        for i in range(a, b):
            if text[i] == ".":
                chunks[i] = "[.]"
            elif text[i] == "@":                          # @ внутри ссылки/почты тоже ломаем
                chunks[i] = "[@]"
    return chunks


def defang_map(text: str):
    """Возвращает (новый_текст, pos): pos[i] — где в новом тексте оказался i-й символ исходного
    (pos[len(text)] — конец). Нужно, чтобы сдвинуть форматирование (жирный, код и т.д.)."""
    chunks = _plan(text)
    pos, acc = [], 0
    for ch in chunks:
        pos.append(acc)
        acc += len(ch)
    pos.append(acc)
    return "".join(chunks), pos


def defang(text: str) -> str:
    """Обезвреживает ссылки, e-mail и @юзернеймы в тексте (текст сохраняется, но не кликается)."""
    return defang_map(text)[0] if text else text


def parse_title(raw: str):
    """Название группы: схлопывает пробелы и переводы строк, без эмодзи, ссылки обезврежены.
    Возвращает (название, ошибка)."""
    title = defang(" ".join((raw or "").split()))
    if not title:
        return "", "❌ Название не может быть пустым."
    if EMOJI_RE.search(title):
        return "", "🚫 В названии группы нельзя использовать эмодзи."
    if len(title) > TITLE_MAX:
        return "", f"✂️ Слишком длинное название: максимум {TITLE_MAX} символов, у вас {len(title)}."
    return title, None


def parse_desc(raw: str):
    """Описание группы (для каталога): схлопывает пробелы, ссылки обезврежены, может быть пустым.
    Возвращает (текст, ошибка)."""
    desc = defang(" ".join((raw or "").split()))
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


class SeenMiddleware(BaseMiddleware):
    """Запоминает время последней активности человека — из этого считается «онлайн» в админ-панели."""

    async def __call__(self, handler, event, data: dict):
        u = getattr(event, "from_user", None)
        if u:
            touch(u.id)
        return await handler(event, data)


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
        kb.append([InlineKeyboardButton(text="👑 Передать владение", callback_data=f"p:transfer:{gid}")])
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


async def purge_group(gid: int, reason: str):
    """Полностью удаляет группу (участники, статистика, связки сообщений) и сообщает участникам.
    reason — продолжение фразы «Группа «…» …», например «удалена администрацией»."""
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    if not g:
        return
    ids = [r["user_id"] for r in many("SELECT user_id FROM members WHERE group_id=?", (gid,))]
    notes = {i: drop_member(i, gid) for i in ids}
    wipe_group(gid)
    for i in ids:
        await notify(i, f"🗑 Группа «{esc(g['title'])}» {reason}." + notes[i], kb=True)
        await asyncio.sleep(0.04)


async def reg(m: Message):
    """Вернёт пользователя, если у него есть ник, иначе попросит придумать."""
    u = ensure_user(m.from_user.id)
    if not u["nick"]:
        if not REG_OPEN and not is_admin(u["user_id"]):
            await m.answer("🔒 Регистрация новых пользователей временно закрыта. Загляните позже.")
            return None
        run("UPDATE users SET state='nick' WHERE user_id=?", (u["user_id"],))
        await m.answer("✏️ Сначала придумайте ник — отправьте его сообщением "
                       "(3–20 символов: буквы, цифры, _ и -).")
        return None
    return u


async def staff(m: Message, owner_only: bool = False, gid: Optional[int] = None):
    """Вернёт участника, если он модератор+ (или владелец/администратор при owner_only) группы
    (активной или указанной gid). owner_only=True требует роль владелец или администратор —
    администратор обладает всеми правами владельца, кроме удаления группы и передачи владения."""
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


def find_kicked_target(m: Message, args: list, gid: int):
    """Цель для /unkick: ищет по ответу на старое сообщение (через relay) или по нику — среди ВСЕХ
    пользователей бота, а не только текущих участников группы (исключённый уже не член группы).
    Возвращает (user_id, nick) или (None, None)."""
    if m.reply_to_message:
        r = one("SELECT sender_id FROM relay WHERE chat_id=? AND msg_id=? AND group_id=?",
                (m.chat.id, m.reply_to_message.message_id, gid))
        if r:
            u = one("SELECT user_id, nick FROM users WHERE user_id=?", (r["sender_id"],))
            if u:
                return u["user_id"], u["nick"]
    if args:
        u = one("SELECT user_id, nick FROM users WHERE nick_lc=?", (args[0].lstrip("@").lower(),))
        if u:
            return u["user_id"], u["nick"]
    return None, None


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
# Сущности, которые делают текст кликабельным: ссылки, ссылки-«под текстом», @упоминания.
# Они отбрасываются (видимый текст остаётся), а сам текст дополнительно проходит через defang().
LINK_ENTITY_TYPES = ("url", "text_link", "mention", "text_mention")


def _to_units(s: str) -> str:
    """Строка, где каждый символ = одна UTF-16-единица (как считает Telegram в offset/length)."""
    b = s.encode("utf-16-le")
    return "".join(map(chr, struct.unpack(f"<{len(b) // 2}H", b)))


def _from_units(s: str) -> str:
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le")


def to_html(m: Message) -> str:
    """Текст (или подпись) сообщения в HTML-разметке с обезвреженными ссылками, почтой и @юзернеймами.
    Дефанг делается по ПРОСТОМУ тексту, а не по готовому HTML: иначе <b>t</b>.me/x проскакивал бы —
    теги разрезали ссылку, а Telegram ищет ссылки именно в простом тексте. Форматирование
    (offset/length) при этом пересчитывается под удлинившийся текст."""
    raw = m.text or m.caption or ""
    if not raw:
        return ""
    text = _to_units(raw)
    new_text, pos = defang_map(text)
    ents = []
    for e in (m.entities or m.caption_entities or []):
        if str(getattr(e.type, "value", e.type)) in LINK_ENTITY_TYPES:
            continue
        start = min(e.offset, len(text))
        end = min(e.offset + e.length, len(text))
        ents.append(e.model_copy(update={"offset": pos[start], "length": pos[end] - pos[start]}))
    return html_decoration.unparse(_from_units(new_text), ents)


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
    # Стикеры/кружки (или слишком длинная подпись): ник — отдельным сообщением, реплай — на него.
    # Подпись медиа при этом тоже идёт через to_html(), а у скопированного медиа оригинальная
    # подпись убирается (caption=""), иначе ссылки из неё остались бы кликабельными.
    is_cap = ctype in CAPTION_TYPES
    h = await bot.send_message(chat_id, f"{head}:" + (f"\n{cap}" if cap and is_cap else ""),
                               protect_content=protect, reply_parameters=rp)
    if is_cap:
        r = await bot.copy_message(chat_id, m.chat.id, m.message_id, caption="", protect_content=protect)
    else:
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
def nick_changes_recent(uid: int) -> list:
    """Времена смен ника за последние 24 часа (по возрастанию)."""
    return [r["date"] for r in many("SELECT date FROM nick_changes WHERE user_id=? AND date>? ORDER BY date",
                                    (uid, now() - NICK_CHANGE_WINDOW))]


def nick_wait(uid: int) -> int:
    """Сколько секунд до следующей смены ника (0 — можно менять сейчас)."""
    ts = nick_changes_recent(uid)
    if len(ts) < NICK_CHANGES_PER_DAY:
        return 0
    # место освободится, когда из окна выйдет нужная по счёту старая смена
    return max(1, ts[len(ts) - NICK_CHANGES_PER_DAY] + NICK_CHANGE_WINDOW - now())


def nick_limit_text(left: int) -> str:
    return (f"⏳ Ник можно менять не больше {NICK_CHANGES_PER_DAY} раз(а) за 24 часа — лимит исчерпан. "
            f"Следующая смена — через {fmt_left(left)}.")


async def finish_nick(m: Message, u, text: str):
    if not u["nick"] and not REG_OPEN and not is_admin(u["user_id"]):
        run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
        await m.answer("🔒 Регистрация новых пользователей временно закрыта. Загляните позже.")
        return
    if u["nick"]:                      # первый ник лимитом не считается — только смены
        left = nick_wait(u["user_id"])
        if left:
            run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
            await m.answer(nick_limit_text(left))
            return
    nick = text.strip().lstrip("@")
    if not NICK_RE.fullmatch(nick):
        await m.answer("❌ Ник: 3–20 символов, только буквы, цифры, _ и -. Попробуйте ещё раз.")
        return
    if u["nick"] == nick:              # тот же ник — не смена, лимит не тратим
        run("UPDATE users SET state='' WHERE user_id=?", (u["user_id"],))
        await m.answer("Это и так ваш ник.", reply_markup=main_kb(u["user_id"]))
        return
    if one("SELECT 1 FROM users WHERE nick_lc=? AND user_id!=?", (nick.lower(), u["user_id"])):
        await m.answer("❌ Этот ник занят. Придумайте другой.")
        return
    old, token, uid = u["nick"], u["pending"], u["user_id"]
    run("UPDATE users SET nick=?, nick_lc=?, state='', pending='' WHERE user_id=?",
        (nick, nick.lower(), uid))
    if old:
        run("INSERT INTO nick_changes(user_id, date) VALUES(?,?)", (uid, now()))
        spare = max(0, NICK_CHANGES_PER_DAY - len(nick_changes_recent(uid)))
        await m.answer(f"✅ Ник изменён: <b>{esc(nick)}</b>\nСмен ника осталось за ближайшие 24 часа: {spare}",
                       reply_markup=main_kb(uid))
        for r in many("SELECT group_id FROM members WHERE user_id=?", (uid,)):   # ник общий для всех групп
            await announce(r["group_id"], f"✏️ <i>{esc(old)} теперь {esc(nick)}</i>", exclude=(uid,))
        return
    await m.answer(f"✅ Ник <b>{esc(nick)}</b> сохранён!\n\n"
                   "Создайте группу: /newgroup\n"
                   "Посмотрите каталог: /catalog\n"
                   "или откройте ссылку-приглашение от владельца группы.\n"
                   "Правила бота — /rules\n"
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
    if is_kicked(uid, g["id"]):
        await m.answer("🚫 Вам запрещено входить в эту группу.", reply_markup=main_kb(uid))
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
        if not REG_OPEN and not is_admin(u["user_id"]):
            await m.answer("🔒 Регистрация новых пользователей временно закрыта. Загляните позже.")
            return
        run("UPDATE users SET state='nick', pending=? WHERE user_id=?", (token, u["user_id"]))
        await m.answer("👋 <b>Добро пожаловать!</b>\nЭто бот анонимных групп: в группах вас видят только под ником. "
                       "Правила — /rules\n\n"
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
    if u["nick"]:
        left = nick_wait(u["user_id"])
        if left:                       # лимит исчерпан — не просим вводить ник впустую
            await m.answer(nick_limit_text(left))
            return
    if command and command.args:
        await finish_nick(m, u, command.args)
        return
    run("UPDATE users SET state='nick' WHERE user_id=?", (u["user_id"],))
    await m.answer("✏️ Отправьте новый ник: 3–20 символов, буквы, цифры, _ и -."
                   + (f"\nСменить ник можно не больше {NICK_CHANGES_PER_DAY} раз(а) за 24 часа.\nОтмена — /cancel"
                      if u["nick"] else ""))


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
    if not NEWGROUP_OPEN and not is_admin(uid):
        await say("🔒 Создание новых групп временно приостановлено администрацией.")
        return
    if count_groups(uid) >= MAX_GROUPS:
        await say(limit_text())
        return
    run("UPDATE users SET state='newgroup' WHERE user_id=?", (uid,))
    await say(f"📝 Напишите название группы одним сообщением (до {TITLE_MAX} символов, без эмодзи).\n"
              "Отмена — /cancel")


async def create_group(m: Message, u, raw_title: str):
    uid = u["user_id"]
    if not NEWGROUP_OPEN and not is_admin(uid):
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer("🔒 Создание новых групп временно приостановлено администрацией.")
        return
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
    gid = run("INSERT INTO groups(title, owner_id, token, created, last_active) VALUES(?,?,?,?,?)",
              (title, uid, token, now(), now())).lastrowid
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
        await m.answer("👑 Владелец не может выйти. Передайте владение (/transfer) или удалите группу в /panel.")
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
        await c.answer("Владелец не может выйти — передайте владение или удалите группу в /panel", show_alert=True)
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
    gid = mem["group_id"]
    kick_forever(t["user_id"], gid)
    note = drop_member(t["user_id"], gid)
    await m.answer(f"⛔ {esc(t['nick'])} навсегда исключён из группы «{esc(mem['title'])}» — "
                   "не сможет войти снова ни по ссылке-приглашению, ни через каталог, пока это "
                   "не отменит /unkick.")
    await notify(t["user_id"], f"⛔ Вас навсегда исключили из группы «{esc(mem['title'])}». "
                               "Вернуться будет невозможно, пока модерация не снимет блокировку." + note,
                 kb=bool(note))
    await announce(gid, f"⛔ <i>Участник {esc(t['nick'])} навсегда исключён из группы</i>",
                   exclude=(mem["user_id"],))


@router.message(Command("unkick"))
async def cmd_unkick(m: Message, command: CommandObject):
    """Снимает блокировку, поставленную /kick — можно ответом (свайпом) на старое сообщение
    исключённого или по нику: /unkick ник."""
    mem = await staff(m, owner_only=False, gid=reply_group(m))
    if not mem:
        return
    gid = mem["group_id"]
    args = (command.args or "").split()
    uid, nick = find_kicked_target(m, args, gid)
    if not uid:
        await m.answer("Не нашёл пользователя. Ответьте командой на его старое сообщение "
                       "или укажите ник, например <code>/unkick ник</code>.")
        return
    if not unkick(uid, gid):
        await m.answer(f"{esc(nick)} и так не заблокирован в этой группе.")
        return
    await m.answer(f"✅ {esc(nick)} разблокирован — снова сможет войти в «{esc(mem['title'])}» "
                   "по ссылке-приглашению или через каталог.")
    await notify(uid, f"✅ Вас разблокировали в группе «{esc(mem['title'])}» — можно снова войти "
                      "по ссылке-приглашению или через каталог.")


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
    Администратор обладает всеми правами владельца, кроме удаления группы и передачи владения."""
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
                                   "У вас те же права, что у владельца, кроме удаления группы и передачи владения.")
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


# ───────────────────────── Передача владения ─────────────────────────
def member_by_nick(gid: int, nick: str):
    return one("SELECT m.user_id, m.role, u.nick FROM members m JOIN users u ON u.user_id = m.user_id "
               "WHERE m.group_id=? AND u.nick_lc=?", (gid, nick.strip().lstrip("@").lower()))


def transfer_error(mem, t) -> Optional[str]:
    if not t:
        return "❌ Не нашёл такого участника в этой группе."
    if t["user_id"] == mem["user_id"]:
        return "🙂 Вы и так владелец."
    return None


async def confirm_transfer(m: Message, mem, t):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, передать", callback_data=f"tr:yes:{mem['group_id']}:{t['user_id']}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="tr:no")]])
    await m.answer(f"👑 Передать владение группой «{esc(mem['title'])}» участнику <b>{esc(t['nick'])}</b>?\n\n"
                   "Он получит все права владельца, включая удаление группы и передачу владения. "
                   "Вы станете администратором. Вернуть владение сами не сможете — только если новый владелец "
                   "передаст его вам.", reply_markup=kb)


async def apply_transfer(gid: int, old_id: int, new_id: int, by_admin: bool = False):
    """Меняет владельца группы: новый — владелец, прежний — администратор. Уведомляет всех причастных."""
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    if not g or old_id == new_id:
        return
    db.execute("UPDATE members SET role='admin' WHERE user_id=? AND group_id=?", (old_id, gid))
    db.execute("UPDATE members SET role='owner', muted_until=0 WHERE user_id=? AND group_id=?", (new_id, gid))
    db.execute("UPDATE groups SET owner_id=? WHERE id=?", (new_id, gid))
    db.commit()
    title = esc(g["title"])
    new_nick = (one("SELECT nick FROM users WHERE user_id=?", (new_id,)) or {"nick": "—"})["nick"]
    who = "администрацией бота" if by_admin else "владельцем"
    await notify(new_id, f"👑 Вам передали владение группой «{title}» ({who}). "
                         "Теперь вы владелец: доступны все настройки и удаление группы — /panel.")
    await notify(old_id, f"👑 Владение группой «{title}» передано участнику {esc(new_nick)}. "
                         "Вы теперь администратор.")
    await announce(gid, f"👑 <i>Новый владелец группы — {esc(new_nick)}</i>", exclude=(new_id, old_id))


@router.message(Command("transfer"))
async def cmd_transfer(m: Message, command: CommandObject):
    if not await reg(m):
        return
    uid = m.from_user.id
    mem = get_member(uid, reply_group(m))
    if not mem:
        await m.answer(no_group_text(uid))
        return
    if mem["role"] != "owner":
        await m.answer("🚫 Передать владение может только владелец группы.")
        return
    args = (command.args or "").split()
    if not args and not m.reply_to_message:
        run("UPDATE users SET state=? WHERE user_id=?", (f"transfer:{mem['group_id']}", uid))
        await m.answer(f"👑 Напишите ник участника, которому хотите передать группу «{esc(mem['title'])}».\n"
                       "Отмена — /cancel")
        return
    t, _ = find_target(m, args, mem)
    err = transfer_error(mem, t)
    if err:
        await m.answer(err + "\nУкажите ник: <code>/transfer ник</code> или ответьте командой на его сообщение.")
        return
    await confirm_transfer(m, mem, t)


async def finish_transfer(m: Message, u, gid: int, raw: str):
    uid = u["user_id"]
    mem = get_member(uid, gid)
    if not mem or mem["role"] != "owner":
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer("🚫 Передать владение может только владелец группы.")
        return
    t = member_by_nick(gid, raw)
    err = transfer_error(mem, t)
    if err:                            # остаёмся в режиме ввода — можно прислать другой ник
        await m.answer(f"{err}\nНапишите ник ещё раз или /cancel.")
        return
    run("UPDATE users SET state='' WHERE user_id=?", (uid,))
    await confirm_transfer(m, mem, t)


@router.callback_query(F.data.startswith("tr:"))
async def transfer_cb(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":")
    if parts[1] == "no":
        await edit(c, "Отменено.")
        await c.answer()
        return
    try:
        gid, tid = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await c.answer("Кнопка устарела — вызовите /transfer заново", show_alert=True)
        return
    mem = get_member(uid, gid)
    if not mem or mem["role"] != "owner":
        await c.answer("Вы больше не владелец этой группы", show_alert=True)
        return
    t = get_member(tid, gid)
    if not t:
        await c.answer("Этого участника уже нет в группе", show_alert=True)
        return
    nick = ensure_user(tid)["nick"]
    await apply_transfer(gid, uid, tid)
    await edit(c, f"👑 Готово: новый владелец группы «{esc(mem['title'])}» — {esc(nick)}. Вы теперь администратор.")
    await c.answer()


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
    if is_kicked(uid, gid):
        await c.answer("Вам запрещено входить в эту группу", show_alert=True)
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
    elif owner and act == "transfer":
        run("UPDATE users SET state=? WHERE user_id=?", (f"transfer:{gid}", uid))
        await bot.send_message(uid, f"👑 Напишите ник участника, которому хотите передать группу "
                                    f"«{esc(mem['title'])}».\nОтмена — /cancel")
    elif owner and act == "del":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"p:delyes:{gid}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"p:cancel:{gid}")]])
        await edit(c, f"🗑 Удалить группу «{esc(mem['title'])}»? "
                      "Все участники будут исключены. Это необратимо.", kb)
    elif owner and act == "delyes":
        ids = [r["user_id"] for r in many("SELECT user_id FROM members WHERE group_id=?", (gid,))]
        notes = {i: drop_member(i, gid) for i in ids}
        wipe_group(gid)
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


# ───────────────────────── Помощь, правила, информация ─────────────────────────
@router.message(Command("help"))
@router.message(F.text == B_HELP)
async def cmd_help(m: Message):
    await m.answer(help_text())


@router.message(Command("about"))
@router.message(F.text == B_ABOUT)
async def cmd_about(m: Message):
    await m.answer(about_text())


@router.message(Command("rules"))
async def cmd_rules(m: Message):
    await m.answer(rules_text())


# ───────────────────────── Связь с администрацией (/support) ─────────────────────────
def support_wait(uid: int) -> int:
    """Сколько секунд осталось до следующего обращения (0 — можно писать)."""
    r = one("SELECT MAX(date) AS d FROM support WHERE user_id=?", (uid,))
    if r and r["d"]:
        return max(0, r["d"] + SUPPORT_COOLDOWN - now())
    return 0


def fmt_left(sec: int) -> str:
    h, mnt = sec // 3600, (sec % 3600 + 59) // 60
    if mnt == 60:
        h, mnt = h + 1, 0
    if h and mnt:
        return f"{h} ч {mnt} мин"
    return f"{h} ч" if h else f"{max(1, mnt)} мин"


async def finish_support(m: Message, u, raw: str):
    uid = u["user_id"]
    text = (raw or "").strip()
    n = len(text)
    if n < SUPPORT_MIN or n > SUPPORT_MAX:     # остаёмся в режиме ввода — можно прислать другой текст
        run("UPDATE users SET state='support' WHERE user_id=?", (uid,))
        await m.answer(f"✍️ Обращение должно быть от {SUPPORT_MIN} до {SUPPORT_MAX} символов, у вас {n}. "
                       "Напишите ещё раз или /cancel.")
        return
    left = support_wait(uid)
    if left:
        run("UPDATE users SET state='' WHERE user_id=?", (uid,))
        await m.answer(f"⏳ Вы уже писали администрации. Следующее обращение — через {fmt_left(left)}.")
        return
    run("UPDATE users SET state='' WHERE user_id=?", (uid,))
    sid = run("INSERT INTO support(user_id, text, date) VALUES(?,?,?)", (uid, text, now())).lastrowid
    delivered = 0
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Ответить", callback_data=f"adm:rp:{sid}")]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"📩 <b>Обращение #{sid}</b>\n"
                                        f"От: <b>{esc(u['nick'])}</b> · ID <code>{uid}</code>\n\n{esc(text)}",
                                   reply_markup=reply_kb)
            delivered += 1
        except TelegramAPIError as e:
            log.warning("обращение не доставлено админу %s: %s", aid, e)
    if not delivered:                  # никому не дошло — лимит не тратим
        run("DELETE FROM support WHERE id=?", (sid,))
        await m.answer("😔 Не удалось передать обращение. Попробуйте позже.")
        return
    await m.answer(f"✅ Обращение отправлено администрации. Следующее можно будет отправить через {SUPPORT_COOLDOWN_H} ч.",
                   reply_markup=main_kb(uid))


@router.message(Command("support"))
async def cmd_support(m: Message, command: CommandObject):
    u = await reg(m)
    if not u:
        return
    if not ADMIN_IDS:
        await m.answer("📩 Связь с администрацией сейчас недоступна.")
        return
    left = support_wait(u["user_id"])
    if left:
        await m.answer(f"⏳ Вы уже писали администрации. Следующее обращение — через {fmt_left(left)}.")
        return
    if (command.args or "").strip():
        await finish_support(m, u, command.args)       # можно и сразу: /support текст обращения
        return
    run("UPDATE users SET state='support' WHERE user_id=?", (u["user_id"],))
    await m.answer(f"📩 Опишите проблему или идею одним сообщением ({SUPPORT_MIN}–{SUPPORT_MAX} символов).\n"
                   f"Администрация увидит ваш ник и ID. Писать можно 1 раз в {SUPPORT_COOLDOWN_H} ч.\nОтмена — /cancel")


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
    elif st == "support":
        await finish_support(m, u, m.text)
    elif st.startswith("rename:"):
        await finish_rename(m, u, int(st[7:]), m.text)
    elif st.startswith("desc:"):
        await finish_desc(m, u, int(st[5:]), m.text)
    elif st.startswith("transfer:"):
        await finish_transfer(m, u, int(st[9:]), m.text)


@router.message(F.text.startswith("/"))
async def unknown_command(m: Message):
    await m.answer("🤷 Неизвестная команда. Список — /help")


@router.message()
async def on_message(m: Message):
    u = await reg(m)
    if not u:
        return
    if is_banned(u):                   # в бане — молча игнорируем любые сообщения
        return
    st = u["state"]
    if st:                             # ждём текст (ник / название / описание / обращение), а прислали не текст
        if st == "nick":
            what = "ник"
        elif st == "newgroup":
            what = "название группы"
        elif st == "support":
            what = "текст обращения"
        elif st.startswith("rename:"):
            what = "новое название группы"
        elif st.startswith("desc:"):
            what = "описание группы"
        elif st.startswith("transfer:"):
            what = "ник нового владельца"
        else:
            what = "ответ текстом"
        await m.answer(f"✍️ Сейчас я жду {what} текстом. Отмена — /cancel")
        return
    if not RELAY_ON and not is_admin(u["user_id"]):
        await m.answer("⏸ Пересылка сообщений временно приостановлена администрацией. Попробуйте позже.")
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
    run("UPDATE groups SET last_active=? WHERE id=?", (now(), mem["group_id"]))   # для автоудаления неактивных
    await relay(m, u, mem)


# ───────────────────────── Скрытая админ-панель ─────────────────────────
# Команды НЕ регистрируются в BotFather и не входят в COMMANDS. Доступ — только ID из ADMIN_IDS.
# Если команду пишет не админ, бот молчит: ни «нет доступа», ни «неизвестная команда».
# admin_router подключается в диспетчер ПЕРВЫМ, поэтому перехватывает эти команды раньше основного роутера.
admin_router = Router()
admin_router.message.filter(F.chat.type == "private")
HIDDEN_CMDS = ("admin", "statistics", "ban", "unban", "find", "groups_list", "gmembers", "users", "banned",
               "activity", "settings", "set", "limit", "broadcast", "reply", "backup")
ADMIN_LIST_PAGE = 8                        # строк на страницу в списках участников / юзеров / банов
ADMIN_TITLE = "🛠 <b>Админ-панель</b>"

_bc_pending: dict = {}          # админ → текст рассылки, ждущий подтверждения
_set_pending: dict = {}         # админ → (ключ, значение), ждущие подтверждения
_bg_tasks: set = set()


def spawn(coro):
    """Запускает долгую задачу (рассылку) в фоне, чтобы не блокировать обработчик."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def is_admin(uid) -> bool:
    return uid in ADMIN_IDS


def _not_admin(m: Message) -> bool:
    return not (m.from_user and is_admin(m.from_user.id))


def _admin_state(uid: int) -> str:
    r = one("SELECT state FROM users WHERE user_id=?", (uid,))
    return (r["state"] or "") if r else ""


def admin_in_state(m: Message) -> bool:
    return bool(m.from_user and is_admin(m.from_user.id) and _admin_state(m.from_user.id).startswith("adm:"))


def admin_input_filter(m: Message) -> bool:
    return bool(m.text) and not m.text.startswith("/") and admin_in_state(m)


def set_state(uid: int, state: str):
    ensure_user(uid)
    run("UPDATE users SET state=? WHERE user_id=?", (state, uid))


def _arg_id(command: CommandObject) -> Optional[int]:
    args = (command.args or "").split()
    if args and re.fullmatch(r"\d{1,15}", args[0]):
        return int(args[0])
    return None


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M") + " UTC"


def fmt_ban(u) -> str:
    if not is_banned(u):
        return "нет"
    if u["banned_until"] >= FOREVER:
        return "навсегда"
    return f"до {fmt_ts(u['banned_until'])}"


def fmt_ago(ts: int) -> str:
    if not ts:
        return "—"
    d = max(0, now() - ts)
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн. назад"


def _count(sql: str, *args) -> int:
    return one(sql, args)["c"]


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📊 Статистика", "adm:stats"), _btn("🟢 Активность", "adm:act")],
        [_btn("🗂 Группы", "adm:gl:0"), _btn("👤 Юзеры", "adm:ul:0")],
        [_btn("🔎 Найти (ID/ник)", "adm:find"), _btn("🚫 Список банов", "adm:bl:0")],
        [_btn("⛔ Бан по ID", "adm:ban"), _btn("✅ Разбан по ID", "adm:unban")],
        [_btn("📋 Жалобы", "adm:rep"), _btn("⚙️ Настройки", "adm:cfg")],
        [_btn("📢 Рассылка", "adm:bc"), _btn("💾 Бэкап базы", "adm:bk")],
    ])


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("◀️ Меню", "adm:menu")]])


# «Код возврата» — куда вести кнопку «Назад» (в callback_data двоеточие занято, поэтому точки):
# "menu" → adm:menu, "gm.12.8" → adm:gm:12:8, "ul.0" → adm:ul:0, "bl.0" → adm:bl:0.
_CODE_RE = re.compile(r"[a-z]{1,4}(?:\.\d{1,9}){0,2}")


def _safe_code(code: str) -> str:
    return code if _CODE_RE.fullmatch(code or "") else "menu"


def _back_cb(code: str) -> str:
    return "adm:" + _safe_code(code).replace(".", ":")


def _back_kb(code: str) -> InlineKeyboardMarkup:
    """Клавиатура под результатом действия: «Назад» (туда, откуда пришли) и «Меню»."""
    code = _safe_code(code)
    if code == "menu":
        return admin_back_kb()
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("◀️ Назад", _back_cb(code))],
                                                 [_btn("◀️ Меню", "adm:menu")]])


def admin_ban_kb(target: int, with_unban: bool = False, extra_rows: Optional[list] = None,
                 back: str = "menu") -> InlineKeyboardMarkup:
    back = _safe_code(back)
    sfx = "" if back == "menu" else f":{back}"
    rows = [[_btn("♾ Навсегда", f"adm:b:{target}:0{sfx}"),
             _btn("⏱ 1 час", f"adm:b:{target}:1{sfx}"),
             _btn("📅 24 часа", f"adm:b:{target}:24{sfx}")]]
    if with_unban:
        rows.append([_btn("✅ Разбанить", f"adm:ub:{target}{sfx}")])
    rows += extra_rows or []
    if back != "menu":
        rows.append([_btn("◀️ Назад", _back_cb(back))])
    rows.append([_btn("◀️ Меню", "adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── статистика и аналитика ──
def admin_stats_text() -> str:
    t = now()
    day = t - 86400
    win = t - ACTIVE_WINDOW_MIN * 60
    users = _count("SELECT COUNT(*) AS c FROM users")
    nicked = _count("SELECT COUNT(*) AS c FROM users WHERE COALESCE(nick, '') != ''")
    banned = _count("SELECT COUNT(*) AS c FROM users WHERE banned_until>?", t)
    online = _count("SELECT COUNT(*) AS c FROM users WHERE last_seen>?", win)
    online_day = _count("SELECT COUNT(*) AS c FROM users WHERE last_seen>?", day)
    groups = _count("SELECT COUNT(*) AS c FROM groups")
    public = _count("SELECT COUNT(*) AS c FROM groups WHERE is_public=1")
    closed = _count("SELECT COUNT(*) AS c FROM groups WHERE is_closed=1")
    active_now = _count("SELECT COUNT(*) AS c FROM groups WHERE last_active>?", win)
    dialogs = _count("SELECT COUNT(*) AS c FROM groups WHERE last_active>?", day)
    reports = _count("SELECT COUNT(*) AS c FROM reports WHERE date>?", day)
    msgs = one("SELECT COALESCE(SUM(texts),0) AS t, COALESCE(SUM(media),0) AS m FROM stats")
    off = [s.short for k, s in SETTINGS.items() if s.kind == "bool" and not cfg(k)]
    return (
        "📊 <b>Статистика</b>\n\n"
        f"👤 Юзеров: {users} (с ником: {nicked})\n"
        f"🟢 Онлайн за {ACTIVE_WINDOW_MIN} мин: {online} · за сутки: {online_day}\n"
        f"🚫 Сейчас в бане: {banned}\n"
        f"🗂 Групп: {groups} (публичных: {public}, с закрытым входом: {closed})\n"
        f"🔥 Активных групп за {ACTIVE_WINDOW_MIN} мин: {active_now} · за сутки: {dialogs}\n"
        f"✉️ Сообщений всего: {msgs['t']} текст · {msgs['m']} медиа\n"
        f"📋 Жалоб за сутки: {reports}\n\n"
        f"⚙️ Лимиты: {MAX_GROUPS} групп на человека · {MAX_MEMBERS} участников в группе · "
        f"автоудаление через {INACTIVE_DAYS} дн.\n"
        f"⛔ Выключено: {', '.join(off) if off else 'ничего'}"
    )


def activity_view():
    """Кто онлайн и какие группы активны за последние ACTIVE_WINDOW_MIN минут: (текст, клавиатура)."""
    since = now() - ACTIVE_WINDOW_MIN * 60
    online = _count("SELECT COUNT(*) AS c FROM users WHERE last_seen>?", since)
    active_total = _count("SELECT COUNT(*) AS c FROM groups WHERE last_active>?", since)
    groups = many("""SELECT g.id, g.title, g.last_active,
                            (SELECT COUNT(*) FROM members m JOIN users u ON u.user_id = m.user_id
                              WHERE m.group_id = g.id AND u.active_group = g.id AND u.last_seen>?) AS online
                     FROM groups g WHERE g.last_active>? ORDER BY g.last_active DESC LIMIT 10""", (since, since))
    users = many("""SELECT u.user_id, u.nick, g.title FROM users u LEFT JOIN groups g ON g.id = u.active_group
                    WHERE u.last_seen>? ORDER BY u.last_seen DESC LIMIT 15""", (since,))
    lines = [f"🟢 <b>Активность за {ACTIVE_WINDOW_MIN} мин</b>", "",
             f"👤 Онлайн (писали боту или нажимали кнопки): <b>{online}</b>",
             f"🔥 Групп, где писали: <b>{active_total}</b>"]
    if groups:
        lines += ["", "<b>Активные группы</b>"]
        for r in groups:
            lines.append(f"<b>#{r['id']}</b> «{esc(r['title'])}» — {count_members(r['id'])}/{MAX_MEMBERS} уч. · "
                         f"{r['online']} онлайн · {fmt_ago(r['last_active'])}")
    if users:
        lines += ["", "<b>Онлайн</b>"]
        for r in users:
            grp = f" · «{esc(r['title'])}»" if r["title"] else ""
            lines.append(f"{esc(r['nick'] or '—')} · <code>{r['user_id']}</code>{grp}")
    lines += ["", f"🕒 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Обновить", "adm:act")], [_btn("◀️ Меню", "adm:menu")]])
    return "\n".join(lines)[:4000], kb


# ── пользователи ──
def admin_profile(uid: int, back: str = "menu"):
    """Профиль пользователя для админа: (текст, клавиатура) или None, если такого ID нет в базе."""
    u = one("SELECT * FROM users WHERE user_id=?", (uid,))
    if not u:
        return None
    tt, tm = personal_stats_total(uid)
    grp = many("""SELECT m.group_id, m.role, g.title FROM members m JOIN groups g ON g.id = m.group_id
                  WHERE m.user_id=? ORDER BY m.joined, m.group_id""", (uid,))
    lines = [
        f"👤 <b>Профиль</b> <code>{uid}</code>",
        f"Ник: {('<b>' + esc(u['nick']) + '</b>') if u['nick'] else '— (не задан)'}",
        f"Регистрация: {fmt_ts(u['created']) if u['created'] else '—'}",
        f"Последняя активность: {fmt_ago(u['last_seen'])}",
        f"Бан: {fmt_ban(u)}",
        f"Сообщений: {tt} текст · {tm} медиа",
        f"Жалоб на него: {reports_last_24h_on(uid)} за сутки · "
        f"{_count('SELECT COUNT(*) AS c FROM reports WHERE offender_id=?', uid)} за неделю",
        f"Жалоб от него: {_count('SELECT COUNT(*) AS c FROM reports WHERE reporter_id=?', uid)} за неделю",
        f"Групп: {len(grp)}/{MAX_GROUPS}",
    ]
    for r in grp:
        mark = " ✅" if r["group_id"] == u["active_group"] else ""
        lines.append(f"  {ROLE_ICON[r['role']]} <b>#{r['group_id']}</b> «{esc(r['title'])}» — {ROLE_NAME[r['role']]}{mark}")
    extra = [[_btn(f"👥 #{r['group_id']} «{r['title'][:18]}» — участники", f"adm:gm:{r['group_id']}:0")]
             for r in grp[:20]]
    return "\n".join(lines), admin_ban_kb(uid, with_unban=True, extra_rows=extra, back=back)


def ban_precheck(admin_id: int, target: int) -> Optional[str]:
    if target == admin_id:
        return "🙂 Нельзя забанить себя."
    if is_admin(target):
        return "🚫 Нельзя забанить другого администратора бота."
    if not one("SELECT 1 FROM users WHERE user_id=?", (target,)):
        return f"❌ Пользователь <code>{target}</code> не найден в базе."
    return None


async def do_ban(admin_id: int, target: int, hours: Optional[int]) -> str:
    """Бан по ID. hours=None — навсегда. Возвращает текст-результат для админа."""
    err = ban_precheck(admin_id, target)
    if err:
        return err
    until = FOREVER if hours is None else now() + hours * 3600
    run("UPDATE users SET banned_until=? WHERE user_id=?", (until, target))
    term = "навсегда" if hours is None else f"на {hours} ч"
    nick = one("SELECT nick FROM users WHERE user_id=?", (target,))["nick"]
    await notify(target, f"🚫 Вы забанены администрацией {term}. Если это ошибка — напишите в /support.")
    return f"🚫 <b>{esc(nick or '—')}</b> (<code>{target}</code>) забанен {term}."


async def do_unban(target: int) -> str:
    u = one("SELECT nick, banned_until FROM users WHERE user_id=?", (target,))
    if not u:
        return f"❌ Пользователь <code>{target}</code> не найден в базе."
    was = bool(u["banned_until"]) and u["banned_until"] > now()
    run("UPDATE users SET banned_until=0 WHERE user_id=?", (target,))
    run("DELETE FROM reports WHERE offender_id=?", (target,))     # чтобы старые жалобы не вернули бан сразу
    if was:
        await notify(target, "✅ Администрация сняла с вас бан. Пожалуйста, соблюдайте правила — /rules")
    return (f"✅ <b>{esc(u['nick'] or '—')}</b> (<code>{target}</code>) разбанен."
            if was else f"ℹ️ <code>{target}</code> и так не в бане (жалобы на него сброшены).")


# ── группы ──
def admin_groups_view(offset: int):
    """Страница списка групп: (текст, клавиатура)."""
    total = _count("SELECT COUNT(*) AS c FROM groups")
    if not total:
        return "🗂 Групп пока нет.", admin_back_kb()
    offset = min(max(0, offset), ((total - 1) // ADMIN_PAGE_SIZE) * ADMIN_PAGE_SIZE)
    rows = many("""SELECT g.id, g.title, g.is_public, g.is_closed, g.last_active, g.owner_id, u.nick AS owner_nick
                   FROM groups g LEFT JOIN users u ON u.user_id = g.owner_id
                   ORDER BY g.id DESC LIMIT ? OFFSET ?""", (ADMIN_PAGE_SIZE, offset))
    lines = [f"🗂 <b>Группы</b> ({total})", ""]
    kb = []
    for r in rows:
        idle = (now() - (r["last_active"] or 0)) // 86400
        flags = ("🌐 в каталоге · " if r["is_public"] else "") + ("🔒 вход закрыт · " if r["is_closed"] else "")
        lines.append(f"<b>#{r['id']}</b> «{esc(r['title'])}» — {count_members(r['id'])}/{MAX_MEMBERS}\n"
                     f"{flags}владелец: {esc(r['owner_nick'] or '—')} (<code>{r['owner_id']}</code>) · "
                     f"без сообщений: {idle} дн.")
        kb.append([_btn(f"👥 Участники #{r['id']}", f"adm:gm:{r['id']}:0")])
        kb.append([
            _btn(f"{'🔓 Открыть' if r['is_closed'] else '🔒 Закрыть'} #{r['id']}", f"adm:gc:{r['id']}:{offset}"),
            _btn(f"🗑 Удалить #{r['id']}", f"adm:gd:{r['id']}:{offset}")])
    nav = []
    if offset > 0:
        nav.append(_btn("◀️ Назад", f"adm:gl:{max(0, offset - ADMIN_PAGE_SIZE)}"))
    if offset + ADMIN_PAGE_SIZE < total:
        nav.append(_btn("Дальше ▶️", f"adm:gl:{offset + ADMIN_PAGE_SIZE}"))
    if nav:
        kb.append(nav)
    kb.append([_btn("◀️ Меню", "adm:menu")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def admin_reports_text() -> str:
    rows = many("""SELECT r.id, r.date, r.reporter_id, r.offender_id, r.group_id,
                          ur.nick AS rnick, uo.nick AS onick, g.title
                   FROM reports r
                   LEFT JOIN users ur ON ur.user_id = r.reporter_id
                   LEFT JOIN users uo ON uo.user_id = r.offender_id
                   LEFT JOIN groups g ON g.id = r.group_id
                   ORDER BY r.id DESC LIMIT 20""")
    if not rows:
        return "📋 Жалоб пока нет."
    lines = ["📋 <b>Последние жалобы</b> (до 20)", ""]
    for r in rows:
        when = datetime.fromtimestamp(r["date"], timezone.utc).strftime("%d.%m %H:%M")
        grp = f"«{esc(r['title'])}»" if r["title"] else f"#{r['group_id']} (удалена)"
        lines.append(f"<b>#{r['id']}</b> · {when} UTC\n"
                     f"{esc(r['rnick'] or '—')} (<code>{r['reporter_id']}</code>) → "
                     f"{esc(r['onick'] or '—')} (<code>{r['offender_id']}</code>) · {grp}")
    return "\n\n".join(lines)[:4000]


# ── участники групп, все юзеры, баны (с ID) ──
def resolve_user(raw: str) -> Optional[int]:
    """ID (если такой юзер есть в базе) или ник → user_id. Иначе None."""
    raw = (raw or "").strip().lstrip("@")
    if re.fullmatch(r"\d{1,15}", raw):
        r = one("SELECT user_id FROM users WHERE user_id=?", (int(raw),))
        if r:
            return r["user_id"]
    if not raw:
        return None
    r = one("SELECT user_id FROM users WHERE nick_lc=?", (raw.lower(),))
    return r["user_id"] if r else None


def _arg_user(command: CommandObject) -> Optional[int]:
    args = (command.args or "").split()
    return resolve_user(args[0]) if args else None


def _nav_row(prefix: str, offset: int, total: int) -> list:
    nav = []
    if offset > 0:
        nav.append(_btn("◀️ Назад", f"{prefix}:{max(0, offset - ADMIN_LIST_PAGE)}"))
    if offset + ADMIN_LIST_PAGE < total:
        nav.append(_btn("Дальше ▶️", f"{prefix}:{offset + ADMIN_LIST_PAGE}"))
    return nav


def _clamp(offset: int, total: int) -> int:
    return min(max(0, offset), ((max(total, 1) - 1) // ADMIN_LIST_PAGE) * ADMIN_LIST_PAGE)


def _user_row(r, back: str, unban: bool = False) -> list:
    """Строка кнопок под пользователем: профиль + «Бан» (или «Разбан» в списке банов)."""
    uid = r["user_id"]
    row = [_btn(f"👤 {(r['nick'] or '—')[:16]}", f"adm:pf:{uid}:{back}")]
    if unban:
        row.append(_btn("✅ Разбан", f"adm:ubl:{uid}:{back}"))
    elif not is_admin(uid):
        row.append(_btn("🚫 Бан", f"adm:bp:{uid}:{back}"))
    return row


def admin_members_view(gid: int, offset: int = 0):
    """Участники группы с ID и кнопками бана: (текст, клавиатура)."""
    g = one("SELECT id, title FROM groups WHERE id=?", (gid,))
    if not g:
        return "❌ Группа не найдена (возможно, уже удалена).", InlineKeyboardMarkup(
            inline_keyboard=[[_btn("🗂 К группам", "adm:gl:0")], [_btn("◀️ Меню", "adm:menu")]])
    total = count_members(gid)
    offset = _clamp(offset, total)
    rows = many("""SELECT u.user_id, u.nick, u.banned_until, u.last_seen, m.role, m.muted_until
                   FROM members m JOIN users u ON u.user_id = m.user_id
                   WHERE m.group_id=?
                   ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
                                        WHEN 'moderator' THEN 2 ELSE 3 END, u.nick_lc
                   LIMIT ? OFFSET ?""", (gid, ADMIN_LIST_PAGE, offset))
    lines = [f"👥 <b>#{gid}</b> «{esc(g['title'])}» — участники ({total})", ""]
    kb = []
    for r in rows:
        flags = (" 🚫" if is_banned(r) else "") + (" 🔇" if r["muted_until"] > now() else "")
        lines.append(f"{ROLE_ICON[r['role']]} <b>{esc(r['nick'] or '—')}</b> · <code>{r['user_id']}</code>{flags}"
                     f" · {fmt_ago(r['last_seen'])}")
        kb.append(_user_row(r, f"gm.{gid}.{offset}"))
    if not rows:
        lines.append("В группе никого нет.")
    nav = _nav_row(f"adm:gm:{gid}", offset, total)
    if nav:
        kb.append(nav)
    kb.append([_btn("🗂 К группам", "adm:gl:0"), _btn("◀️ Меню", "adm:menu")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=kb)


def admin_users_view(offset: int = 0):
    """Все пользователи бота (по последней активности) с ID: (текст, клавиатура)."""
    total = _count("SELECT COUNT(*) AS c FROM users")
    if not total:
        return "👤 Пользователей пока нет.", admin_back_kb()
    offset = _clamp(offset, total)
    rows = many("""SELECT user_id, nick, banned_until, last_seen FROM users
                   ORDER BY last_seen DESC, user_id DESC LIMIT ? OFFSET ?""", (ADMIN_LIST_PAGE, offset))
    lines = [f"👤 <b>Юзеры</b> ({total}) — по последней активности", ""]
    kb = []
    for r in rows:
        flag = " 🚫" if is_banned(r) else ""
        lines.append(f"<b>{esc(r['nick'] or '— (без ника)')}</b> · <code>{r['user_id']}</code>{flag}"
                     f" · {fmt_ago(r['last_seen'])}")
        kb.append(_user_row(r, f"ul.{offset}"))
    nav = _nav_row("adm:ul", offset, total)
    if nav:
        kb.append(nav)
    kb.append([_btn("🔎 Найти (ID/ник)", "adm:find"), _btn("◀️ Меню", "adm:menu")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=kb)


def admin_banned_view(offset: int = 0):
    """Сейчас забаненные: (текст, клавиатура) — с кнопкой разбана."""
    total = _count("SELECT COUNT(*) AS c FROM users WHERE banned_until>?", now())
    if not total:
        return "🚫 Сейчас в бане никого нет.", admin_back_kb()
    offset = _clamp(offset, total)
    rows = many("""SELECT user_id, nick, banned_until, last_seen FROM users WHERE banned_until>?
                   ORDER BY banned_until DESC LIMIT ? OFFSET ?""", (now(), ADMIN_LIST_PAGE, offset))
    lines = [f"🚫 <b>В бане</b> ({total})", ""]
    kb = []
    for r in rows:
        lines.append(f"<b>{esc(r['nick'] or '—')}</b> · <code>{r['user_id']}</code> · {fmt_ban(r)}")
        kb.append(_user_row(r, f"bl.{offset}", unban=True))
    nav = _nav_row("adm:bl", offset, total)
    if nav:
        kb.append(nav)
    kb.append([_btn("◀️ Меню", "adm:menu")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=kb)


# ── настройки прямо из бота ──
def fmt_setting(key: str, val: int) -> str:
    return ("вкл" if val else "выкл") if SETTINGS[key].kind == "bool" else str(val)


def settings_view():
    lines = ["⚙️ <b>Настройки бота</b>",
             "Меняются сразу, без перезапуска. Если уменьшить лимит, уже существующие группы и участники "
             "остаются — он лишь не пускает новых.", ""]
    kb = []
    for key, s in SETTINGS.items():
        val = cfg(key)
        lines.append(f"• {s.label}: <b>{fmt_setting(key, val)}</b>")
        if s.kind == "bool":
            kb.append([_btn(f"{'✅' if val else '⛔'} {s.short}", f"adm:st:{key}")])
        else:
            kb.append([_btn(f"✏️ {s.short}: {val}", f"adm:se:{key}")])
    kb.append([_btn("◀️ Меню", "adm:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


def settings_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⚙️ Настройки", "adm:cfg"), _btn("◀️ Меню", "adm:menu")]])


def parse_setting(key: str, raw: str):
    """Разбирает введённое значение. Возвращает (число, ошибка)."""
    s = SETTINGS[key]
    raw = (raw or "").strip().lower()
    if s.kind == "bool":
        if raw in ("1", "on", "вкл", "да"):
            return 1, None
        if raw in ("0", "off", "выкл", "нет"):
            return 0, None
        return None, "❌ Для переключателя нужно 1/0 (или вкл/выкл)."
    if not re.fullmatch(r"\d{1,9}", raw):
        return None, "❌ Нужно целое число."
    val = int(raw)
    if not s.lo <= val <= s.hi:
        return None, f"❌ Допустимо от {s.lo} до {s.hi}."
    return val, None


def commit_setting(key: str, val: int) -> str:
    old = cfg(key)
    save_setting(key, val)
    return f"✅ {SETTINGS[key].label}: {fmt_setting(key, old)} → <b>{fmt_setting(key, val)}</b>"


def apply_setting(admin_id: int, key: str, val: int):
    """Применяет настройку. Опасное изменение (автоудаление групп) сначала просит подтверждения.
    Возвращает (текст, клавиатура)."""
    if key == "INACTIVE_DAYS" and val < INACTIVE_DAYS:
        n = _count("SELECT COUNT(*) AS c FROM groups WHERE COALESCE(last_active,0)<?", now() - val * 86400)
        if n:
            _set_pending[admin_id] = (key, val)
            kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Применить", "adm:sc:y"), _btn("↩️ Отмена", "adm:sc:n")]])
            return (f"⚠️ При сроке {val} дн. ближайшая чистка (раз в час) удалит групп: <b>{n}</b> — "
                    "вместе с участниками, необратимо. Применить?"), kb
    return commit_setting(key, val), settings_back_kb()


# ── рассылка, ответы на обращения, бэкап ──
def broadcast_ids() -> list:
    rows = many("SELECT user_id FROM users WHERE COALESCE(nick,'')!='' AND COALESCE(banned_until,0)<=?", (now(),))
    return [r["user_id"] for r in rows]


def broadcast_preview(admin_id: int, text: str):
    """Готовит подтверждение рассылки: (текст, клавиатура)."""
    _bc_pending[admin_id] = text
    n = len(broadcast_ids())
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn(f"📢 Отправить ({n})", "adm:bcy"), _btn("↩️ Отмена", "adm:bcn")]])
    return f"📢 <b>Разослать всем ({n} чел.)?</b>\n\n{esc(text)}", kb


async def do_broadcast(admin_id: int, text: str):
    body = f"📢 <b>Сообщение от администрации</b>\n\n{esc(text)}"
    sent = failed = 0
    for uid in broadcast_ids():
        try:
            try:
                await bot.send_message(uid, body)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.send_message(uid, body)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await notify(admin_id, f"📢 Рассылка завершена: доставлено {sent}, не доставлено {failed}.")


async def send_reply(sid: int, text: str) -> str:
    r = one("SELECT user_id FROM support WHERE id=?", (sid,))
    if not r:
        return "❌ Обращение не найдено (записи хранятся 30 дней)."
    try:
        await bot.send_message(r["user_id"], f"📩 <b>Ответ администрации</b> на обращение #{sid}:\n\n{esc(text)}")
    except TelegramAPIError:
        return "😔 Не удалось доставить: человек, возможно, заблокировал бота."
    return f"✅ Ответ на обращение #{sid} отправлен."


async def send_backup(chat_id: int):
    """Присылает копию базы. Снимок делается штатным backup-API SQLite — он безопасен при работающем боте."""
    path = os.path.join(tempfile.gettempdir(), f"anon_groups_{now()}.db")
    dst = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()
    try:
        await bot.send_document(chat_id, FSInputFile(path), caption="💾 Копия базы. Внутри данные пользователей — храните в тайне.")
    except TelegramAPIError as e:
        await notify(chat_id, f"😔 Не удалось отправить файл: {esc(str(e))}")
    finally:
        os.remove(path)


# ── тихий отказ для не-админов: обработчик «съедает» команду, ничего не отвечая ──
@admin_router.message(Command(*HIDDEN_CMDS, ignore_case=True), _not_admin)
async def admin_silent(m: Message):
    return


@admin_router.message(Command("cancel"), admin_in_state)
async def adm_cancel(m: Message):
    set_state(m.from_user.id, "")
    await m.answer("Отменено.", reply_markup=admin_back_kb())


@admin_router.message(F.text, admin_input_filter)
async def adm_input(m: Message):
    """Админ прислал текст после кнопки в меню: ID, значение настройки, текст рассылки или ответа."""
    uid = m.from_user.id
    parts = _admin_state(uid).split(":")
    kind, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    raw = m.text.strip()

    if kind in ("ban", "unban", "find"):
        if kind == "find":
            target = resolve_user(raw)
            if target is None:
                await m.answer("❌ Не нашёл такого ID или ника. Отправьте ещё раз или /cancel.")
                return
        elif not re.fullmatch(r"\d{1,15}", raw):
            await m.answer("❌ Нужен числовой ID пользователя. Отправьте ещё раз или /cancel.")
            return
        else:
            target = int(raw)
        set_state(uid, "")
        if kind == "ban":
            err = ban_precheck(uid, target)
            if err:
                await m.answer(err, reply_markup=admin_back_kb())
            else:
                await m.answer(f"На какой срок забанить <code>{target}</code>?", reply_markup=admin_ban_kb(target))
        elif kind == "unban":
            await m.answer(await do_unban(target), reply_markup=admin_back_kb())
        else:
            prof = admin_profile(target)
            if prof:
                await m.answer(prof[0], reply_markup=prof[1])
            else:
                await m.answer(f"❌ Пользователь <code>{target}</code> не найден в базе.", reply_markup=admin_back_kb())
    elif kind == "set" and arg in SETTINGS:
        val, err = parse_setting(arg, raw)
        if err:
            await m.answer(f"{err}\nОтправьте ещё раз или /cancel.")
            return
        set_state(uid, "")
        text, kb = apply_setting(uid, arg, val)
        await m.answer(text, reply_markup=kb)
    elif kind == "bc":
        if len(raw) > 3500:
            await m.answer("✂️ Слишком длинно: максимум 3500 символов. Сократите и отправьте ещё раз или /cancel.")
            return
        set_state(uid, "")
        text, kb = broadcast_preview(uid, raw)
        await m.answer(text, reply_markup=kb)
    elif kind == "rp" and arg.isdigit():
        set_state(uid, "")
        await m.answer(await send_reply(int(arg), raw), reply_markup=admin_back_kb())
    else:
        set_state(uid, "")
        await m.answer("Кнопка устарела — откройте /admin заново.")


@admin_router.message(Command("admin", ignore_case=True))
async def adm_menu(m: Message):
    set_state(m.from_user.id, "")
    await m.answer(ADMIN_TITLE, reply_markup=admin_menu_kb())


@admin_router.message(Command("statistics", ignore_case=True))
async def adm_statistics(m: Message):
    set_state(m.from_user.id, "")
    await m.answer(admin_stats_text(), reply_markup=admin_back_kb())


@admin_router.message(Command("activity", ignore_case=True))
async def adm_activity_cmd(m: Message):
    set_state(m.from_user.id, "")
    text, kb = activity_view()
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("ban", ignore_case=True))
async def adm_ban_cmd(m: Message, command: CommandObject):
    set_state(m.from_user.id, "")
    args = (command.args or "").split()
    target = _arg_id(command)
    hours = None
    if len(args) > 1:
        if not args[1].isdigit() or int(args[1]) < 1:
            target = None
        else:
            hours = min(int(args[1]), MAX_BAN_HOURS)
    if target is None:
        await m.answer("Использование: <code>/ban ID [часы]</code> — без часов бан навсегда.")
        return
    await m.answer(await do_ban(m.from_user.id, target, hours))


@admin_router.message(Command("unban", ignore_case=True))
async def adm_unban_cmd(m: Message, command: CommandObject):
    set_state(m.from_user.id, "")
    target = _arg_id(command)
    if target is None:
        await m.answer("Использование: <code>/unban ID</code>")
        return
    await m.answer(await do_unban(target))


@admin_router.message(Command("find", ignore_case=True))
async def adm_find_cmd(m: Message, command: CommandObject):
    set_state(m.from_user.id, "")
    target = _arg_user(command)
    if target is None:
        await m.answer("Использование: <code>/find ID_или_ник</code>. Если ничего не нашлось — "
                       "такого пользователя нет в базе.")
        return
    prof = admin_profile(target)
    await m.answer(prof[0], reply_markup=prof[1])


@admin_router.message(Command("groups_list", ignore_case=True))
async def adm_groups_cmd(m: Message):
    set_state(m.from_user.id, "")
    text, kb = admin_groups_view(0)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("gmembers", ignore_case=True))
async def adm_gmembers_cmd(m: Message, command: CommandObject):
    """/gmembers ID_ГРУППЫ — участники группы с ID и кнопками бана (ID группы — из /groups_list)."""
    set_state(m.from_user.id, "")
    gid = _arg_id(command)
    if gid is None:
        await m.answer("Использование: <code>/gmembers ID_группы</code> (номер — из /groups_list, «#12»)")
        return
    text, kb = admin_members_view(gid, 0)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("users", ignore_case=True))
async def adm_users_cmd(m: Message):
    set_state(m.from_user.id, "")
    text, kb = admin_users_view(0)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("banned", ignore_case=True))
async def adm_banned_cmd(m: Message):
    set_state(m.from_user.id, "")
    text, kb = admin_banned_view(0)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("settings", ignore_case=True))
async def adm_settings_cmd(m: Message):
    set_state(m.from_user.id, "")
    text, kb = settings_view()
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("set", ignore_case=True))
async def adm_set_cmd(m: Message, command: CommandObject):
    """/set КЛЮЧ ЗНАЧЕНИЕ — то же, что кнопки в настройках."""
    set_state(m.from_user.id, "")
    args = (command.args or "").split()
    if len(args) != 2 or args[0].upper() not in SETTINGS:
        keys = "\n".join(f"<code>{k}</code> — {esc(s.label)} (сейчас {fmt_setting(k, cfg(k))})"
                         for k, s in SETTINGS.items())
        await m.answer("Использование: <code>/set КЛЮЧ ЗНАЧЕНИЕ</code>\n\n" + keys)
        return
    key = args[0].upper()
    val, err = parse_setting(key, args[1])
    if err:
        await m.answer(err)
        return
    text, kb = apply_setting(m.from_user.id, key, val)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("limit", ignore_case=True))
async def adm_limit_cmd(m: Message, command: CommandObject):
    """/limit N — быстро изменить лимит групп на одного человека."""
    set_state(m.from_user.id, "")
    arg = (command.args or "").strip()
    if not arg:
        await m.answer(f"Лимит групп на человека: <b>{MAX_GROUPS}</b>\nУчастников в группе: <b>{MAX_MEMBERS}</b>\n\n"
                       "Изменить: <code>/limit 8</code> — групп на человека; "
                       "<code>/set MAX_MEMBERS 100</code> — участников в группе.")
        return
    val, err = parse_setting("MAX_GROUPS", arg)
    if err:
        await m.answer(err)
        return
    text, kb = apply_setting(m.from_user.id, "MAX_GROUPS", val)
    await m.answer(text, reply_markup=kb)


@admin_router.message(Command("broadcast", ignore_case=True))
async def adm_broadcast_cmd(m: Message, command: CommandObject):
    uid = m.from_user.id
    text = (command.args or "").strip()
    if not text:
        set_state(uid, "adm:bc")
        await m.answer("📢 Отправьте текст рассылки одним сообщением (до 3500 символов). Отмена — /cancel")
        return
    set_state(uid, "")
    if len(text) > 3500:
        await m.answer("✂️ Слишком длинно: максимум 3500 символов.")
        return
    out, kb = broadcast_preview(uid, text)
    await m.answer(out, reply_markup=kb)


@admin_router.message(Command("reply", ignore_case=True))
async def adm_reply_cmd(m: Message, command: CommandObject):
    """/reply НОМЕР_ОБРАЩЕНИЯ текст — ответ человеку, написавшему в /support."""
    set_state(m.from_user.id, "")
    parts = (command.args or "").split(None, 1)
    if len(parts) < 2 or not parts[0].lstrip("#").isdigit():
        await m.answer("Использование: <code>/reply НОМЕР текст</code> (номер — из «Обращение #…»)")
        return
    await m.answer(await send_reply(int(parts[0].lstrip("#")), parts[1].strip()))


@admin_router.message(Command("backup", ignore_case=True))
async def adm_backup_cmd(m: Message):
    set_state(m.from_user.id, "")
    await send_backup(m.chat.id)


@admin_router.callback_query(F.data.startswith("adm:"))
async def adm_cb(c: CallbackQuery):
    uid = c.from_user.id
    if not is_admin(uid):              # не админ — молча ничего не делаем
        await c.answer()
        return
    parts = c.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    try:
        if act == "menu":
            set_state(uid, "")
            await edit(c, ADMIN_TITLE, admin_menu_kb())
        elif act == "stats":
            await edit(c, admin_stats_text(), admin_back_kb())
        elif act == "act":
            text, kb = activity_view()
            await edit(c, text, kb)
        elif act in ("ban", "unban", "find"):
            set_state(uid, f"adm:{act}")
            if act == "find":
                await c.message.answer("Отправьте ID или ник пользователя. Отмена — /cancel")
            else:
                what = {"ban": "забанить", "unban": "разбанить"}[act]
                await c.message.answer(f"Отправьте ID пользователя, которого нужно {what}. Отмена — /cancel")
        elif act == "b":
            hours = int(parts[3]) or None
            back = _safe_code(parts[4] if len(parts) > 4 else "menu")
            await edit(c, await do_ban(uid, int(parts[2]), hours), _back_kb(back))
        elif act == "ub":
            back = _safe_code(parts[3] if len(parts) > 3 else "menu")
            await edit(c, await do_unban(int(parts[2])), _back_kb(back))
        elif act == "gm":                                   # участники группы
            text, kb = admin_members_view(int(parts[2]), int(parts[3]) if len(parts) > 3 else 0)
            await edit(c, text, kb)
        elif act == "ul":                                   # все юзеры
            text, kb = admin_users_view(int(parts[2]) if len(parts) > 2 else 0)
            await edit(c, text, kb)
        elif act == "bl":                                   # список банов
            text, kb = admin_banned_view(int(parts[2]) if len(parts) > 2 else 0)
            await edit(c, text, kb)
        elif act == "pf":                                   # профиль юзера
            back = _safe_code(parts[3] if len(parts) > 3 else "menu")
            prof = admin_profile(int(parts[2]), back)
            if prof:
                await edit(c, prof[0], prof[1])
            else:
                await edit(c, "❌ Пользователь не найден.", _back_kb(back))
        elif act == "bp":                                   # «Бан» из списка: выбор срока
            target = int(parts[2])
            back = _safe_code(parts[3] if len(parts) > 3 else "menu")
            err = ban_precheck(uid, target)
            if err:
                await edit(c, err, _back_kb(back))
            else:
                u = one("SELECT nick, banned_until FROM users WHERE user_id=?", (target,))
                await edit(c, f"🚫 Забанить <b>{esc(u['nick'] or '—')}</b> (<code>{target}</code>)?\n"
                              f"Сейчас бан: {fmt_ban(u)}\nНа какой срок?", admin_ban_kb(target, back=back))
        elif act == "ubl":                                  # «Разбан» из списка банов
            await do_unban(int(parts[2]))
            code = _safe_code(parts[3] if len(parts) > 3 else "bl.0")
            off = int(code.split(".")[1]) if code.startswith("bl.") else 0
            text, kb = admin_banned_view(off)
            await edit(c, text, kb)
        elif act == "gl":
            text, kb = admin_groups_view(int(parts[2]))
            await edit(c, text, kb)
        elif act == "gc":                                   # открыть / закрыть вход
            gid, off = int(parts[2]), int(parts[3])
            run("UPDATE groups SET is_closed=1-is_closed WHERE id=?", (gid,))
            text, kb = admin_groups_view(off)
            await edit(c, text, kb)
        elif act == "gd":                                   # удаление — сначала подтверждение
            gid, off = int(parts[2]), int(parts[3])
            g = one("SELECT title FROM groups WHERE id=?", (gid,))
            if not g:
                text, kb = admin_groups_view(off)
                await edit(c, text, kb)
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    _btn("✅ Да, удалить", f"adm:gdy:{gid}:{off}"), _btn("↩️ Отмена", f"adm:gl:{off}")]])
                await edit(c, f"🗑 Удалить группу <b>#{gid}</b> «{esc(g['title'])}»? "
                              f"Участников: {count_members(gid)}. Все они будут исключены. Это необратимо.", kb)
        elif act == "gdy":
            gid, off = int(parts[2]), int(parts[3])
            await purge_group(gid, "удалена администрацией бота")
            text, kb = admin_groups_view(off)
            await edit(c, text, kb)
        elif act == "rep":
            await edit(c, admin_reports_text(), admin_back_kb())
        elif act == "cfg":
            text, kb = settings_view()
            await edit(c, text, kb)
        elif act == "se":                                   # число: просим прислать новое значение
            key = parts[2]
            s = SETTINGS[key]
            set_state(uid, f"adm:set:{key}")
            kb = InlineKeyboardMarkup(inline_keyboard=[[_btn(f"↩️ По умолчанию ({s.default})", f"adm:sr:{key}")]])
            await c.message.answer(f"✏️ <b>{esc(s.label)}</b>\nСейчас: <b>{cfg(key)}</b>. Допустимо: {s.lo}–{s.hi}.\n"
                                   "Отправьте новое число или /cancel.", reply_markup=kb)
        elif act == "st":                                   # переключатель: вкл ↔ выкл
            key = parts[2]
            if SETTINGS[key].kind == "bool":
                save_setting(key, 1 - cfg(key))
            text, kb = settings_view()
            await edit(c, text, kb)
        elif act == "sr":                                   # вернуть значение по умолчанию
            key = parts[2]
            set_state(uid, "")
            text, kb = apply_setting(uid, key, SETTINGS[key].default)
            await c.message.answer(text, reply_markup=kb)
        elif act == "sc":                                   # подтверждение опасной настройки
            pend = _set_pending.pop(uid, None)
            if parts[2] == "y" and pend:
                await edit(c, commit_setting(*pend), settings_back_kb())
            else:
                await edit(c, "Отменено.", settings_back_kb())
        elif act == "bc":
            set_state(uid, "adm:bc")
            await c.message.answer("📢 Отправьте текст рассылки одним сообщением (до 3500 символов). Отмена — /cancel")
        elif act == "bcy":
            text = _bc_pending.pop(uid, None)
            if not text:
                await edit(c, "Рассылка не найдена — начните заново.", admin_back_kb())
            else:
                await edit(c, "📢 Рассылка запущена — пришлю итог, когда закончу.", admin_back_kb())
                spawn(do_broadcast(uid, text))
        elif act == "bcn":
            _bc_pending.pop(uid, None)
            await edit(c, "Отменено.", admin_back_kb())
        elif act == "rp":                                   # ответить на обращение из /support
            sid = int(parts[2])
            set_state(uid, f"adm:rp:{sid}")
            await c.message.answer(f"↩️ Напишите ответ на обращение #{sid} одним сообщением. Отмена — /cancel")
        elif act == "bk":
            await send_backup(uid)
    except (ValueError, IndexError, KeyError):
        await c.answer("Кнопка устарела — откройте /admin заново", show_alert=True)
        return
    await c.answer()


# ───────────────────────── Запуск ─────────────────────────
async def cleanup_loop():
    while True:
        try:
            t = now()
            run("DELETE FROM relay WHERE ts<?", (t - RELAY_TTL,))
            run("DELETE FROM reports WHERE date<?", (t - REPORT_TTL,))
            run("DELETE FROM support WHERE date<?", (t - SUPPORT_TTL,))
            run("DELETE FROM nick_changes WHERE date<?", (t - NICK_CHANGE_WINDOW,))
            _seen_cache.clear()
            # группы без сообщений INACTIVE_DAYS дней удаляются вместе с участниками
            for g in many("SELECT id FROM groups WHERE COALESCE(last_active,0)<?", (t - INACTIVE_TTL,)):
                await purge_group(g["id"], f"удалена из-за неактивности ({INACTIVE_DAYS} дней без сообщений)")
        except Exception:
            log.exception("ошибка в cleanup_loop")
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
    dp.message.outer_middleware(SeenMiddleware())            # «онлайн» для админ-аналитики
    dp.callback_query.outer_middleware(SeenMiddleware())
    dp.include_router(admin_router)     # первым: скрытые админ-команды перехватываются раньше основных
    dp.include_router(router)
    asyncio.create_task(cleanup_loop())
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — админ-панель и /support недоступны")
    log.info("Бот @%s запущен (админов: %d)", BOT_USERNAME, len(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
