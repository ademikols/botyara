import asyncio
import json
import math
import random
import string
import time as _time
from dataclasses import dataclass, field
from typing import Dict, Optional
from aiohttp import web
from datetime import datetime

FIELD_WIDTH = 800
FIELD_HEIGHT = 500
FIELD_HALF_WIDTH = FIELD_WIDTH / 2
FIELD_HALF_HEIGHT = FIELD_HEIGHT / 2

PLAYER_RADIUS = 20
BALL_RADIUS = 10
GOAL_HEIGHT = 140
GOAL_OFFSET_Y = (FIELD_HEIGHT - GOAL_HEIGHT) / 2

# Мгновенный разгон и быстрое торможение — как в оригинале Haxball
MAX_SPEED = 360.0
ACCELERATION = 3600.0
FRICTION = 0.82
WALL_ELASTICITY = 0.85
PLAYER_PLAYER_ELASTICITY = 0.55
PLAYER_BALL_ELASTICITY = 0.85
BALL_FRICTION = 0.994
KICK_FORCE = 420.0
KICK_COOLDOWN = 0.28
KICK_BONUS_FROM_PLAYER_VEL = 1.10

TICK_RATE = 60
SUBSTEPS = 3
DT = 1.0 / TICK_RATE

SNAPSHOT_RATE = 25
SNAPSHOT_EVERY = max(1, TICK_RATE // SNAPSHOT_RATE)

MAX_PLAYERS = 6
TEAM_CAP = 3
MATCH_TIME = 180.0
GOAL_PAUSE = 2.0
WIN_SCORE = 5

LEFT_SPAWN = [(140.0, 120.0), (140.0, 250.0), (140.0, 380.0)]
RIGHT_SPAWN = [(660.0, 120.0), (660.0, 250.0), (660.0, 380.0)]


@dataclass
class Player:
    user_id: int
    name: str
    team: str
    slot: int = 0
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    ix: float = 0.0
    iy: float = 0.0
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    kick: bool = False
    kick_cd: float = 0.0
    kick_glow: float = 0.0

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "name": self.name,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "team": self.team,
            "kick_glow": round(self.kick_glow, 2),
        }


@dataclass
class Ball:
    x: float = FIELD_HALF_WIDTH
    y: float = FIELD_HALF_HEIGHT
    vx: float = 0.0
    vy: float = 0.0

    def to_dict(self):
        return {"x": round(self.x, 2), "y": round(self.y, 2)}


@dataclass
class HaxballGame:
    code: str
    name: str
    host_id: int
    host_name: str
    created_at: datetime = field(default_factory=datetime.now)
    players: Dict[int, Player] = field(default_factory=dict)
    ball: Ball = field(default_factory=Ball)
    score: Dict[str, int] = field(default_factory=lambda: {"left": 0, "right": 0})
    phase: str = "waiting"
    timer: float = MATCH_TIME
    pause_until: float = 0.0
    winner: Optional[str] = None
    sockets: Dict[int, web.WebSocketResponse] = field(default_factory=dict)
    last_goal_team: Optional[str] = None

    def to_state(self, my_user_id: int) -> dict:
        my_player = self.players.get(my_user_id)
        my_side = my_player.team if my_player else None
        return {
            "phase": self.phase,
            "score": dict(self.score),
            "timer": max(0, int(self.timer)),
            "players": [p.to_dict() for p in self.players.values()],
            "ball": self.ball.to_dict(),
            "my_side": my_side,
            "winner": self.winner,
            "last_goal_team": self.last_goal_team,
        }


games: Dict[str, HaxballGame] = {}
player_games: Dict[int, str] = {}


def generate_code():
    return "".join(random.choices(string.digits, k=6))


def team_count(game, team):
    return sum(1 for p in game.players.values() if p.team == team)


def pick_team(game):
    lc = team_count(game, "left")
    rc = team_count(game, "right")
    if lc < rc: return "left"
    if rc < lc: return "right"
    if lc < TEAM_CAP: return "left"
    if rc < TEAM_CAP: return "right"
    return None


def spawn_for(team, slot):
    arr = LEFT_SPAWN if team == "left" else RIGHT_SPAWN
    return arr[min(slot, len(arr) - 1)]


def reset_ball(game):
    game.ball.x = FIELD_HALF_WIDTH
    game.ball.y = FIELD_HALF_HEIGHT
    game.ball.vx = 0.0
    game.ball.vy = 0.0


def reset_positions(game):
    for p in game.players.values():
        p.vx = p.vy = 0.0
        p.up = p.down = p.left = p.right = p.kick = False
        p.ix = p.iy = 0.0
        p.kick_cd = 0.0
        x, y = spawn_for(p.team, p.slot)
        p.x = x
        p.y = y


# ---------- ФИЗИКА ----------

def update_player(player, dt):
    ix = player.ix
    iy = player.iy
    if ix == 0.0 and iy == 0.0:
        if player.up: iy -= 1.0
        if player.down: iy += 1.0
        if player.left: ix -= 1.0
        if player.right: ix += 1.0

    mag = math.hypot(ix, iy)
    if mag > 1.0:
        ix /= mag
        iy /= mag

    if mag > 0.05:
        # Есть ввод — задаём скорость напрямую (мгновенный отклик)
        player.vx = ix * MAX_SPEED * min(1.0, mag)
        player.vy = iy * MAX_SPEED * min(1.0, mag)
    else:
        # Нет ввода — быстро тормозим
        k = FRICTION ** (dt * 60.0)
        player.vx *= k
        player.vy *= k
        if abs(player.vx) < 3.0: player.vx = 0.0
        if abs(player.vy) < 3.0: player.vy = 0.0

    player.x += player.vx * dt
    player.y += player.vy * dt

    if player.x - PLAYER_RADIUS < 0:
        player.x = PLAYER_RADIUS
        player.vx = abs(player.vx) * WALL_ELASTICITY
    if player.x + PLAYER_RADIUS > FIELD_WIDTH:
        player.x = FIELD_WIDTH - PLAYER_RADIUS
        player.vx = -abs(player.vx) * WALL_ELASTICITY
    if player.y - PLAYER_RADIUS < 0:
        player.y = PLAYER_RADIUS
        player.vy = abs(player.vy) * WALL_ELASTICITY
    if player.y + PLAYER_RADIUS > FIELD_HEIGHT:
        player.y = FIELD_HEIGHT - PLAYER_RADIUS
        player.vy = -abs(player.vy) * WALL_ELASTICITY

    if player.kick_cd > 0:
        player.kick_cd = max(0.0, player.kick_cd - dt)
    if player.kick_glow > 0:
        player.kick_glow = max(0.0, player.kick_glow - dt)


def update_ball(ball, dt):
    k = BALL_FRICTION ** (dt * 60.0)
    ball.vx *= k
    ball.vy *= k
    ball.x += ball.vx * dt
    ball.y += ball.vy * dt

    in_goal_y = GOAL_OFFSET_Y < ball.y < GOAL_OFFSET_Y + GOAL_HEIGHT

    if ball.x - BALL_RADIUS < 0 and not in_goal_y:
        ball.x = BALL_RADIUS
        ball.vx = abs(ball.vx) * WALL_ELASTICITY
    if ball.x + BALL_RADIUS > FIELD_WIDTH and not in_goal_y:
        ball.x = FIELD_WIDTH - BALL_RADIUS
        ball.vx = -abs(ball.vx) * WALL_ELASTICITY
    if ball.y - BALL_RADIUS < 0:
        ball.y = BALL_RADIUS
        ball.vy = abs(ball.vy) * WALL_ELASTICITY
    if ball.y + BALL_RADIUS > FIELD_HEIGHT:
        ball.y = FIELD_HEIGHT - BALL_RADIUS
        ball.vy = -abs(ball.vy) * WALL_ELASTICITY


def check_goal(ball):
    in_goal_y = GOAL_OFFSET_Y < ball.y < GOAL_OFFSET_Y + GOAL_HEIGHT
    if not in_goal_y:
        return None
    if ball.x + BALL_RADIUS < 0:
        return "right"
    if ball.x - BALL_RADIUS > FIELD_WIDTH:
        return "left"
    return None


def resolve_player_player(p1, p2):
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    dist = math.hypot(dx, dy)
    min_dist = PLAYER_RADIUS * 2
    if dist >= min_dist or dist < 0.0001:
        return
    nx = dx / dist
    ny = dy / dist
    overlap = min_dist - dist
    p1.x -= nx * overlap * 0.5
    p1.y -= ny * overlap * 0.5
    p2.x += nx * overlap * 0.5
    p2.y += ny * overlap * 0.5
    rvx = p2.vx - p1.vx
    rvy = p2.vy - p1.vy
    vel_n = rvx * nx + rvy * ny
    if vel_n > 0:
        return
    e = PLAYER_PLAYER_ELASTICITY
    j = -(1 + e) * vel_n / 2.0
    p1.vx -= j * nx
    p1.vy -= j * ny
    p2.vx += j * nx
    p2.vy += j * ny


def resolve_player_ball(player, ball):
    dx = ball.x - player.x
    dy = ball.y - player.y
    dist = math.hypot(dx, dy)
    min_dist = PLAYER_RADIUS + BALL_RADIUS
    if dist >= min_dist or dist < 0.0001:
        return False
    nx = dx / dist
    ny = dy / dist
    overlap = min_dist - dist
    ball.x += nx * overlap * 0.9
    ball.y += ny * overlap * 0.9
    player.x -= nx * overlap * 0.1
    player.y -= ny * overlap * 0.1

    if player.kick and player.kick_cd <= 0:
        base_x = nx * KICK_FORCE + player.vx * KICK_BONUS_FROM_PLAYER_VEL
        base_y = ny * KICK_FORCE + player.vy * KICK_BONUS_FROM_PLAYER_VEL
        ball.vx = base_x
        ball.vy = base_y
        player.kick_cd = KICK_COOLDOWN
        player.kick_glow = 0.25
        return True

    rvx = ball.vx - player.vx
    rvy = ball.vy - player.vy
    vel_n = rvx * nx + rvy * ny
    if vel_n < 0:
        e = PLAYER_BALL_ELASTICITY
        m_player = 3.0
        m_ball = 1.0
        j = -(1 + e) * vel_n / (1.0 / m_player + 1.0 / m_ball)
        player.vx -= j / m_player * nx
        player.vy -= j / m_player * ny
        ball.vx += j / m_ball * nx
        ball.vy += j / m_ball * ny
    return False


def step_physics(game, dt):
    for p in game.players.values():
        update_player(p, dt)
    players = list(game.players.values())
    for i in range(len(players)):
        for j in range(i + 1, len(players)):
            resolve_player_player(players[i], players[j])
    update_ball(game.ball, dt)
    for p in players:
        resolve_player_ball(p, game.ball)


def update_game_physics(game, dt):
    if game.phase != "battle":
        return
    if dt > 0.1:
        dt = 0.1
    for _ in range(SUBSTEPS):
        step_physics(game, dt / SUBSTEPS)

    goal = check_goal(game.ball)
    if goal:
        game.score[goal] += 1
        game.last_goal_team = goal
        reset_ball(game)
        reset_positions(game)
        if game.score[goal] >= WIN_SCORE:
            game.phase = "over"
            game.winner = goal
        else:
            game.phase = "goal_pause"
            game.pause_until = asyncio.get_event_loop().time() + GOAL_PAUSE


async def _kick_from_old_game(user_id):
    old_code = player_games.get(user_id)
    if not old_code:
        return
    old_game = games.get(old_code)
    if not old_game:
        player_games.pop(user_id, None)
        return
    old_game.players.pop(user_id, None)
    ws = old_game.sockets.pop(user_id, None)
    if ws and not ws.closed:
        try:
            await ws.close()
        except Exception:
            pass


# ---------- HTTP ----------

async def handle_create(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    user_id = data.get("user_id")
    user_name = (data.get("user_name") or "Player")[:24]
    game_name = (data.get("game_name") or "Haxball")[:32]

    if not user_id:
        return web.json_response({"ok": False, "error": "No user_id"})

    await _kick_from_old_game(user_id)

    code = generate_code()
    while code in games:
        code = generate_code()

    game = HaxballGame(code=code, name=game_name, host_id=user_id, host_name=user_name)
    p = Player(user_id=user_id, name=user_name, team="left", slot=0)
    p.x, p.y = spawn_for("left", 0)
    game.players[user_id] = p
    games[code] = game
    player_games[user_id] = code
    return web.json_response({"ok": True, "code": code})


async def handle_join(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    code = str(data.get("code") or "").strip()
    user_id = data.get("user_id")
    user_name = (data.get("user_name") or "Player")[:24]

    if not code or not user_id:
        return web.json_response({"ok": False, "error": "No code or user_id"})
    if code not in games:
        return web.json_response({"ok": False, "error": "Game not found"})

    game = games[code]

    if user_id in game.players:
        player_games[user_id] = code
        return web.json_response({"ok": True, "code": code})

    if game.phase == "over":
        return web.json_response({"ok": False, "error": "Game already finished"})
    if len(game.players) >= MAX_PLAYERS:
        return web.json_response({"ok": False, "error": "Game is full"})

    team = pick_team(game)
    if team is None:
        return web.json_response({"ok": False, "error": "Teams are full"})

    await _kick_from_old_game(user_id)

    slot = team_count(game, team)
    p = Player(user_id=user_id, name=user_name, team=team, slot=slot)
    p.x, p.y = spawn_for(team, slot)
    game.players[user_id] = p
    player_games[user_id] = code

    if game.phase == "waiting" and len(game.players) >= 2:
        game.phase = "battle"
        game.timer = MATCH_TIME
        game.score = {"left": 0, "right": 0}
        reset_ball(game)
        reset_positions(game)

    return web.json_response({"ok": True, "code": code})


async def handle_list(request):
    items = []
    for code, game in games.items():
        if game.phase == "over":
            continue
        items.append({
            "code": code, "name": game.name,
            "host": game.host_name,
            "players": len(game.players),
            "max": MAX_PLAYERS,
        })
    return web.json_response({"ok": True, "items": items})


async def handle_websocket(request):
    code = request.match_info.get("code")
    try:
        user_id = int(request.query.get("uid", "0"))
    except ValueError:
        user_id = 0

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    game = games.get(code)
    if not game or user_id not in game.players:
        try:
            await ws.send_json({"type": "error", "error": "Not in game"})
        except Exception:
            pass
        await ws.close()
        return ws

    game.sockets[user_id] = ws

    try:
        await ws.send_json({"type": "state", "state": game.to_state(user_id)})
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                player = game.players.get(user_id)
                if not player:
                    continue
                if data.get("action") == "move":
                    try:
                        player.ix = float(data.get("ix", 0.0))
                        player.iy = float(data.get("iy", 0.0))
                    except (TypeError, ValueError):
                        player.ix = player.iy = 0.0
                    player.up = bool(data.get("up"))
                    player.down = bool(data.get("down"))
                    player.left = bool(data.get("left"))
                    player.right = bool(data.get("right"))
                    player.kick = bool(data.get("kick"))
            elif msg.type == web.WSMsgType.ERROR:
                break
    finally:
        game.sockets.pop(user_id, None)
        if not game.sockets:
            for uid in list(game.players.keys()):
                player_games.pop(uid, None)
            games.pop(code, None)

    return ws


async def haxball_watchdog():
    tick_counter = 0
    loop = asyncio.get_event_loop()
    last = _time.monotonic()
    while True:
        try:
            now_real = _time.monotonic()
            real_dt = now_real - last
            last = now_real
            if real_dt > 0.1:
                real_dt = 0.1
            if real_dt <= 0:
                real_dt = DT

            tick_counter += 1
            now = loop.time()

            for code in list(games.keys()):
                game = games.get(code)
                if not game:
                    continue

                if game.phase == "battle":
                    game.timer -= real_dt
                    if game.timer <= 0:
                        game.timer = 0
                        game.phase = "over"
                        if game.score["left"] > game.score["right"]:
                            game.winner = "left"
                        elif game.score["right"] > game.score["left"]:
                            game.winner = "right"
                        else:
                            game.winner = "draw"
                    else:
                        update_game_physics(game, real_dt)

                elif game.phase == "goal_pause":
                    if now >= game.pause_until:
                        game.phase = "battle"

                if tick_counter % SNAPSHOT_EVERY == 0 and game.sockets:
                    for uid, ws in list(game.sockets.items()):
                        if ws.closed:
                            game.sockets.pop(uid, None)
                            continue
                        try:
                            await ws.send_json({"type": "state", "state": game.to_state(uid)})
                        except Exception:
                            game.sockets.pop(uid, None)

            elapsed = _time.monotonic() - now_real
            await asyncio.sleep(max(0.0, DT - elapsed))
        except Exception as e:
            print(f"[haxball] watchdog error: {e}")
            await asyncio.sleep(0.05)


def register_haxball_routes(app):
    app.router.add_post("/api/haxball/create", handle_create)
    app.router.add_post("/api/haxball/join", handle_join)
    app.router.add_get("/api/haxball/list", handle_list)
    app.router.add_get("/ws/haxball/{code}", handle_websocket)
