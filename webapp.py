import os
import json
import random
from aiohttp import web
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

PORT = int(os.getenv("PORT", "3000"))
WEB_APP_URL = os.getenv("WEB_APP_URL", "").rstrip("/")

webapp_router = Router()
webapp_router.message.filter(lambda m: m.chat.type == "private")


@webapp_router.message(Command("play"))
async def cmd_play(m: Message):
    if not WEB_APP_URL:
        await m.answer("❌ Не задана переменная WEB_APP_URL в Bothost.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Открыть игры", web_app=WebAppInfo(url=f"{WEB_APP_URL}/app.html"))
    ]])
    await m.answer("🎮 Mini App с играми:", reply_markup=kb)


@webapp_router.message(Command("durak"))
async def cmd_durak(m: Message):
    if not WEB_APP_URL:
        await m.answer("❌ Не задана переменная WEB_APP_URL в Bothost.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🃏 Дурак", web_app=WebAppInfo(url=f"{WEB_APP_URL}/app.html"))
    ]])
    await m.answer("🃏 Дурак подкидной (2–4 игрока):", reply_markup=kb)


# ═══════════════════════ ОБЩЕЕ ═══════════════════════
rooms = {}
durak_rooms = {}


def gen_code():
    while True:
        c = str(random.randint(100000, 999999))
        if c not in rooms and c not in durak_rooms:
            return c


async def handle_index(request):
    if os.path.exists("app.html"):
        return web.FileResponse("app.html")
    return web.Response(text="app.html not found", status=404)


async def handle_health(request):
    return web.json_response({"ok": True})


# ═══════════════════════ КРЕСТИКИ-НОЛИКИ ═══════════════════════
def new_ttt():
    return {"board": [""] * 9, "turn": "X", "winner": None}


def check_win(b, s):
    lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
    return any(b[a] == b[c] == b[d] == s for a, c, d in lines)


async def broadcast_ttt(r, data):
    for c in list(r["clients"]):
        if c.closed:
            continue
        try:
            await c.send_json(data)
        except Exception:
            r["clients"].discard(c)


async def api_create(request):
    d = await request.json()
    code = gen_code()
    rooms[code] = {"host_id": d.get("user_id"), "host_name": d.get("username") or "Игрок 1",
                   "guest_id": None, "guest_name": None, "state": new_ttt(), "clients": set()}
    return web.json_response({"ok": True, "code": code})


async def api_join(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    r = rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Комната не найдена"}, status=404)
    if r["guest_id"] is not None:
        return web.json_response({"ok": False, "error": "Комната уже занята"}, status=400)
    r["guest_id"] = d.get("user_id")
    r["guest_name"] = d.get("username") or "Игрок 2"
    return web.json_response({"ok": True, "code": code})


async def ws_handler(request):
    code = request.match_info.get("code")
    try:
        uid = int(request.query.get("uid", "0"))
    except ValueError:
        uid = 0
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    r = rooms.get(code)
    if not r:
        await ws.send_json({"type": "error", "error": "Комната не найдена"})
        await ws.close()
        return ws
    if uid == r["host_id"]:
        symbol = "X"
    elif uid == r["guest_id"]:
        symbol = "O"
    else:
        await ws.send_json({"type": "error", "error": "Вы не в этой комнате"})
        await ws.close()
        return ws
    r["clients"].add(ws)
    await ws.send_json({"type": "init", "symbol": symbol, "state": r["state"]})
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            if data.get("type") == "move":
                idx = data.get("index")
                st = r["state"]
                if st["winner"] or not isinstance(idx, int) or not (0 <= idx < 9):
                    continue
                if st["board"][idx] != "" or st["turn"] != symbol:
                    continue
                st["board"][idx] = symbol
                if check_win(st["board"], symbol):
                    st["winner"] = symbol
                elif all(st["board"]):
                    st["winner"] = "draw"
                else:
                    st["turn"] = "O" if symbol == "X" else "X"
                await broadcast_ttt(r, {"type": "state", **st})
            elif data.get("type") == "reset":
                r["state"] = new_ttt()
                await broadcast_ttt(r, {"type": "state", **r["state"], "reset": True})
    finally:
        r["clients"].discard(ws)
    return ws


# ═══════════════════════ ДУРАК (2–4 ИГРОКА) ═══════════════════════
RANK_NAMES = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "В", 12: "Д", 13: "К", 14: "Т"}
SUIT_NAMES = {"h": "♥", "d": "♦", "c": "♣", "s": "♠"}


def make_deck(size=36):
    min_rank = 6 if size == 36 else 2
    deck = []
    for rank in range(min_rank, 15):
        for suit in ("h", "d", "c", "s"):
            deck.append({"r": rank, "s": suit})
    return deck


def card_value(c):
    return c["r"]


def beats(attacker, defender, trump):
    if attacker["s"] == defender["s"]:
        return defender["r"] > attacker["r"]
    if defender["s"] == trump and attacker["s"] != trump:
        return True
    return False


def new_durak(code, host_id, host_name, opts):
    deck = make_deck(opts.get("deck_size", 36))
    random.shuffle(deck)
    trump_card = deck[-1]
    trump = trump_card["s"]
    mp = opts.get("max_players", 2)
    hands = []
    for _ in range(mp):
        hands.append(sorted(deck[:6], key=card_value, reverse=True))
        deck = deck[6:]
    return {
        "code": code, "opts": opts, "max_players": mp,
        "players": [{"user_id": host_id, "name": host_name, "hand": hands[0], "left": False}],
        "pending_hands": hands[1:], "deck": deck, "trump": trump, "trump_card": trump_card,
        "table": [], "attacker_idx": 0, "defender_idx": 1,
        "phase": "waiting", "durak_id": None, "is_draw": False,
        "clients": {}, "log": [],
    }


def find_player(r, uid):
    for i, p in enumerate(r["players"]):
        if p["user_id"] == uid:
            return i
    return -1


def active_indices(r):
    return [i for i, p in enumerate(r["players"]) if not p.get("left")]


def next_active(r, from_idx, skip=0):
    """Следующий активный игрок по кругу. skip — сколько активных пропустить."""
    n = len(r["players"])
    passed = 0
    for i in range(1, n * 2 + 1):
        j = (from_idx + i) % n
        if not r["players"][j].get("left"):
            if passed >= skip:
                return j
            passed += 1
    return from_idx


def refill_hand(r, idx):
    p = r["players"][idx]
    if p.get("left"):
        return
    while len(p["hand"]) < 6 and r["deck"]:
        p["hand"].append(r["deck"].pop(0))
    p["hand"].sort(key=card_value, reverse=True)


def check_left(r):
    if len(r["deck"]) > 0:
        return
    for p in r["players"]:
        if not p.get("left") and not p["hand"]:
            p["left"] = True
            r["log"].append(f"✅ {p['name']} вышел")


def check_end(r):
    check_left(r)
    if len(r["deck"]) > 0:
        return
    active = [p for p in r["players"] if not p.get("left")]
    if len(active) <= 1:
        r["phase"] = "over"
        if not active:
            r["is_draw"] = True
            r["log"].append("🤝 Ничья — все вышли одновременно")
        else:
            r["durak_id"] = active[0]["user_id"]
            r["log"].append(f"🏆 {active[0]['name']} — дурак!")


def advance_roles(r):
    """После отбоя/взятия — сдвигаем роли по кругу."""
    old_def = r["defender_idx"]
    active = active_indices(r)
    if len(active) < 2:
        check_end(r)
        return
    # Новый атакующий — следующий активный после старого защитника
    new_att = next_active(r, old_def)
    # Новый защитник — следующий активный после нового атакующего
    new_def = next_active(r, new_att)
    r["attacker_idx"] = new_att
    r["defender_idx"] = new_def


def public_state(r, for_uid):
    me_idx = find_player(r, for_uid)
    players = []
    for i, p in enumerate(r["players"]):
        players.append({
            "seat": i, "name": p["name"], "user_id": p["user_id"],
            "hand_count": len(p["hand"]) if not p.get("left") else 0,
            "left": p.get("left", False),
            "is_attacker": i == r["attacker_idx"],
            "is_defender": i == r["defender_idx"] and r["phase"] in ("attack", "defend"),
        })
    me = r["players"][me_idx] if me_idx >= 0 else None
    return {
        "code": r["code"], "trump": r["trump"], "trump_card": r["trump_card"],
        "deck_count": len(r["deck"]), "table": r["table"],
        "attacker_idx": r["attacker_idx"], "defender_idx": r["defender_idx"],
        "phase": r["phase"], "durak_id": r["durak_id"], "is_draw": r["is_draw"],
        "your_idx": me_idx, "your_hand": me["hand"] if me else [],
        "your_name": me["name"] if me else None,
        "your_left": me.get("left", False) if me else True,
        "players": players, "max_players": r["max_players"],
        "log": r["log"][-6:], "opts": r["opts"],
    }


async def durak_broadcast(r):
    for uid, ws in list(r["clients"].items()):
        if ws.closed:
            r["clients"].pop(uid, None)
            continue
        try:
            await ws.send_json({"type": "state", "state": public_state(r, uid)})
        except Exception:
            r["clients"].pop(uid, None)


async def durak_create(request):
    d = await request.json()
    size = d.get("deck_size", 36)
    if size not in (36, 52):
        size = 36
    mp = int(d.get("max_players", 2))
    if mp not in (2, 3, 4):
        mp = 2
    opts = {"deck_size": size, "translate": bool(d.get("translate", False)),
            "throw_limit": 6, "max_players": mp}
    code = gen_code()
    durak_rooms[code] = new_durak(code, d.get("user_id"), d.get("username") or "Хозяин", opts)
    return web.json_response({"ok": True, "code": code, "max_players": mp})


async def durak_join(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Комната не найдена"}, status=404)
    uid = d.get("user_id")
    if find_player(r, uid) >= 0:
        return web.json_response({"ok": True, "code": code})  # уже внутри — просто пускаем
    if r["phase"] != "waiting":
        return web.json_response({"ok": False, "error": "Игра уже началась"}, status=400)
    if len(r["players"]) >= r["max_players"]:
        return web.json_response({"ok": False, "error": "Комната заполнена"}, status=400)
    hand = r["pending_hands"].pop(0)
    name = d.get("username") or f"Игрок {len(r['players']) + 1}"
    r["players"].append({"user_id": uid, "name": name, "hand": hand, "left": False})
    r["log"].append(f"➕ {name} зашёл в игру")
    await durak_broadcast(r)
    return web.json_response({"ok": True, "code": code})


async def durak_start(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Комната не найдена"}, status=404)
    if r["players"][0]["user_id"] != uid:
        return web.json_response({"ok": False, "error": "Только хозяин может начать"}, status=403)
    if len(r["players"]) < 2:
        return web.json_response({"ok": False, "error": "Нужно минимум 2 игрока"}, status=400)
    r["phase"] = "attack"
    r["attacker_idx"] = 0
    r["defender_idx"] = next_active(r, 0)
    r["log"].append(f"🎴 Игра началась! Козырь: {RANK_NAMES[r['trump_card']['r']]}{SUIT_NAMES[r['trump']]}")
    await durak_broadcast(r)
    return web.json_response({"ok": True})


def table_ranks(r):
    ranks = set()
    for pair in r["table"]:
        ranks.add(pair["attack"]["r"])
        if pair.get("defend"):
            ranks.add(pair["defend"]["r"])
    return ranks


def remove_card(hand, card):
    for i, c in enumerate(hand):
        if c["r"] == card["r"] and c["s"] == card["s"]:
            hand.pop(i)
            return True
    return False


def handle_attack(r, uid, card):
    if r["phase"] not in ("attack", "defend"):
        return "Сейчас не время ходить"
    if r["phase"] == "defend" and r["table"] and not r["table"][-1].get("defend"):
        return "Защитник ещё не отбил предыдущую карту"
    idx = find_player(r, uid)
    if idx < 0 or r["players"][idx].get("left"):
        return "Вы не в игре"
    if idx == r["defender_idx"]:
        return "Защитник не подкидывает"
    p = r["players"][idx]
    if r["table"]:
        if card["r"] not in table_ranks(r):
            return "Подкидывать можно только карты рангов со стола"
        if len(r["table"]) >= r["opts"]["throw_limit"]:
            return "Стол полон"
    else:
        # Первый ход в раунде — только атакующий
        if idx != r["attacker_idx"]:
            return "Первым ходит атакующий"
    if not remove_card(p["hand"], card):
        return "Такой карты нет в руке"
    r["table"].append({"attack": card, "defend": None})
    r["phase"] = "defend"
    return None


def handle_defend(r, uid, card):
    if r["phase"] != "defend":
        return "Сейчас не фаза защиты"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Вы не защитник"
    if not r["table"] or r["table"][-1].get("defend"):
        return "Нет карты для отбоя"
    attack = r["table"][-1]["attack"]
    if not beats(attack, card, r["trump"]):
        return "Эта карта не бьёт"
    p = r["players"][idx]
    if not remove_card(p["hand"], card):
        return "Такой карты нет в руке"
    r["table"][-1]["defend"] = card
    r["phase"] = "attack"
    return None


def handle_take(r, uid):
    if r["phase"] != "defend":
        return "Сейчас не фаза защиты"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Вы не защитник"
    p = r["players"][idx]
    for pair in r["table"]:
        p["hand"].append(pair["attack"])
        if pair.get("defend"):
            p["hand"].append(pair["defend"])
    p["hand"].sort(key=card_value, reverse=True)
    r["table"] = []
    advance_roles(r)
    for i in range(len(r["players"])):
        refill_hand(r, i)
    r["phase"] = "attack"
    check_end(r)
    return None


def handle_pass(r, uid):
    if r["phase"] != "attack":
        return "Сейчас не фаза атаки"
    idx = find_player(r, uid)
    if idx != r["attacker_idx"]:
        return "Завершить раунд может только атакующий"
    if not r["table"]:
        return "Нечего завершать"
    for pair in r["table"]:
        if not pair.get("defend"):
            return "Защитник ещё не отбил все карты"
    r["table"] = []
    advance_roles(r)
    for i in range(len(r["players"])):
        refill_hand(r, i)
    r["phase"] = "attack"
    check_end(r)
    return None


def handle_translate(r, uid, card):
    if not r["opts"]["translate"]:
        return "Переводной отключён"
    if r["phase"] != "defend":
        return "Не фаза защиты"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Вы не защитник"
    if len(r["table"]) != 1 or r["table"][0].get("defend"):
        return "Перевод — только когда на столе одна неотбитая карта"
    attack = r["table"][0]["attack"]
    if card["r"] != attack["r"]:
        return "Перевод — только картой того же ранга"
    p = r["players"][idx]
    if not remove_card(p["hand"], card):
        return "Такой карты нет в руке"
    r["table"].append({"attack": card, "defend": None})
    # Меняем роли: защитник становится тем, кого защищают (переводит атаку дальше)
    r["attacker_idx"], r["defender_idx"] = r["defender_idx"], r["attacker_idx"]
    return None


async def durak_ws(request):
    code = request.match_info.get("code")
    try:
        uid = int(request.query.get("uid", "0"))
    except ValueError:
        uid = 0
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    r = durak_rooms.get(code)
    if not r:
        await ws.send_json({"type": "error", "error": "Комната не найдена"})
        await ws.close()
        return ws
    if find_player(r, uid) < 0:
        await ws.send_json({"type": "error", "error": "Вы не в этой игре"})
        await ws.close()
        return ws
    r["clients"][uid] = ws
    await ws.send_json({"type": "state", "state": public_state(r, uid)})
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except Exception:
                continue
            act = d.get("action")
            err = None
            if act == "attack":
                err = handle_attack(r, uid, d.get("card"))
            elif act == "defend":
                err = handle_defend(r, uid, d.get("card"))
            elif act == "take":
                err = handle_take(r, uid)
            elif act == "pass":
                err = handle_pass(r, uid)
            elif act == "translate":
                err = handle_translate(r, uid, d.get("card"))
            if err:
                await ws.send_json({"type": "error", "error": err})
            else:
                await durak_broadcast(r)
    finally:
        r["clients"].pop(uid, None)
    return ws


# ═══════════════════════ ЗАПУСК ═══════════════════════
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/app.html", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/room/create", api_create)
    app.router.add_post("/api/room/join", api_join)
    app.router.add_get("/ws/game/{code}", ws_handler)
    app.router.add_post("/api/durak/create", durak_create)
    app.router.add_post("/api/durak/join", durak_join)
    app.router.add_post("/api/durak/start", durak_start)
    app.router.add_get("/ws/durak/{code}", durak_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🔧 Веб-сервер на 0.0.0.0:{PORT}", flush=True)
