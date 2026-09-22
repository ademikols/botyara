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
    await m.answer("🎮 Mini App с играми по коду:", reply_markup=kb)


rooms = {}


def new_ttt():
    return {"board": [""] * 9, "turn": "X", "winner": None}


def check_win(b, s):
    lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
    return any(b[a] == b[c] == b[d] == s for a, c, d in lines)


def gen_code():
    while True:
        c = str(random.randint(100000, 999999))
        if c not in rooms:
            return c


async def broadcast(r, data):
    for c in list(r["clients"]):
        if c.closed:
            continue
        try:
            await c.send_json(data)
        except Exception:
            r["clients"].discard(c)


async def handle_index(request):
    if os.path.exists("app.html"):
        return web.FileResponse("app.html")
    return web.Response(text="app.html not found", status=404)


async def handle_health(request):
    return web.json_response({"ok": True})


async def api_create(request):
    d = await request.json()
    code = gen_code()
    rooms[code] = {
        "host_id": d.get("user_id"),
        "host_name": d.get("username") or "Игрок 1",
        "guest_id": None,
        "guest_name": None,
        "state": new_ttt(),
        "clients": set(),
    }
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
    return web.json_response({"ok": True, "code": code, "host_name": r["host_name"]})


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
                await broadcast(r, {"type": "state", **st})
            elif data.get("type") == "reset":
                r["state"] = new_ttt()
                await broadcast(r, {"type": "state", **r["state"], "reset": True})
    finally:
        r["clients"].discard(ws)
    return ws


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/app.html", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/room/create", api_create)
    app.router.add_post("/api/room/join", api_join)
    app.router.add_get("/ws/game/{code}", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🔧 Веб-сервер на 0.0.0.0:{PORT}", flush=True)
