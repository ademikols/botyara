import asyncio
import importlib
import random
import sys
import time
from html import escape as esc

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ───────────────────────── Связь с main.py ─────────────────────────
# Этот модуль НЕ импортирует main.py при загрузке — иначе получился бы циклический импорт
# (main.py импортирует games_router отсюда) и, что хуже, при запуске «python main.py» файл
# main.py загрузился бы второй раз под именем "main" (вторая копия базы, bot = None и т.д.).
#
# Вместо этого `main` — ленивый прокси: при первом обращении к main.<что-то> (уже внутри хендлера,
# когда бот полностью запущен) он находит РАБОТАЮЩИЙ модуль:
#   • если бот запущен как «python main.py» — это sys.modules["__main__"];
#   • если main.py импортирован как модуль "main" — обычный import.
# Весь остальной код ниже обращается к функциям main так же, как раньше: main.one(...), main.tag(...).
_main_mod = None


def _get_main():
    global _main_mod
    if _main_mod is None:
        mod = sys.modules.get("__main__")
        if not (mod and hasattr(mod, "active_recipients") and hasattr(mod, "get_member")):
            mod = importlib.import_module("main")
        _main_mod = mod
    return _main_mod


class _MainProxy:
    def __getattr__(self, name):
        return getattr(_get_main(), name)


main = _MainProxy()

# ВАЖНО: games_router нужно подключать в диспетчер РАНЬШЕ основного router из main.py
# (см. main(): dp.include_router(games_router) стоит перед dp.include_router(router)),
# иначе «/games» и кнопку «🎮 Игры» перехватят unknown_command и on_message из main.py.
games_router = Router()
games_router.message.filter(F.chat.type == "private")

MAX_PLAYERS = 15
MIN_PLAYERS = 3
GAMES_BTN = "🎮 Игры"                      # текст кнопки (если её добавят в меню) — тоже открывает игры
LOBBY_TTL = 15 * 60                        # лобби, которое никто не запустил, закрывается через 15 минут
SEND_DELAY = 0.04                          # пауза между отправками (как в main.py, чтобы не упереться в лимиты Telegram)
TG_TEXT_LIMIT = 4000                       # запас до лимита Telegram в 4096 символов

# Расширенные наборы слов для игры "Шпион"
SPY_PACKS = {
    "clash": {
        "name": "👑 Clash Royale (Все карты)",
        "words": [

            # --- Обычные ---
            "Скелеты", "Ледяной дух", "Огненный дух", "Электрический дух", "Дух исцеления",
            "Разряд", "Гигантский снежок", "Стрелы", "Гоблины", "Гоблины-копейщики", "Подрывник",
            "Летучие мыши", "Миньоны", "Банда гоблинов", "Пушка", "Тесла", "Мортира",
            "Варвары", "Элитные варвары", "Королевские рекруты", "Королевский гигант",
            "Орда миньонов", "Бочка со скелетами", "Гоблин с дротиками", "Костяные драконы",
            "Рыцарь", "Лучницы", "Огненная лучница", "Королевская почта", "Разбойники",
            "Подозрительный куст",

            # --- Редкие ---
            "Мегаминьон", "Всадник на кабане", "Гигант", "Валькирия", "Мушкетёр", "Колдун",
            "Мини-П.Е.К.К.А.", "Боевой таран", "Ледяной голем", "Три мушкетёра",
            "Огненный шар", "Ракета", "Землетрясение", "Печь", "Хижина гоблинов",
            "Хижина варваров", "Сборщик эликсира", "Адская башня", "Целительница-воин",
            "Эликсирный голем", "Гоблинский бур", "Башня-бомбёжка", "Королевские кабаны",
            "Летучка", "Клетка с гоблином", "Надгробие", "Гоблин-подрывник",

            # --- Эпические ---
            "Гоблинская бочка", "Стражи", "Армия скелетов", "Зеркало", "Клон",
            "Заморозка", "Молния", "Торнадо", "Стенобои", "Дракончик", "Ведьма",
            "Вышибала", "П.Е.К.К.А.", "Гигантский скелет", "Шар", "Принц", "Тёмный принц",
            "Охотник", "Палач", "Повозка с пушкой", "Электродракон", "Арбалет",
            "Гоблин-гигант", "Голем", "Яд", "Варварская бочка", "Электрогигант",
            "Ярость", "Пустота", "Проклятие гоблинов", "Гоблинская машина",

            # --- Легендарные ---
            "Бревно", "Принцесса", "Шахтёр", "Ледяной колдун", "Пламенный дракон",
            "Громовержец", "Бандитка", "Ночная ведьма", "Магический лучник", "Кладбище",
            "Спарки", "Мегарыцарь", "Всадница на баране", "Королевский призрак", "Рыбак",
            "Феникс", "Ведьмина бабушка", "Дровосек", "Адская гончая",

            # --- Чемпионы ---
            "Золотой рыцарь", "Королева лучниц", "Король скелетов", "Монах",
            "Шустрый шахтёр", "Маленький Принц"
        ]
    },
    "mc": {
        "name": "Майнкрафт (Все мобы)",
        "words": [
            "Стив", "Алекс", "Житель", "Бродячий торговец", "Аксолотль", "Броненосец", "Тихоня (Allay)",
            "Пчела", "Верблюд", "Кошка", "Курица", "Треска", "Корова", "Осел", "Лягушка", "Светящийся кальмар",
            "Лошадь", "Грибная корова", "Мул", "Оцелот", "Попугай", "Свинья", "Кролик", "Лосось", "Овца",
            "Лошадь-скелет", "Нюхач (Sniffer)", "Снежный голем", "Кальмар", "Лавомерка (Strider)",
            "Головастик", "Черепаха", "Дельфин", "Эндермен", "Лиса", "Коза", "Железный голем", "Лама",
            "Панда", "Белый медведь", "Ифрит (Блейз)", "Вязнущий (Bogged)", "Бриз (Breeze)", "Скрипун (Creaking)",
            "Крипер", "Древний страж", "Эндермит", "Вызыватель", "Гаст", "Страж", "Хоглин", "Кадавр",
            "Магма-куб", "Фантом", "Пиглин", "Пиглин-брут", "Разбойник", "Разоритель", "Шалкер",
            "Чешуйница", "Скелет", "Слайм", "Зимогор", "Вредность (Vex)", "Поборник", "Хранитель (Warden)",
            "Ведьма", "Скелет-иссушитель", "Зоглин", "Зомби", "Зомби-житель", "Зомбифицированный пиглин",
            "Дракон Края", "Иссушитель", "Паук", "Пещерный паук", "Утопленник", "Летучая мышь", "Иглобрюх"
        ]
    },
    "foot": {
        "name": "⚽️ Топ-100 Футболистов",
        "words": [
            "Лионель Месси", "Криштиану Роналду", "Килиан Мбаппе", "Эрлинг Холанд", "Винисиус Жуниор",
            "Ламин Ямаль", "Джуд Беллингем", "Кевин Де Брюйне", "Родри", "Гарри Кейн", "Мохамед Салах",
            "Джамал Мусиала", "Флориан Виртц", "Роберт Левандовски", "Неймар", "Букайо Сака", "Фил Фоден",
            "Антуан Гризманн", "Коул Палмер", "Федерико Вальверде", "Педри", "Гави", "Лаутаро Мартинес",
            "Виктор Осимхен", "Бернарду Силва", "Бруну Фернандеш", "Рубен Диаш", "Трент Александер-Арнольд",
            "Ашраф Хакими", "Тибо Куртуа", "Алиссон Бекер", "Эдерсон", "Майк Меньян", "Вирджил ван Дейк",
            "Маркиньос", "Вильям Салиба", "Габриэл Магальяэс", "Рональд Араухо", "Йошко Гвардиол",
            "Альфонсо Дэвис", "Тео Эрнандес", "Деклан Райс", "Эдуардо Камавинга", "Орельен Чуамени",
            "Илкай Гюндоган", "Николо Барелла", "Мартин Эдегор", "Хын Мин Сон", "Усман Дембеле",
            "Рафаэл Леау", "Хвича Кварацхелия", "Луис Диас", "Габриэл Жезус", "Дарвин Нуньес",
            "Хулиан Альварес", "Маркус Рашфорд", "Родриго", "Нико Уильямс", "Кобби Майну", "Эндрик",
            "Арда Гюлер", "Жоау Невеш", "Варрен Заир-Эмери", "Расмус Хейлунн", "Виктор Дьёкереш",
            "Савиньо", "Фермин Лопес", "Серхио Рамос", "Лука Модрич", "Тони Кроос", "Каземиро",
            "Томас Мюллер", "Мануэль Нойер", "Анхель Ди Мария", "Карим Бензема", "Н'Голо Канте",
            "Серхио Бускетс", "Дани Карвахаль", "Жоау Канселу", "Жоау Феликс", "Хакан Чалханоглу",
            "Алессандро Бастони", "Федерико Кьеза", "Гонсалу Рамуш", "Серу Гирасси", "Александр Исак",
            "Олли Уоткинс", "Энцо Фернандес", "Алексис Мак Аллистер", "Кристиан Пулишич", "Джонатан Дэвид",
            "Виктор Бонифасе", "Юлиан Брандт", "Грегор Кобель", "Джанлуиджи Доннарумма"
        ]
    },
    "club": {
        "name": "🏟 Футбольные клубы (Топ-5 Лиг)",
        "words": [
            # АПЛ (Англия)
            "Манчестер Сити", "Арсенал", "Ливерпуль", "Астон Вилла", "Тоттенхэм", "Ньюкасл", "Манчестер Юнайтед",
            "Челси", "Вест Хэм", "Брайтон", "Борнмут", "Кристал Пэлас", "Вулверхэмптон", "Фулхэм", "Эвертон",
            "Брентфорд", "Ноттингем Форест", "Лестер Сити", "Ипсвич Таун", "Саутгемптон",
            # Ла Лига (Испания)
            "Реал Мадрид", "Барселона", "Атлетико Мадрид", "Жирона", "Атлетик Бильбао", "Реал Сосьедад",
            "Бетис", "Вильярреал", "Валенсия", "Севилья", "Осасуна", "Хетафе", "Сельта", "Райо Вальекано",
            "Лас-Пальмас", "Мальорка", "Алавес", "Леганес", "Вальядолид", "Эспаньол",
            # Серия А (Италия)
            "Интер", "Милан", "Ювентус", "Аталанта", "Рома", "Лацио", "Наполи", "Фиорентина", "Торино",
            "Болонья", "Монца", "Дженоа", "Лечче", "Эмполи", "Кальяри", "Верона", "Парма", "Комо", "Венеция", "Удинезе",
            # Бундеслига (Германия)
            "Бавария", "Байер Леверкузен", "РБ Лейпциг", "Боруссия Дортмунд", "Айнтрахт Франкфурт", "Штутгарт",
            "Вольфсбург", "Боруссия Мёнхенгладбах", "Хоффенхайм", "Вердер", "Фрайбург", "Аугсбург", "Майнц",
            "Унион Берлин", "Бохум", "Санкт-Паули", "Хольштайн Киль", "Хайденхайм",
            # Лига 1 (Франция)
            "ПСЖ", "Монако", "Брест", "Лилль", "Ницца", "Лион", "Ланс", "Марсель", "Ренн", "Тулуза",
            "Реймс", "Монпелье", "Страсбур", "Нант", "Гавр", "Осер", "Анже", "Сент-Этьен"
        ]
    }
}

# gid группы → {'pack', 'initiator', 'players': {uid: ник}, 'msg_ids': {uid: id сообщения-лобби}, 'created'}
active_lobbies = {}


# ───────────────────────── Тексты ─────────────────────────
def spy_rules_text() -> str:
    return (
        "🕵️‍♂️ <b>Игра «Шпион»</b>\n\n"
        "<b>Суть.</b> Все игроки получают по слову из одного пака. У всех слово одно и то же — "
        "и только у одного игрока (шпиона) оно <b>другое</b>. Никто не знает, кто он: "
        "шпион тоже уверен, что у него такое же слово, как у остальных.\n\n"
        "<b>Как играть</b>\n"
        f"1. Играют от {MIN_PLAYERS} до {MAX_PLAYERS} человек из вашей активной группы. "
        "Организатор выбирает пак слов, игроки заходят в лобби кнопкой «➕ Присоединиться», "
        "организатор нажимает «▶️ Начать игру».\n"
        "2. Каждый получает своё слово личным сообщением от бота.\n"
        "3. По очереди описывайте своё слово в чате — <b>не называя его прямо</b> и не давая слишком "
        "очевидных подсказок. Можно задавать друг другу вопросы.\n"
        "4. Слушайте внимательно: чьи описания не сходятся с остальными — у того, скорее всего, другое слово. "
        "Если чужие описания странно не подходят к вашему слову, возможно, шпион — вы.\n"
        "5. Когда наобсуждались, проголосуйте в чате, кого считаете шпионом.\n"
        "6. После голосования все называют свои слова. Нашли шпиона — победили мирные; "
        "шпион остался незамеченным — победил он.\n\n"
        "<b>Советы</b>\n"
        "• Описывайте слово так, чтобы ваши поняли, что у вас с ними одно слово, но не выдавали его целиком.\n"
        "• Не подстраивайтесь слишком явно под остальных — это тоже выглядит подозрительно.\n\n"
        "📖 Нажмите на пак, чтобы увидеть все слова в нём, — потом можно запускать лобби.\n\n"
        "Выберите набор слов (пак):"
    )


def pack_words_text(p_id: str) -> str:
    """Экран пака: название, сколько слов и полный список (по алфавиту). Если не влезает в лимит Telegram — обрезаем."""
    p = SPY_PACKS[p_id]
    words = sorted(dict.fromkeys(p["words"]), key=str.lower)
    head = (f"📖 <b>{esc(p['name'])}</b>\n"
            f"Слов в паке: <b>{len(words)}</b>\n\n"
            "В игре все игроки получат слова из этого списка: у мирных — одно и то же, у шпиона — другое.\n\n")
    shown, size = [], len(head) + 60
    for w in words:
        piece = esc(w) + ", "
        if size + len(piece) > TG_TEXT_LIMIT:
            break
        shown.append(esc(w))
        size += len(piece)
    body = ", ".join(shown)
    if len(shown) < len(words):
        body += f"… и ещё {len(words) - len(shown)}"
    return head + body


async def _safe_edit(c: CallbackQuery, text: str, kb: InlineKeyboardMarkup = None):
    """Правит сообщение с кнопками; если нельзя (не изменилось / устарело) — молча пропускает."""
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except (TelegramAPIError, AttributeError):
        pass


# ───────────────────────── Лобби ─────────────────────────
def get_lobby_text(gid: int, title: str) -> str:
    """title — «сырое» название группы: экранируется здесь."""
    lobby = active_lobbies[gid]
    pack_name = esc(SPY_PACKS[lobby['pack']]['name'])
    players_list = "\n".join([f"• <b>{esc(nick)}</b>" for nick in lobby['players'].values()])
    return (f"🎮 <b>Лобби: Шпион</b>\n"
            f"Пак: <b>{pack_name}</b>\n"
            f"Группа: <b>{esc(title)}</b>\n\n"
            f"👥 Участники ({len(lobby['players'])}/{MAX_PLAYERS}):\n{players_list}")


def get_lobby_kb(gid: int, is_initiator: bool) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text="➕ Присоединиться", callback_data=f"game:spy:join:{gid}")]]
    if is_initiator:
        kb.append([InlineKeyboardButton(text="▶️ Начать игру", callback_data=f"game:spy:run:{gid}")])
        kb.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"game:spy:cancel:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def _group_title(gid: int) -> str:
    g = main.one("SELECT title FROM groups WHERE id=?", (gid,))
    return g["title"] if g else "Группа"


async def update_lobby_messages(gid: int, bot: Bot):
    lobby = active_lobbies.get(gid)
    if not lobby:
        return
    title = _group_title(gid)

    for rid, msg_id in list(lobby['msg_ids'].items()):
        if active_lobbies.get(gid) is not lobby:      # лобби закрыли/запустили, пока мы обновляли
            return
        try:
            await bot.edit_message_text(
                text=main.tag(rid, title) + get_lobby_text(gid, title),
                chat_id=rid,
                message_id=msg_id,
                reply_markup=get_lobby_kb(gid, rid == lobby['initiator'])
            )
        except TelegramAPIError:
            pass
        await asyncio.sleep(SEND_DELAY)


async def _edit_all(gid: int, lobby: dict, bot: Bot, text: str):
    """Заменяет сообщения лобби у всех получателей на text (кнопки убираются)."""
    title = _group_title(gid)
    for rid, msg_id in list(lobby['msg_ids'].items()):
        try:
            await bot.edit_message_text(
                text=main.tag(rid, title) + text,
                chat_id=rid,
                message_id=msg_id
            )
        except TelegramAPIError:
            pass
        await asyncio.sleep(SEND_DELAY)


async def _expire_if_needed(gid: int, bot: Bot):
    """Закрывает лобби, которое висит дольше LOBBY_TTL, — иначе оно блокировало бы группу навсегда."""
    lobby = active_lobbies.get(gid)
    if lobby and time.time() - lobby['created'] > LOBBY_TTL:
        active_lobbies.pop(gid, None)
        await _edit_all(gid, lobby, bot, "⌛ <b>Лобби закрыто:</b> время ожидания истекло.")


def _parse_gid(data: str):
    try:
        return int(data.split(":")[3])
    except (IndexError, ValueError):
        return None


# ───────────────────────── Меню игр ─────────────────────────
def _games_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕵️‍♂️ Шпион", callback_data="game:spy:packs")]
    ])


@games_router.message(Command("games"))
@games_router.message(F.text == GAMES_BTN)
async def cmd_games(m: Message):
    u = await main.reg(m)                 # спросит ник у незарегистрированных, как и остальные команды
    if not u:
        return
    if main.is_banned(u):                 # забаненным бот, как и в main.py, молчит
        return
    # как в main.py (DropState): команда/кнопка меню отменяет ожидание ввода (ника, названия и т.д.)
    main.run("UPDATE users SET state='' WHERE user_id=? AND state!=''", (u["user_id"],))

    mem = main.get_member(m.from_user.id)
    if not mem:
        await m.answer("🗂 Сначала выберите или создайте группу для игры!")
        return

    await m.answer("🎮 <b>Игры в группе</b>\nВыберите игру для запуска в вашей текущей активной группе:",
                   reply_markup=_games_kb())


@games_router.callback_query(F.data == "game:spy:packs")
async def spy_packs(c: CallbackQuery):
    """Нажатие на «Шпион»: правила игры и выбор пака."""
    kb = []
    for p_id, p_data in SPY_PACKS.items():
        kb.append([InlineKeyboardButton(text=p_data["name"], callback_data=f"game:spy:pack:{p_id}")])
    kb.append([InlineKeyboardButton(text="↩️ Назад", callback_data="game:main")])
    await _safe_edit(c, spy_rules_text(), InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()


@games_router.callback_query(F.data.startswith("game:spy:pack:"))
async def spy_pack_view(c: CallbackQuery):
    """Экран выбранного пака: все слова, которые в нём есть, и кнопка запуска лобби."""
    pack_id = c.data.split(":")[3]
    if pack_id not in SPY_PACKS:
        await c.answer("Этот пак больше недоступен", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Создать лобби с этим паком", callback_data=f"game:spy:start:{pack_id}")],
        [InlineKeyboardButton(text="↩️ К пакам", callback_data="game:spy:packs")],
    ])
    await _safe_edit(c, pack_words_text(pack_id), kb)
    await c.answer()


@games_router.callback_query(F.data == "game:main")
async def game_main(c: CallbackQuery):
    await _safe_edit(c, "🎮 <b>Игры в группе</b>\nВыберите игру:", _games_kb())
    await c.answer()


# ───────────────────────── Игра «Шпион» ─────────────────────────
@games_router.callback_query(F.data.startswith("game:spy:start:"))
async def spy_start_lobby(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    pack_id = c.data.split(":")[3]
    if pack_id not in SPY_PACKS:
        await c.answer("Этот пак больше недоступен", show_alert=True)
        return

    u = main.ensure_user(uid)
    if main.is_banned(u):
        await c.answer()
        return
    if not u["nick"]:
        await c.answer("Сначала придумайте ник — напишите боту /start", show_alert=True)
        return

    mem = main.get_member(uid)
    if not mem:
        await c.answer("У вас нет активной группы!", show_alert=True)
        return
    if not main.RELAY_ON and not main.is_admin(uid):
        await c.answer("⏸ Сообщения в боте временно приостановлены администрацией.", show_alert=True)
        return
    if mem["muted_until"] > main.now():
        await c.answer("🔇 Вы в муте в этой группе — запустить игру нельзя.", show_alert=True)
        return

    gid = mem["group_id"]
    await _expire_if_needed(gid, bot)
    if gid in active_lobbies:
        await c.answer("В этой группе уже собирается лобби!", show_alert=True)
        return

    lobby = {
        'pack': pack_id,
        'initiator': uid,
        'players': {uid: u["nick"] or "Игрок"},
        'msg_ids': {},
        'created': time.time(),
    }
    active_lobbies[gid] = lobby
    await c.answer()                      # отвечаем сразу: рассылка ниже может занять несколько секунд

    title = mem['title']
    recipients = sorted(main.active_recipients(gid), key=lambda r: r != uid)   # организатор — первым

    for rid in recipients:
        if active_lobbies.get(gid) is not lobby:      # организатор уже отменил лобби
            break
        u_rid = main.ensure_user(rid)
        if not u_rid or not u_rid["nick"]:
            continue

        try:
            msg = await bot.send_message(rid, main.tag(rid, title) + get_lobby_text(gid, title),
                                         reply_markup=get_lobby_kb(gid, rid == uid))
            lobby['msg_ids'][rid] = msg.message_id
        except TelegramAPIError:
            pass
        await asyncio.sleep(SEND_DELAY)


@games_router.callback_query(F.data.startswith("game:spy:join:"))
async def spy_join(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = _parse_gid(c.data)
    if gid is None:
        await c.answer("Кнопка устарела", show_alert=True)
        return

    await _expire_if_needed(gid, bot)
    lobby = active_lobbies.get(gid)
    if not lobby:
        await c.answer("Лобби уже закрыто или игра началась.", show_alert=True)
        return

    u = main.ensure_user(uid)
    if main.is_banned(u):
        await c.answer()
        return
    if not u["nick"] or not main.get_member(uid, gid):
        await c.answer("Вы не состоите в этой группе.", show_alert=True)
        return

    if uid in lobby['players']:
        await c.answer("Вы уже в лобби!", show_alert=True)
        return

    if len(lobby['players']) >= MAX_PLAYERS:
        await c.answer(f"Лобби заполнено (максимум {MAX_PLAYERS} игроков)!", show_alert=True)
        return

    lobby['players'][uid] = u["nick"]
    await c.answer("Вы присоединились!")

    await update_lobby_messages(gid, bot)


@games_router.callback_query(F.data.startswith("game:spy:run:"))
async def spy_run(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = _parse_gid(c.data)
    if gid is None:
        await c.answer("Кнопка устарела", show_alert=True)
        return

    await _expire_if_needed(gid, bot)
    lobby = active_lobbies.get(gid)
    if not lobby:
        await c.answer("Лобби уже закрыто или игра началась.", show_alert=True)
        return
    if lobby['initiator'] != uid:
        await c.answer("У вас нет прав для старта этой игры.", show_alert=True)
        return

    # играют только те, кто всё ещё состоит в группе (кто-то мог выйти или быть исключён, пока шёл сбор)
    players = [pid for pid in lobby['players'] if main.get_member(pid, gid)]

    if len(players) < MIN_PLAYERS:
        await c.answer(f"Для игры в Шпиона нужно минимум {MIN_PLAYERS} участника!", show_alert=True)
        return

    active_lobbies.pop(gid, None)         # сразу убираем лобби: повторное нажатие «Начать» не запустит вторую игру
    await c.answer("Игра началась!")

    pack_id = lobby['pack']
    pack_name = esc(SPY_PACKS[pack_id]['name'])
    pool = list(dict.fromkeys(SPY_PACKS[pack_id]['words']))       # уникальные слова пака
    common_word, spy_word = random.sample(pool, 2)                # два РАЗНЫХ слова из одного пака
    spy_id = random.choice(players)

    # Все получают одинаково оформленное сообщение и не знают своей роли: шпион просто
    # получает другое слово, чем у остальных, и сам считает, что оно у всех такое же.
    for pid in players:
        word = esc(spy_word if pid == spy_id else common_word)
        text = (f"🎮 <b>Игра «Шпион»</b> · {pack_name}\n\n"
                f"Ваше слово: <b>{word}</b>\n\n"
                "Опишите его в чате, не называя прямо, и слушайте остальных. "
                "У одного из игроков слово другое — найдите, у кого!")
        try:
            await bot.send_message(pid, text)
        except TelegramAPIError:
            pass
        await asyncio.sleep(SEND_DELAY)

    await _edit_all(gid, lobby, bot,
                    "🎮 <b>Игра «Шпион» началась!</b>\nКаждый получил своё слово отдельным сообщением. "
                    "Общайтесь прямо здесь, в группе, и ищите того, у кого слово другое.")


@games_router.callback_query(F.data.startswith("game:spy:cancel:"))
async def spy_cancel(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = _parse_gid(c.data)
    if gid is None:
        await c.answer("Кнопка устарела", show_alert=True)
        return

    lobby = active_lobbies.get(gid)
    if not lobby:
        await c.answer("Лобби уже закрыто или игра началась.", show_alert=True)
        return
    if lobby['initiator'] != uid:
        await c.answer("У вас нет прав.", show_alert=True)
        return

    active_lobbies.pop(gid, None)
    await c.answer("Отменено.")
    await _edit_all(gid, lobby, bot, "🛑 <b>Сбор лобби отменен организатором.</b>")
