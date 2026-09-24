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
    await m.answer("Mini App: Krestiki, Durak, Shpion, Haxball", reply_markup=kb)


rooms = {}
durak_rooms = {}
spy_rooms = {}


def gen_code():
    while True:
        c = str(random.randint(100000, 999999))
        if c not in rooms and c not in durak_rooms and c not in spy_rooms:
            return c


def clean_name(raw, default="Lobby"):
    name = " ".join((raw or "").split())[:40]
    return name or default


async def handle_index(request):
    if os.path.exists("app.html"):
        return web.FileResponse("app.html")
    return web.Response(text="app.html not found", status=404)


async def handle_haxball_page(request):
    if os.path.exists("haxball.html"):
        return web.FileResponse("haxball.html")
    return web.Response(text="haxball.html not found", status=404)


async def handle_health(request):
    return web.json_response({"ok": True})


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
        "name": clean_name(d.get("name"), "Krestiki"),
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
        "is_host": (me_idx == 0),
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
    opts = {
        "deck_size": size,
        "translate": bool(d.get("translate", False)),
        "throw_limit": 6,
        "max_players": mp,
        "turn_limit": tl,
        "name": clean_name(d.get("name"), "Durak"),
        "public": bool(d.get("public", False)),
    }
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
            "turn_limit": r.get("turn_limit", 30),
            "deck_size": r["opts"].get("deck_size", 36),
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
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = durak_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["players"][0]["user_id"] != uid:
        return web.json_response({"ok": False, "error": "Tolko hozain"}, status=403)
    active = [p for p in r["players"] if p.get("leave_reason") != "surrender"]
    if len(active) < 2:
        return web.json_response({"ok": False, "error": "Nujno minimum 2 igroka"}, status=400)
    deck = make_deck(r["opts"].get("deck_size", 36))
    random.shuffle(deck)
    trump_card = deck[-1]
    trump = trump_card["s"]
    hands = []
    for _ in active:
        hands.append(sorted(deck[:6], key=card_value, reverse=True))
        deck = deck[6:]
    new_players = []
    for i, p in enumerate(active):
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
        return "Perevod tolko odna neotbitaya karta"
    attack = r["table"][0]["attack"]
    if card["r"] != attack["r"]:
        return "Perevod tolko kartoi togo je ranga"
    p = r["players"][idx]
    if not remove_card(p["hand"], card):
        return "Takoi karty net v ruke"
    r["table"].append({"attack": card, "defend": None})
    r["attacker_idx"], r["defender_idx"] = r["defender_idx"], r["attacker_idx"]
    reset_turn_timer(r)
    return None


def handle_surrender(r, uid, reason="surrender"):
    idx = find_player(r, uid)
    if idx < 0:
        return "Vy ne v igre"
    p = r["players"][idx]
    if p.get("left"):
        return None
    p["left"] = True
    p["leave_reason"] = reason
    p["hand"] = []
    if reason == "timeout":
        r["log"].append(f"{p['name']} ne shodil vovremya - SDALSYA")
    else:
        r["log"].append(f"{p['name']} sdalsya")
    if r["phase"] in ("attack", "defend"):
        active = [i for i, x in enumerate(r["players"]) if not x.get("left")]
        if len(active) < 2:
            r["table"] = []
            check_end(r)
            return None
        r["table"] = []
        for i in range(len(r["players"])):
            refill_hand(r, i)
        advance_roles(r)
        reset_turn_timer(r)
    check_end(r)
    return None


def auto_action(r):
    if r["phase"] == "defend":
        r["log"].append(f"{r['players'][r['defender_idx']]['name']} ne uspel - VZYAL karty")
        handle_take(r, r["players"][r["defender_idx"]]["user_id"])
        return
    if r["phase"] == "attack":
        if r["table"] and all(p.get("defend") for p in r["table"]):
            r["log"].append(f"{r['players'][r['attacker_idx']]['name']} ne uspel - BITO")
            handle_pass(r, r["players"][r["attacker_idx"]]["user_id"])
        elif r["table"]:
            return
        else:
            handle_surrender(r, r["players"][r["attacker_idx"]]["user_id"], reason="timeout")


async def durak_watchdog():
    while True:
        await asyncio.sleep(2)
        try:
            now_ts = int(_time.time())
            for code in list(durak_rooms.keys()):
                r = durak_rooms.get(code)
                if not r:
                    continue
                if r["phase"] not in ("attack", "defend"):
                    continue
                if not r["clients"]:
                    continue
                started = r.get("turn_started_at") or now_ts
                limit = r.get("turn_limit", TURN_LIMIT)
                if now_ts - started >= limit:
                    auto_action(r)
                    await durak_broadcast(r)
        except Exception as e:
            print(f"durak watchdog error: {e}", flush=True)


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
        await ws.send_json({"type": "error", "error": "Komnata ne naidena"})
        await ws.close()
        return ws
    if find_player(r, uid) < 0:
        await ws.send_json({"type": "error", "error": "Vy ne v etoi igre"})
        await ws.close()
        return ws
    r["clients"][uid] = ws
    await ws.send_json({"type": "state", "state": public_state_durak(r, uid)})
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except Exception:
                continue
            act = d.get("action")
            if act == "chat":
                text = (d.get("text") or "").strip()[:300]
                if text:
                    name = next((p["name"] for p in r["players"] if p["user_id"] == uid), "?")
                    r["chat"].append({"user": name, "text": text})
                    if len(r["chat"]) > 200:
                        r["chat"] = r["chat"][-200:]
                    await durak_broadcast(r)
                continue
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
            elif act == "surrender":
                err = handle_surrender(r, uid, reason="surrender")
            if err:
                await ws.send_json({"type": "error", "error": err})
            else:
                await durak_broadcast(r)
    finally:
        r["clients"].pop(uid, None)
    return ws


VOTE_TIME_LIMIT = 90


def new_spy_room(code, host_id, host_name, opts):
    return {
        "code": code, "host_id": host_id, "pack": opts.get("pack", "clash"),
        "name": opts.get("name", "Shpion"),
        "public": opts.get("public", False),
        "players": [{"user_id": host_id, "name": host_name, "vote": None}],
        "phase": "waiting", "common_word": None, "spy_word": None, "spy_id": None,
        "clients": {}, "chat": [], "vote_started_at": 0, "log": [],
    }


def spy_find(r, uid):
    for i, p in enumerate(r["players"]):
        if p["user_id"] == uid:
            return i
    return -1


def spy_vote_remaining(r):
    if r["phase"] != "vote":
        return 0
    return max(0, VOTE_TIME_LIMIT - int(_time.time() - r["vote_started_at"]))


def spy_public(r, for_uid):
    me_idx = spy_find(r, for_uid)
    players = []
    for i, p in enumerate(r["players"]):
        players.append({
            "seat": i, "name": p["name"], "user_id": p["user_id"],
            "is_me": p["user_id"] == for_uid,
            "has_voted": p["vote"] is not None,
        })
    reveal = r["phase"] == "result"
    result = None
    if reveal:
        votes = {}
        for p in r["players"]:
            if p["vote"] is not None:
                votes.setdefault(p["vote"], []).append(p["name"])
        tally = {}
        for p in r["players"]:
            if p["vote"] is not None:
                tally[p["vote"]] = tally.get(p["vote"], 0) + 1
        top_id, top_cnt, tie = None, 0, False
        for tid, cnt in tally.items():
            if cnt > top_cnt:
                top_id, top_cnt, tie = tid, cnt, False
            elif cnt == top_cnt:
                tie = True
        spy_caught = (top_id == r["spy_id"]) and not tie
        result = {
            "spy_id": r["spy_id"],
            "spy_name": next((p["name"] for p in r["players"] if p["user_id"] == r["spy_id"]), "?"),
            "common_word": r["common_word"], "spy_word": r["spy_word"],
            "votes": votes, "voted_id": top_id,
            "voted_name": next((p["name"] for p in r["players"] if p["user_id"] == top_id), "-") if top_id else "-",
            "votes_count": top_cnt, "tie": tie, "spy_caught": spy_caught,
            "winner": "civilians" if spy_caught else "spy",
        }
    return {
        "code": r["code"], "pack": r["pack"],
        "pack_name": SPY_PACKS.get(r["pack"], {}).get("name", r["pack"]),
        "phase": r["phase"], "host_id": r["host_id"],
        "is_host": (me_idx == 0),
        "players": players, "your_idx": me_idx,
        "chat": r["chat"][-50:], "vote_remaining": spy_vote_remaining(r),
        "result": result, "log": r["log"][-6:],
    }


async def spy_broadcast(r):
    for uid, ws in list(r["clients"].items()):
        if ws.closed:
            r["clients"].pop(uid, None)
            continue
        try:
            await ws.send_json({"type": "state", "state": spy_public(r, uid)})
        except Exception:
            r["clients"].pop(uid, None)


async def spy_create(request):
    d = await request.json()
    pack = d.get("pack") or "clash"
    if pack not in SPY_PACKS:
        pack = next(iter(SPY_PACKS.keys()))
    opts = {
        "pack": pack,
        "name": clean_name(d.get("name"), "Shpion"),
        "public": bool(d.get("public", False)),
    }
    code = gen_code()
    spy_rooms[code] = new_spy_room(code, d.get("user_id"), d.get("username") or "Hozain", opts)
    return web.json_response({"ok": True, "code": code})


async def spy_join(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    r = spy_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    uid = d.get("user_id")
    if spy_find(r, uid) >= 0:
        return web.json_response({"ok": True, "code": code})
    if r["phase"] != "waiting":
        return web.json_response({"ok": False, "error": "Igra uje nachalas"}, status=400)
    if len(r["players"]) >= 15:
        return web.json_response({"ok": False, "error": "Komnata zapolnena"}, status=400)
    name = d.get("username") or f"Igrok {len(r['players'])+1}"
    r["players"].append({"user_id": uid, "name": name, "vote": None})
    r["log"].append(f"{name} zashel")
    await spy_broadcast(r)
    return web.json_response({"ok": True, "code": code})


async def spy_list(request):
    items = []
    for code, r in spy_rooms.items():
        if r["phase"] != "waiting":
            continue
        if not r.get("public"):
            continue
        items.append({
            "code": code, "name": r["name"],
            "host": r["players"][0]["name"],
            "players": len(r["players"]),
            "max": 15,
            "pack_name": SPY_PACKS.get(r["pack"], {}).get("name", r["pack"]),
        })
    return web.json_response({"ok": True, "items": items[:50]})


async def spy_start(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = spy_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["host_id"] != uid:
        return web.json_response({"ok": False, "error": "Tolko hozain"}, status=403)
    if len(r["players"]) < 3:
        return web.json_response({"ok": False, "error": "Nujno minimum 3 igroka"}, status=400)
    pool = list(dict.fromkeys(SPY_PACKS[r["pack"]]["words"]))
    if len(pool) < 2:
        return web.json_response({"ok": False, "error": "V pake malo slov"}, status=400)
    common, spy = random.sample(pool, 2)
    r["common_word"] = common
    r["spy_word"] = spy
    spy_player = random.choice(r["players"])
    r["spy_id"] = spy_player["user_id"]
    r["phase"] = "discuss"
    r["log"].append("Igra nachalas.")
    for p in r["players"]:
        ws = r["clients"].get(p["user_id"])
        if ws and not ws.closed:
            word = spy if p["user_id"] == r["spy_id"] else common
            try:
                await ws.send_json({"type": "your_word", "word": word,
                                    "pack_name": SPY_PACKS[r["pack"]]["name"]})
            except Exception:
                pass
    await spy_broadcast(r)
    return web.json_response({"ok": True})


async def spy_to_vote(request):
    d = await request.json()
    code = str(d.get("code", "")).strip()
    uid = d.get("user_id")
    r = spy_rooms.get(code)
    if not r:
        return web.json_response({"ok": False, "error": "Komnata ne naidena"}, status=404)
    if r["host_id"] != uid:
        return web.json_response({"ok": False, "error": "Tolko hozain"}, status=403)
    if r["phase"] != "discuss":
        return web.json_response({"ok": False, "error": "Ne faza obsujdeniya"}, status=400)
    r["phase"] = "vote"
    r["vote_started_at"] = int(_time.time())
    for p in r["players"]:
        p["vote"] = None
    r["log"].append(f"Golosovanie nachalos. {VOTE_TIME_LIMIT} sek.")
    await spy_broadcast(r)
    return web.json_response({"ok": True})


def spy_finish_vote(r):
    r["phase"] = "result"


async def spy_watchdog():
    while True:
        await asyncio.sleep(2)
        try:
            now_ts = int(_time.time())
            for code in list(spy_rooms.keys()):
                r = spy_rooms.get(code)
                if not r:
                    continue
                if r["phase"] != "vote":
                    continue
                if not r["clients"]:
                    continue
                if now_ts - r["vote_started_at"] >= VOTE_TIME_LIMIT:
                    spy_finish_vote(r)
                    r["log"].append("Vremya vyshlo.")
                    await spy_broadcast(r)
        except Exception as e:
            print(f"spy watchdog error: {e}", flush=True)


async def spy_ws(request):
    code = request.match_info.get("code")
    try:
        uid = int(request.query.get("uid", "0"))
    except ValueError:
        uid = 0
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    r = spy_rooms.get(code)
    if not r:
        await ws.send_json({"type": "error", "error": "Komnata ne naidena"})
        await ws.close()
        return ws
    if spy_find(r, uid) < 0:
        await ws.send_json({"type": "error", "error": "Vy ne v etoi igre"})
        await ws.close()
        return ws
    r["clients"][uid] = ws
    await ws.send_json({"type": "state", "state": spy_public(r, uid)})
    if r["phase"] in ("discuss", "vote") and r["spy_id"] is not None:
        word = r["spy_word"] if uid == r["spy_id"] else r["common_word"]
        try:
            await ws.send_json({"type": "your_word", "word": word,
                                "pack_name": SPY_PACKS[r["pack"]]["name"]})
        except Exception:
            pass
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
            except Exception:
                continue
            act = d.get("type")
            if act == "chat":
                text = (d.get("text") or "").strip()[:300]
                if text:
                    name = next((p["name"] for p in r["players"] if p["user_id"] == uid), "?")
                    r["chat"].append({"user": name, "text": text})
                    if len(r["chat"]) > 200:
                        r["chat"] = r["chat"][-200:]
                    await spy_broadcast(r)
            elif act == "vote":
                if r["phase"] != "vote":
                    await ws.send_json({"type": "error", "error": "Ne golosovanie"})
                    continue
                target = d.get("target_id")
                if target == uid:
                    await ws.send_json({"type": "error", "error": "Nelzya za sebya"})
                    continue
                me = next((p for p in r["players"] if p["user_id"] == uid), None)
                if me:
                    me["vote"] = target
                if all(p["vote"] is not None for p in r["players"]):
                    spy_finish_vote(r)
                    r["log"].append("Vse progolosovali.")
                await spy_broadcast(r)
            elif act == "reset":
                if r["host_id"] != uid:
                    continue
                r["phase"] = "waiting"
                r["common_word"] = None
                r["spy_word"] = None
                r["spy_id"] = None
                r["chat"] = []
                for p in r["players"]:
                    p["vote"] = None
                r["log"].append("Novaya igra.")
                await spy_broadcast(r)
            elif act == "finish_vote":
                if r["phase"] != "vote":
                    continue
                if r["host_id"] != uid:
                    continue
                spy_finish_vote(r)
                r["log"].append("Hozain zavershil golosovanie.")
                await spy_broadcast(r)
    finally:
        r["clients"].pop(uid, None)
    return ws


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/app.html", handle_index)
    app.router.add_get("/haxball.html", handle_haxball_page)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/room/create", api_create)
    app.router.add_post("/api/room/join", api_join)
    app.router.add_get("/api/room/list", api_list_ttt)
    app.router.add_get("/ws/game/{code}", ws_handler)
    app.router.add_post("/api/durak/create", durak_create)
    app.router.add_post("/api/durak/join", durak_join)
    app.router.add_get("/api/durak/list", durak_list)
    app.router.add_post("/api/durak/start", durak_start)
    app.router.add_post("/api/durak/restart", durak_restart)
    app.router.add_get("/ws/durak/{code}", durak_ws)
    app.router.add_post("/api/spy/create", spy_create)
    app.router.add_post("/api/spy/join", spy_join)
    app.router.add_get("/api/spy/list", spy_list)
    app.router.add_post("/api/spy/start", spy_start)
    app.router.add_post("/api/spy/to_vote", spy_to_vote)
    app.router.add_get("/ws/spy/{code}", spy_ws)

    from haxball import register_haxball_routes, haxball_watchdog
    register_haxball_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(durak_watchdog())
    asyncio.create_task(spy_watchdog())
    asyncio.create_task(haxball_watchdog())
    print(f"Web server started on 0.0.0.0:{PORT}", flush=True)
