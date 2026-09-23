"""
Haxball-клон для Telegram Mini App.
Экспорт: register_haxball_routes(app), haxball_watchdog()
"""

import asyncio
import json
import math
import random
import string
import time

from aiohttp import web, WSMsgType

FIELD_W = 840
FIELD_H = 400
GOAL_TOP = 150
GOAL_BOTTOM = 250
GOAL_DEPTH = 18

PLAYER_R = 15
BALL_R = 8

KICK_RANGE = PLAYER_R + BALL_R + 4
KICK_POWER = 400.0
KICK_VEL_BONUS = 1.1
KICK_COOLDOWN = 0.28
KICK_GLOW_TIME = 0.15

BALL_FRICTION = 0.994
WALL_BOUNCE = 0.85
PLAYER_BALL_BOUNCE = 0.85
PLAYER_PLAYER_BOUNCE = 0.55

TELEPORT_GUARD = 200.0

TICK_HZ = 60
TICK_DT = 1.0 / TICK_HZ
MAX_SUBSTEP = 0.005
BROADCAST_INTERVAL = 0.033

GOAL_PAUSE_TIME = 2.0
ROOM_TTL_EMPTY = 60.0

ROOMS = {}


def gen_code():
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if code not in ROOMS:
            return code


def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


class Player:
    def __init__(self, user_id, name, team, slot):
        self.user_id = user_id
        self.name = (name or "Игрок")[:16]
        self.team = team
        self.slot = slot
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.kick = False
        self.kick_glow_until = 0.0
        self.kick_cooldown_until = 0.0
        self.online = True
        self.ws = None
        self.spawn()

    def spawn(self):
        cy = FIELD_H / 2.0
        base_x = FIELD_W * 0.25 if self.team == "left" else FIELD_W * 0.75
        offset = (self.slot - 1) * 50 - 25
        self.x = base_x
        self.y = clamp(cy + offset, PLAYER_R, FIELD_H - PLAYER_R)
        self.vx = 0.0
        self.vy = 0.0

    def to_dict(self, now):
        return {
            "user_id": self.user_id,
            "name": self.name,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "vx": round(self.vx, 1),
            "vy": round(self.vy, 1),
            "team": self.team,
            "slot": self.slot,
            "kick_glow": 1 if now < self.kick_glow_until else 0,
            "online": self.online,
        }


class Room:
    def __init__(self, code, name, host_id, max_players, win_score, match_time):
        self.code = code
        self.name = (name or "Игра")[:24]
        self.host_id = host_id
        self.max_players = max_players
        self.win_score = win_score
        self.match_time_total = match_time
        self.timer = float(match_time)
        self.phase = "waiting"
        self.score = {"left": 0, "right": 0}
        self.winner = None
        self.phase_after_pause = "battle"
        self.players = {}
        self.ball = {"x": FIELD_W / 2.0, "y": FIELD_H / 2.0, "vx": 0.0, "vy": 0.0}
        self.goal_pause_until = 0.0
        self.last_tick = time.monotonic()
        self.last_broadcast = 0.0
        self.empty_since = None

    def team_counts(self):
        left = sum(1 for p in self.players.values() if p.team == "left")
        right = sum(1 for p in self.players.values() if p.team == "right")
        return left, right

    def next_team_slot(self):
        left, right = self.team_counts()
        per_team = self.max_players // 2
        if left <= right and left < per_team:
            team = "left"
        elif right < per_team:
            team = "right"
        else:
            return None, None
        used = set(p.slot for p in self.players.values() if p.team == team)
        slot = 1
        while slot in used:
            slot += 1
        return team, slot

    def add_player(self, user_id, name):
        if not user_id:
            return False, "no_user_id"
        if user_id in self.players:
            self.players[user_id].online = True
            return True, None
        if self.phase == "over":
            return False, "game_over"
        if len(self.players) >= self.max_players:
            return False, "room_full"
        team, slot = self.next_team_slot()
        if team is None:
            return False, "room_full"
        p = Player(user_id, name, team, slot)
        self.players[user_id] = p
        self.empty_since = None
        if len(self.players) >= 2 and self.phase == "waiting":
            self.start_match()
        return True, None

    def start_match(self):
        self.phase = "battle"
        self.timer = float(self.match_time_total)
        self.reset_positions()

    def reset_positions(self):
        self.ball = {"x": FIELD_W / 2.0, "y": FIELD_H / 2.0, "vx": 0.0, "vy": 0.0}
        for p in self.players.values():
            p.spawn()

    def public_summary(self):
        host_name = self.players[self.host_id].name if self.host_id in self.players else self.name
        return {
            "code": self.code,
            "name": self.name,
            "host": host_name,
            "players": len(self.players),
            "max": self.max_players,
        }

    def snapshot(self, for_uid):
        now = time.monotonic()
        me = self.players.get(for_uid)
        return {
            "type": "state",
            "state": {
                "phase": self.phase,
                "score": dict(self.score),
                "timer": max(0, round(self.timer)),
                "players": [pl.to_dict(now) for pl in self.players.values()],
                "ball": {
                    "x": round(self.ball["x"], 1),
                    "y": round(self.ball["y"], 1),
                    "vx": round(self.ball["vx"], 1),
                    "vy": round(self.ball["vy"], 1),
                },
                "my_side": me.team if me else None,
                "winner": self.winner,
                "win_score": self.win_score,
                "max_players": self.max_players,
            },
        }

    def clamp_player_pos(self, x, y):
        y = clamp(y, PLAYER_R, FIELD_H - PLAYER_R)
        in_mouth = (GOAL_TOP + 2) <= y <= (GOAL_BOTTOM - 2)
        if in_mouth:
            x = clamp(x, -GOAL_DEPTH + 2, FIELD_W + GOAL_DEPTH - 2)
        else:
            x = clamp(x, PLAYER_R, FIELD_W - PLAYER_R)
        return x, y

    def apply_move(self, user_id, data):
        p = self.players.get(user_id)
        if not p or self.phase != "battle":
            return
        try:
            nx = float(data.get("x", p.x))
            ny = float(data.get("y", p.y))
            nvx = float(data.get("vx", 0.0))
            nvy = float(data.get("vy", 0.0))
            kick = bool(data.get("kick", False))
        except (TypeError, ValueError):
            return

        dist = math.hypot(nx - p.x, ny - p.y)
        if dist <= TELEPORT_GUARD:
            nx, ny = self.clamp_player_pos(nx, ny)
            p.x, p.y = nx, ny
            p.vx, p.vy = nvx, nvy

        p.kick = kick
        now = time.monotonic()
        if kick and now >= p.kick_cooldown_until:
            dx = self.ball["x"] - p.x
            dy = self.ball["y"] - p.y
            d = math.hypot(dx, dy)
            if d <= KICK_RANGE:
                if d < 0.001:
                    ux, uy = 1.0, 0.0
                else:
                    ux, uy = dx / d, dy / d
                self.ball["vx"] = ux * KICK_POWER + p.vx * KICK_VEL_BONUS
                self.ball["vy"] = uy * KICK_POWER + p.vy * KICK_VEL_BONUS
                p.kick_cooldown_until = now + KICK_COOLDOWN
                p.kick_glow_until = now + KICK_GLOW_TIME

    def physics_step(self, dt):
        remaining = dt
        while remaining > 1e-9:
            step = MAX_SUBSTEP if remaining > MAX_SUBSTEP else remaining
            remaining -= step
            self._substep(step)
        self.check_goal()

    def _substep(self, dt):
        b = self.ball
        b["x"] += b["vx"] * dt
        b["y"] += b["vy"] * dt
        b["vx"] *= BALL_FRICTION
        b["vy"] *= BALL_FRICTION

        if b["y"] - BALL_R < 0:
            b["y"] = BALL_R
            b["vy"] = -b["vy"] * WALL_BOUNCE
        elif b["y"] + BALL_R > FIELD_H:
            b["y"] = FIELD_H - BALL_R
            b["vy"] = -b["vy"] * WALL_BOUNCE

        in_goal_y = GOAL_TOP < b["y"] < GOAL_BOTTOM
        if not in_goal_y:
            if b["x"] - BALL_R < 0:
                b["x"] = BALL_R
                b["vx"] = -b["vx"] * WALL_BOUNCE
            elif b["x"] + BALL_R > FIELD_W:
                b["x"] = FIELD_W - BALL_R
                b["vx"] = -b["vx"] * WALL_BOUNCE
        else:
            if b["x"] - BALL_R < -GOAL_DEPTH:
                b["x"] = -GOAL_DEPTH + BALL_R
                b["vx"] = -b["vx"] * WALL_BOUNCE
            elif b["x"] + BALL_R > FIELD_W + GOAL_DEPTH:
                b["x"] = FIELD_W + GOAL_DEPTH - BALL_R
                b["vx"] = -b["vx"] * WALL_BOUNCE

        for p in self.players.values():
            dx = b["x"] - p.x
            dy = b["y"] - p.y
            d = math.hypot(dx, dy)
            min_d = PLAYER_R + BALL_R
            if 0 < d < min_d:
                ux, uy = dx / d, dy / d
                overlap = min_d - d
                b["x"] += ux * overlap
                b["y"] += uy * overlap
                rel_vx = b["vx"] - p.vx
                rel_vy = b["vy"] - p.vy
                dot = rel_vx * ux + rel_vy * uy
                if dot < 0:
                    b["vx"] -= (1 + PLAYER_BALL_BOUNCE) * dot * ux
                    b["vy"] -= (1 + PLAYER_BALL_BOUNCE) * dot * uy

        plist = list(self.players.values())
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                p1, p2 = plist[i], plist[j]
                dx = p2.x - p1.x
                dy = p2.y - p1.y
                d = math.hypot(dx, dy)
                min_d = PLAYER_R * 2
                if 0 < d < min_d:
                    ux, uy = dx / d, dy / d
                    overlap = (min_d - d) * PLAYER_PLAYER_BOUNCE
                    p1.x -= ux * overlap / 2
                    p1.y -= uy * overlap / 2
                    p2.x += ux * overlap / 2
                    p2.y += uy * overlap / 2
                    p1.x, p1.y = self.clamp_player_pos(p1.x, p1.y)
                    p2.x, p2.y = self.clamp_player_pos(p2.x, p2.y)

    def check_goal(self):
        b = self.ball
        scorer = None
        if b["x"] + BALL_R < 0 and GOAL_TOP < b["y"] < GOAL_BOTTOM:
            scorer = "right"
        elif b["x"] - BALL_R > FIELD_W and GOAL_TOP < b["y"] < GOAL_BOTTOM:
            scorer = "left"
        if scorer:
            self.score[scorer] += 1
            self.phase = "goal_pause"
            self.goal_pause_until = time.monotonic() + GOAL_PAUSE_TIME
            if self.score[scorer] >= self.win_score:
                self.winner = scorer
                self.phase_after_pause = "over"
            else:
                self.phase_after_pause = "battle"

    def tick_timer(self, dt):
        if self.phase in ("battle", "goal_pause"):
            self.timer -= dt
            if self.timer <= 0:
                self.timer = 0
                if self.score["left"] == self.score["right"]:
                    self.winner = "draw"
                else:
                    self.winner = "left" if self.score["left"] > self.score["right"] else "right"
                self.phase = "over"

    def update(self, now):
        dt = now - self.last_tick
        if dt <= 0:
            return
        if dt > 0.25:
            dt = 0.25
        self.last_tick = now

        if self.phase == "goal_pause" and now >= self.goal_pause_until:
            if self.phase_after_pause == "over":
                self.phase = "over"
            else:
                self.phase = "battle"
                self.reset_positions()

        if self.phase == "battle":
            self.physics_step(dt)

        self.tick_timer(dt)


async def handle_create(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    user_id = str(data.get("user_id", "")).strip()
    user_name = str(data.get("user_name", "Игрок")).strip() or "Игрок"
    game_name = str(data.get("game_name", "Игра")).strip() or "Игра"
    max_players = data.get("max_players", 4)
    win_score = data.get("win_score", 5)
    match_time = data.get("match_time", 180)

    if not user_id:
        return web.json_response({"ok": False, "error": "no_user_id"}, status=400)
    if max_players not in (2, 4, 6):
        max_players = 4
    if win_score not in (3, 5, 7, 10):
        win_score = 5
    if match_time not in (60, 120, 180, 300):
        match_time = 180

    code = gen_code()
    room = Room(code, game_name, user_id, max_players, win_score, match_time)
    ROOMS[code] = room
    room.add_player(user_id, user_name)
    return web.json_response({"ok": True, "code": code})


async def handle_join(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    code = str(data.get("code", "")).strip()
    user_id = str(data.get("user_id", "")).strip()
    user_name = str(data.get("user_name", "Игрок")).strip() or "Игрок"

    room = ROOMS.get(code)
    if not room:
        return web.json_response({"ok": False, "error": "not_found"})

    ok, err = room.add_player(user_id, user_name)
    if not ok:
        return web.json_response({"ok": False, "error": err})
    return web.json_response({"ok": True, "code": code})


async def handle_list(request):
    items = []
    for room in ROOMS.values():
        if room.phase != "over" and len(room.players) < room.max_players:
            items.append(room.public_summary())
    return web.json_response({"ok": True, "items": items})


async def handle_ws(request):
    code = request.match_info.get("code", "")
    user_id = request.query.get("uid", "")
    room = ROOMS.get(code)

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    if not room or user_id not in room.players:
        await ws.send_json({"type": "error", "error": "not_found"})
        await ws.close()
        return ws

    player = room.players[user_id]
    player.online = True
    player.ws = ws

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get("action") == "move":
                    room.apply_move(user_id, data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        player.online = False
        if player.ws is ws:
            player.ws = None
        if all(not p.online for p in room.players.values()):
            room.empty_since = time.monotonic()

    return ws


def register_haxball_routes(app):
    app.router.add_post("/api/haxball/create", handle_create)
    app.router.add_post("/api/haxball/join", handle_join)
    app.router.add_get("/api/haxball/list", handle_list)
    app.router.add_get("/ws/haxball/{code}", handle_ws)


async def haxball_watchdog():
    while True:
        now = time.monotonic()
        dead_codes = []
        sends = []
        for code, room in list(ROOMS.items()):
            room.update(now)
            if now - room.last_broadcast >= BROADCAST_INTERVAL:
                room.last_broadcast = now
                for p in list(room.players.values()):
                    ws = p.ws
                    if ws is not None and not ws.closed:
                        sends.append((ws, room.snapshot(p.user_id)))
            if room.empty_since and now - room.empty_since > ROOM_TTL_EMPTY:
                dead_codes.append(code)
        for code in dead_codes:
            ROOMS.pop(code, None)

        # Параллельная рассылка — не блокирует тик физики
        if sends:
            await asyncio.gather(
                *[ws.send_json(snap) for ws, snap in sends],
                return_exceptions=True
            )

        await asyncio.sleep(TICK_DT)
