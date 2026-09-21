import random
from html import escape as esc

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError

# Импортируем функции из основного файла main.py
from main import ensure_user, get_member, active_recipients, tag, one

games_router = Router()
games_router.message.filter(F.chat.type == "private")

# Расширенные наборы слов для игры "Шпион"
SPY_PACKS = {
    "clash": {
        "name": "Clash Royale (Все карты)",
        "words": [
            "Рыцарь", "Рыцарь на гончей", "Лучезарный дракон", "Лучицы", "Гоблины", "Копейщики", "Гигант",
            "ПЕККА", "Мини ПЕККА", "Всадник на кабане", "Бревно", "Спарки", "Мегарыцарь", "Принцесса",
            "Варвары", "Элитные варвары", "Дракончик", "Ведьма", "Ночная ведьма", "Шар", "Шахтер",
            "Бандитка", "Громовержец", "Пламенный дракон", "Кладбище", "Вышибала", "Палач", "Торнадо",
            "Яд", "Ракета", "Мортира", "Электродракон", "Королевский гигант", "Всадница на баране",
            "Эликсирный голем", "Боевой целитель", "Феникс", "Монах", "Рунный гигант", "Берсерк",
            "Босс Бандитка", "Егерь", "Землетрясение", "Магический лучник", "Королева лучниц",
            "Золотой рыцарь", "Скелет в бочке", "Подрывник", "Адская башня", "Тесла", "Пушка", "Арбалет",
            "Печь", "Хижина гоблинов", "Хижина варваров", "Клон", "Заморозка", "Молния", "Зеркало",
            "Зап", "Снежок", "Стрелы", "Огненный шар", "Скелеты", "Армия скелетов", "Стражи", "Ледяной дух",
            "Огненный дух", "Электрический дух", "Дух исцеления", "Ледяной колдун", "Колдун", "Пламенный колдун",
            "Страж", "Королевские рекруты", "Повозка с пушкой", "Пламенный голем", "Гигантский скелет",
            "Маленький дракон", "Скелетный дракон", "Пламенная башня", "Сборщик эликсира", "Гоблинская бочка",
            "Гоблин-бурильщик", "Стеклорез", "Стенари", "Королевские призраки", "Королевский призрак"
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

active_lobbies = {}

def get_lobby_text(gid: int, title: str) -> str:
    lobby = active_lobbies[gid]
    pack_name = SPY_PACKS[lobby['pack']]['name']
    players_list = "\n".join([f"• <b>{esc(nick)}</b>" for nick in lobby['players'].values()])
    return (f"🎮 <b>Лобби: Шпион</b>\n"
            f"Пак: <b>{pack_name}</b>\n"
            f"Группа: <b>{title}</b>\n\n"
            f"👥 Участники ({len(lobby['players'])}/15):\n{players_list}")

def get_lobby_kb(gid: int, is_initiator: bool) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text="➕ Присоединиться", callback_data=f"game:spy:join:{gid}")]]
    if is_initiator:
        kb.append([InlineKeyboardButton(text="▶️ Начать игру", callback_data=f"game:spy:run:{gid}")])
        kb.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"game:spy:cancel:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def update_lobby_messages(gid: int, bot: Bot):
    if gid not in active_lobbies: return
    lobby = active_lobbies[gid]
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    title = esc(g["title"]) if g else "Группа"
    
    for rid, msg_id in list(lobby['msg_ids'].items()):
        is_init = (rid == lobby['initiator'])
        try:
            await bot.edit_message_text(
                text=tag(rid, title) + get_lobby_text(gid, title),
                chat_id=rid,
                message_id=msg_id,
                reply_markup=get_lobby_kb(gid, is_init)
            )
        except TelegramAPIError:
            pass

@games_router.message(F.text == "🎮 Игры")
async def cmd_games(m: Message):
    mem = get_member(m.from_user.id)
    if not mem:
        await m.answer("🗂 Сначала выберите или создайте группу для игры!")
        return
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕵️‍♂️ Шпион", callback_data="game:spy:packs")]
    ])
    await m.answer("🎮 <b>Игры в группе</b>\nВыберите игру для запуска в вашей текущей активной группе:", reply_markup=kb)

@games_router.callback_query(F.data == "game:spy:packs")
async def spy_packs(c: CallbackQuery):
    kb = []
    for p_id, p_data in SPY_PACKS.items():
        kb.append([InlineKeyboardButton(text=p_data["name"], callback_data=f"game:spy:start:{p_id}")])
    kb.append([InlineKeyboardButton(text="↩️ Назад", callback_data="game:main")])
    await c.message.edit_text("🕵️‍♂️ <b>Игра: Шпион</b>\n\nШпион не знает загаданного слова. Все задают друг другу вопросы, чтобы вычислить шпиона. Шпион должен догадаться, что это за слово.\n\nВыберите набор слов (пак):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()

@games_router.callback_query(F.data == "game:main")
async def game_main(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕵️‍♂️ Шпион", callback_data="game:spy:packs")]
    ])
    await c.message.edit_text("🎮 <b>Игры в группе</b>\nВыберите игру:", reply_markup=kb)
    await c.answer()

@games_router.callback_query(F.data.startswith("game:spy:start:"))
async def spy_start_lobby(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    pack_id = c.data.split(":")[3]
    mem = get_member(uid)
    
    if not mem:
        await c.answer("У вас нет активной группы!", show_alert=True)
        return
        
    gid = mem["group_id"]
    if gid in active_lobbies:
        await c.answer("В этой группе уже собирается лобби!", show_alert=True)
        return

    u = ensure_user(uid)
    active_lobbies[gid] = {
        'pack': pack_id,
        'initiator': uid,
        'players': {uid: u.get("nick", "Игрок")},
        'msg_ids': {}
    }
    
    title = esc(mem['title'])
    recipients = active_recipients(gid)
    
    for rid in recipients:
        u_rid = ensure_user(rid)
        if not u_rid or not u_rid["nick"]: continue
        
        is_init = (rid == uid)
        kb = get_lobby_kb(gid, is_init)
        
        try:
            msg = await bot.send_message(rid, tag(rid, title) + get_lobby_text(gid, title), reply_markup=kb)
            active_lobbies[gid]['msg_ids'][rid] = msg.message_id
        except TelegramAPIError:
            pass
        
    await c.answer()

@games_router.callback_query(F.data.startswith("game:spy:join:"))
async def spy_join(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = int(c.data.split(":")[3])
    
    if gid not in active_lobbies:
        await c.answer("Лобби уже закрыто или игра началась.", show_alert=True)
        return
        
    lobby = active_lobbies[gid]
    if uid in lobby['players']:
        await c.answer("Вы уже в лобби!", show_alert=True)
        return
        
    u = ensure_user(uid)
    lobby['players'][uid] = u["nick"]
    await c.answer("Вы присоединились!")
    
    await update_lobby_messages(gid, bot)

@games_router.callback_query(F.data.startswith("game:spy:run:"))
async def spy_run(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = int(c.data.split(":")[3])
    
    if gid not in active_lobbies or active_lobbies[gid]['initiator'] != uid:
        await c.answer("У вас нет прав для старта этой игры.", show_alert=True)
        return
        
    lobby = active_lobbies[gid]
    players = list(lobby['players'].keys())
    
    if len(players) < 3:
        await c.answer("Для игры в Шпиона нужно минимум 3 участника!", show_alert=True)
        return
        
    pack_id = lobby['pack']
    word = random.choice(SPY_PACKS[pack_id]['words'])
    spy_id = random.choice(players)
    
    for pid in players:
        if pid == spy_id:
            text = "🕵️‍♂️ <b>Вы — Шпион!</b>\nПопытайтесь узнать слово из обсуждения участников группы и не спалиться."
        else:
            text = f"🟢 <b>Вы — мирный!</b>\nЗагаданное слово: <b>{word}</b>\nЗадавайте вопросы участникам группы, чтобы вычислить шпиона!"
        try:
            await bot.send_message(pid, text)
        except TelegramAPIError:
            pass
            
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    title = esc(g["title"]) if g else "Группа"
    
    for rid, msg_id in lobby['msg_ids'].items():
        try:
            await bot.edit_message_text(
                text=tag(rid, title) + "🎮 <b>Игра Шпион началась!</b>\nРоли разосланы, общайтесь прямо здесь, в группе.",
                chat_id=rid,
                message_id=msg_id
            )
        except TelegramAPIError:
            pass
            
    del active_lobbies[gid]
    await c.answer("Игра началась!")

@games_router.callback_query(F.data.startswith("game:spy:cancel:"))
async def spy_cancel(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    gid = int(c.data.split(":")[3])
    
    if gid not in active_lobbies or active_lobbies[gid]['initiator'] != uid:
        await c.answer("У вас нет прав.", show_alert=True)
        return
        
    lobby = active_lobbies[gid]
    g = one("SELECT title FROM groups WHERE id=?", (gid,))
    title = esc(g["title"]) if g else "Группа"
    
    for rid, msg_id in lobby['msg_ids'].items():
        try:
            await bot.edit_message_text(
                text=tag(rid, title) + "🛑 <b>Сбор лобби отменен организатором.</b>",
                chat_id=rid,
                message_id=msg_id
            )
        except TelegramAPIError:
            pass
            
    del active_lobbies[gid]
    await c.answer("Отменено.")
