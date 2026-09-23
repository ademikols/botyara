import os
import json
import random
import time as _time
import asyncio
from aiohttp import web
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

try:
    from games import SPY_PACKS
except Exception:
    SPY_PACKS = {"clash": {"name": "Clash Royale", "words": ["Хог", "Мушкетер", "Ведьма", "Гигант", "Скелеты", "Принц"]}}

PORT = int(os.getenv("PORT", "3000"))
WEB_APP_URL = os.getenv("WEB_APP_URL", "").rstrip("/")

TTT_TURN_LIMIT = 15
TURN_LIMIT = 30

webapp_router = Router()
webapp_router.message.filter(lambda m: m.chat.type == "private")


@webapp_router.message(Command("play"))
async def cmd_play(m: Message):
    if not WEB_APP_URL:
        await m.answer("Ne zadana peremennaya WEB_APP_URL v Bothost.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Otkryt igry", web_app=WebAppInfo(url=f"{WEB_APP_URL}/app.html"))
    ]])
    await m.answer("Mini App: Krestiki, Durak, Shpion", reply_markup=kb)


rooms = {}
durak_rooms = {}
spy_rooms = {}


def gen_code():
    while True:
        c = str(random.randint(100000, 999999))
        if c not in rooms and c not in durak_rooms and c not in spy_rooms:
            return c


def clean_lobby_name(raw, default="Lobby"):
    name = " ".join((raw or "").split())[:40]
    return name or default


async def handle_index(request):
    if os.path.exists("app.html"):
        return web.FileResponse("app.html")
    return web.Response(text="app.html not found", status=404)


async def handle_health(request):
    return web.json_response({"ok": True})


# ================= Krestiki-noliki =================
def new_ttt():
    return {"board": [""] * 9, "turn": "X", "winner": None}


def check_win(b, s):
    lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
    return any(b[a] == b[c] == b[d] == s for a, c, d in lines)


def ttt_remaining(r):
    if r["state"]["winner"] or not r["guest_id"]:
        return 0
    started = r.get("turn_started_at") or int(_time.time())
    return max(0, TTT_TURN_LIMIT - (int(_time.time()) - started))


def ttt_reset_timer(r):
    r["turn_started_at"] = int(_time.time())


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
    rooms[code] = {
        "host_id": d.get("user_id"), "host_name": d.get("username") or "Igrok 1",
        "guest_id": None, "guest_name": None,
        "state": new_ttt(), "clients": set(),
        "turn_started_at": int(_time.time()),
        "name": clean_lobby_name(d.get("name"), "Krestiki"),
        "public": bool(d.get("public", False)),
    }
    return web.json_response({"ok": True, "code": code})


async def api_join(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    r = rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["guest_id"] is not None:
        return web.json_response({"ok": False, "error": "Komnata uje zanyata"}, status=400)
    r["guest_id"] = d.get("user_id")
    r["guest_name"] = d.get("username") or "Igrok 2"
    ttt_reset_timer(r)
    return web.json_response({"ok": True, "code": code})


async def api_list_ttt(request):
    items = []
    for code, r in rooms.items():
        if r["guest_id"] is not None:
            continue
        if not r.get("public"):
            continue
        items.append({
            "code": code, "name": r["name"],
            "host": r["host_name"], "players": 1, "max": 2,
        })
    return web.json_response({"ok": True, "items": items[:50]})


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
        await ws.send_json({"type": "error", "error": "Komnata ne naidena"})
        await ws.close()
        return ws
    if uid == r["host_id"]:
        symbol = "X"
    elif uid == r["guest_id"]:
        symbol = "O"
    else:
        await ws.send_json({"type": "error", "error": "Vy ne v etoi komnate"})
        await ws.close()
        return ws
    r["clients"].add(ws)
    await ws.send_json({"type": "init", "symbol": symbol, "state": r["state"],
                        "turn_remaining": ttt_remaining(r), "turn_limit": TTT_TURN_LIMIT})
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            t = data.get("type")
            st = r["state"]
            if t == "move":
                idx = data.get("index")
                if st["winner"] or not isinstance(idx, int) or not (0 <= idx < 9):
                    continue
                if st["board"][idx] != "" or st["turn"] != symbol:
                    continue
                if r["guest_id"] and ttt_remaining(r) <= 0:
                    opponent = "O" if symbol == "X" else "X"
                    st["winner"] = opponent
                    await broadcast_ttt(r, {"type": "state", **st,
                                            "turn_remaining": 0, "turn_limit": TTT_TURN_LIMIT,
                                            "timeout_by": symbol})
                    continue
                st["board"][idx] = symbol
                if check_win(st["board"], symbol):
                    st["winner"] = symbol
                elif all(st["board"]):
                    st["winner"] = "draw"
                else:
                    st["turn"] = "O" if symbol == "X" else "X"
                    ttt_reset_timer(r)
                await broadcast_ttt(r, {"type": "state", **st,
                                        "turn_remaining": ttt_remaining(r), "turn_limit": TTT_TURN_LIMIT})
            elif t == "reset":
                r["state"] = new_ttt()
                ttt_reset_timer(r)
                await broadcast_ttt(r, {"type": "state", **r["state"], "reset": True,
                                        "turn_remaining": ttt_remaining(r), "turn_limit": TTT_TURN_LIMIT})
            elif t == "surrender":
                if st["winner"]:
                    continue
                opponent = "O" if symbol == "X" else "X"
                st["winner"] = opponent
                await broadcast_ttt(r, {"type": "state", **st,
                                        "turn_remaining": 0, "turn_limit": TTT_TURN_LIMIT,
                                        "surrendered_by": symbol})
            elif t == "timeout":
                if st["winner"]:
                    continue
                if not r["guest_id"]:
                    continue
                if st["turn"] != symbol:
                    continue
                if ttt_remaining(r) > 0:
                    continue
                opponent = "O" if symbol == "X" else "X"
                st["winner"] = opponent
                await broadcast_ttt(r, {"type": "state", **st,
                                        "turn_remaining": 0, "turn_limit": TTT_TURN_LIMIT,
                                        "timeout_by": symbol})
    finally:
        r["clients"].discard(ws)
    return ws


# ================= Durak =================
RANK_NAMES = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "V", 12: "D", 13: "K", 14: "T"}
SUIT_NAMES = {"h": "H", "d": "D", "c": "C", "s": "S"}


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
        "name": opts.get("name", "Durak"),
        "public": opts.get("public", False),
        "players": [{"user_id": host_id, "name": host_name, "hand": hands[0], "left": False, "leave_reason": None}],
        "pending_hands": hands[1:], "deck": deck, "trump": trump, "trump_card": trump_card,
        "table": [], "attacker_idx": 0, "defender_idx": 1,
        "phase": "waiting", "durak_id": None, "is_draw": False,
        "clients": {}, "log": [], "chat": [], "turn_started_at": 0,
        "turn_limit": opts.get("turn_limit", 30),
    }


def find_player(r, uid):
    for i, p in enumerate(r["players"]):
        if p["user_id"] == uid:
            return i
    return -1


def next_active(r, from_idx, skip=0):
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
            p["leave_reason"] = "out_of_cards"
            r["log"].append(f"{p['name']} vyshel")


def check_end(r):
    check_left(r)
    if len(r["deck"]) > 0:
        return
    active = [p for p in r["players"] if not p.get("left")]
    if len(active) <= 1:
        r["phase"] = "over"
        if not active:
            r["is_draw"] = True
            r["log"].append("Nichya - vse vyshli")
        else:
            r["durak_id"] = active[0]["user_id"]
            r["log"].append(f"{active[0]['name']} - durak!")


def advance_roles(r):
    old_def = r["defender_idx"]
    active = [i for i, p in enumerate(r["players"]) if not p.get("left")]
    if len(active) < 2:
        check_end(r)
        return
    new_att = next_active(r, old_def)
    new_def = next_active(r, new_att)
    r["attacker_idx"] = new_att
    r["defender_idx"] = new_def


def reset_turn_timer(r):
    r["turn_started_at"] = int(_time.time())


def turn_remaining(r):
    if r["phase"] in ("waiting", "over"):
        return 0
    limit = r.get("turn_limit", TURN_LIMIT)
    started = r.get("turn_started_at") or int(_time.time())
    return max(0, limit - (int(_time.time()) - started))


def public_state_durak(r, for_uid):
    me_idx = find_player(r, for_uid)
    players = []
    for i, p in enumerate(r["players"]):
        players.append({
            "seat": i, "name": p["name"], "user_id": p["user_id"],
            "hand_count": len(p["hand"]) if not p.get("left") else 0,
            "left": p.get("left", False),
            "leave_reason": p.get("leave_reason"),
            "is_attacker": i == r["attacker_idx"] and r["phase"] not in ("waiting", "over"),
            "is_defender": i == r["defender_idx"] and r["phase"] == "defend",
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
        "your_leave_reason": me.get("leave_reason") if me else None,
        "players": players, "max_players": r["max_players"],
        "log": r["log"][-6:], "opts": r["opts"],
        "chat": r["chat"][-50:],
        "turn_remaining": turn_remaining(r), "turn_limit": r.get("turn_limit", TURN_LIMIT),
    }


async def durak_broadcast(r):
    for uid, ws in list(r["clients"].items()):
        if ws.closed:
            r["clients"].pop(uid, None)
            continue
        try:
            await ws.send_json({"type": "state", "state": public_state_durak(r, uid)})
        except Exception:
            r["clients"].pop(uid, None)


async def durak_create(request):
    d = await request.json()
    size = d.get("deck_size", 36)
    if size not in (36, 52):
        size = 36
    mp = int(d.get("max_players", 2))
    if mp not in (2, 3, 4, 5, 6):
        mp = 2
    tl = int(d.get("turn_limit", 30))
    if tl not in (15, 30, 45, 60, 90):
        tl = 30
    opts = {"deck_size": size, "translate": bool(d.get("translate", False)),
            "throw_limit": 6, "max_players": mp, "turn_limit": tl,
            "name": clean_lobby_name(d.get("name"), "Durak"),
            "public": bool(d.get("public", False))}
    code = gen_code()
    durak_rooms[code] = new_durak(code, d.get("user_id"), d.get("username") or "Hozain", opts)
    return web.json_response({"ok": True, "code": code, "max_players": mp})


async def durak_join(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    uid = d.get("user_id")
    if find_player(r, uid) >= 0:
        return web.json_response({"ok": True, "code": code})
    if r["phase"] != "waiting":
        return web.json_response({"ok": False, "error": "Igra uje nachalas"}, status=400)
    if len(r["players"]) >= r["max_players"]:
        return web.json_response({"ok": False, "error": "Komnata zapolnena"}, status=400)
    hand = r["pending_hands"].pop(0)
    name = d.get("username") or f"Igrok {len(r['players']) + 1}"
    r["players"].append({"user_id": uid, "name": name, "hand": hand, "left": False, "leave_reason": None})
    r["log"].append(f"{name} zashel v igru")
    await durak_broadcast(r)
    return web.json_response({"ok": True, "code": code})


async def durak_list(request):
    items = []
    for code, r in durak_rooms.items():
        if r["phase"] != "waiting":
            continue
        if not r.get("public"):
            continue
        items.append({
            "code": code, "name": r["name"],
            "host": r["players"][0]["name"],
            "players": len(r["players"]),
            "max": r["max_players"],
        })
    return web.json_response({"ok": True, "items": items[:50]})


async def durak_start(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["players"][0]["user_id"] != uid:
        return web.json_response({"ok": False, "error": "Tolko hozain"}, status=403)
    if len(r["players"]) < 2:
        return web.json_response({"ok": False, "error": "Nujno minimum 2 igroka"}, status=400)
    r["phase"] = "attack"
    r["attacker_idx"] = 0
    r["defender_idx"] = next_active(r, 0)
    r["log"].append(f"Igra nachalas. Kozyr: {RANK_NAMES[r['trump_card']['r']]}{SUIT_NAMES[r['trump']]}")
    reset_turn_timer(r)
    await durak_broadcast(r)
    return web.json_response({"ok": True})


async def durak_restart(request):
    """Новая партия с тем же составом."""
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["players"][0]["user_id"] != uid:
        return web.json_response({"ok": False, "error": "Tolko hozain"}, status=403)
    # оставляем только тех, кто не вышел из игры (не сдался)
    active_players = [p for p in r["players"] if p.get("leave_reason") in (None, "out_of_cards")]
    active_players = [p for p in r["players"] if not p.get("left") or p.get("leave_reason") == "out_of_cards"]
    # на самом деле реванш — для тех, кто не сдался и не ушёл
    active_players = [p for p in r["players"] if p.get("leave_reason") != "surrender" and p.get("leave_reason") != "timeout"]
    if len(active_players) < 2:
        return web.json_response({"ok": False, "error": "Nujno minimum 2 igroka dlya novoi partii"}, status=400)

    deck = make_deck(r["opts"].get("deck_size", 36))
    random.shuffle(deck)
    trump_card = deck[-1]
    trump = trump_card["s"]
    hands = []
    for _ in active_players:
        hands.append(sorted(deck[:6], key=card_value, reverse=True))
        deck = deck[6:]

    new_players = []
    for i, p in enumerate(active_players):
        new_players.append({
            "user_id": p["user_id"], "name": p["name"],
            "hand": hands[i], "left": False, "leave_reason": None,
        })

    r["players"] = new_players
    r["max_players"] = len(new_players)
    r["pending_hands"] = []
    r["deck"] = deck
    r["trump"] = trump
    r["trump_card"] = trump_card
    r["table"] = []
    r["attacker_idx"] = 0
    r["defender_idx"] = next_active(r, 0)
    r["phase"] = "attack"
    r["durak_id"] = None
    r["is_draw"] = False
    r["log"] = [f"Novaya partiya! Kozyr: {RANK_NAMES[trump_card['r']]}{SUIT_NAMES[trump]}"]
    r["chat"] = []
    reset_turn_timer(r)
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
        return "Ne vremya hodit"
    if r["phase"] == "defend" and r["table"] and not r["table"][-1].get("defend"):
        return "Zashitnik ne otbil"
    idx = find_player(r, uid)
    if idx < 0 or r["players"][idx].get("left"):
        return "Vy ne v igre"
    if idx == r["defender_idx"]:
        return "Zashitnik ne podkidyvaet"
    p = r["players"][idx]
    if r["table"]:
        if card["r"] not in table_ranks(r):
            return "Podkidyvat mojno tolko rangi so stola"
        if len(r["table"]) >= r["opts"]["throw_limit"]:
            return "Stol polon"
    else:
        if idx != r["attacker_idx"]:
            return "Pervym hodit atakuyshiy"
    if not remove_card(p["hand"], card):
        return "Takoi karty net v ruke"
    cs = f"{RANK_NAMES[card['r']]}{SUIT_NAMES[card['s']]}"
    if r["table"]:
        r["log"].append(f"{p['name']}: {cs} (podkinul)")
    else:
        r["log"].append(f"{p['name']}: {cs}")
    r["table"].append({"attack": card, "defend": None})
    r["phase"] = "defend"
    reset_turn_timer(r)
    return None


def handle_defend(r, uid, card):
    if r["phase"] != "defend":
        return "Ne faza zashity"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Vy ne zashitnik"
    if not r["table"] or r["table"][-1].get("defend"):
        return "Net karty dlya otboya"
    attack = r["table"][-1]["attack"]
    if not beats(attack, card, r["trump"]):
        return "Eta karta ne bet"
    p = r["players"][idx]
    if not remove_card(p["hand"], card):
        return "Takoi karty net v ruke"
    cs = f"{RANK_NAMES[card['r']]}{SUIT_NAMES[card['s']]}"
    ac = f"{RANK_NAMES[attack['r']]}{SUIT_NAMES[attack['s']]}"
    r["log"].append(f"{p['name']}: {cs} bet {ac}")
    r["table"][-1]["defend"] = card
    r["phase"] = "attack"
    reset_turn_timer(r)
    return None


def handle_take(r, uid):
    if r["phase"] != "defend":
        return "Ne faza zashity"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Vy ne zashitnik"
    p = r["players"][idx]
    cnt = len(r["table"])
    for pair in r["table"]:
        p["hand"].append(pair["attack"])
        if pair.get("defend"):
            p["hand"].append(pair["defend"])
    p["hand"].sort(key=card_value, reverse=True)
    r["table"] = []
    r["log"].append(f"{p['name']} vzyal {cnt} kart")
    advance_roles(r)
    for i in range(len(r["players"])):
        refill_hand(r, i)
    r["phase"] = "attack" if r["phase"] != "over" else "over"
    reset_turn_timer(r)
    check_end(r)
    return None


def handle_pass(r, uid):
    if r["phase"] != "attack":
        return "Ne faza ataki"
    idx = find_player(r, uid)
    if idx != r["attacker_idx"]:
        return "Zavershit mojet tolko atakuyshiy"
    if not r["table"]:
        return "Nechto zavershat"
    for pair in r["table"]:
        if not pair.get("defend"):
            return "Zashitnik ne otbil vse karty"
    r["table"] = []
    r["log"].append("BITO")
    advance_roles(r)
    for i in range(len(r["players"])):
        refill_hand(r, i)
    r["phase"] = "attack" if r["phase"] != "over" else "over"
    reset_turn_timer(r)
    check_end(r)
    return None


def handle_translate(r, uid, card):
    if not r["opts"]["translate"]:
        return "Perevodnoi otkluchen"
    if r["phase"] != "defend":
        return "Ne faza zashity"
    idx = find_player(r, uid)
    if idx != r["defender_idx"]:
        return "Vy ne zashitnik"
    if len(r["table"]) != 1 or r["table"][0].get("defend"):
        return "Perevod tolko od
