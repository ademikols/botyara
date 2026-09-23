import asyncio
import json
import math
import random
import string
from dataclasses import dataclass, field
from typing import Dict, Optional
from aiohttp import web
from datetime import datetime

FIELD_WIDTH = 800
FIELD_HEIGHT = 500
FIELD_HALF_WIDTH = FIELD_WIDTH / 2
FIELD_HALF_HEIGHT = FIELD_HEIGHT / 2

PLAYER_RADIUS = 15
BALL_RADIUS = 8
GOAL_HEIGHT = 120
GOAL_OFFSET_Y = (FIELD_HEIGHT - GOAL_HEIGHT) / 2

MAX_SPEED = 300
ACCELERATION = 800
FRICTION = 0.92
WALL_ELASTICITY = 0.9
PLAYER_PLAYER_ELASTICITY = 0.8
PLAYER_BALL_ELASTICITY = 0.95
BALL_FRICTION = 0.985
KICK_FORCE = 450
KICK_COOLDOWN = 0.35

TICK_RATE = 30
SNAPSHOT_RATE = 15
DT = 1.0 / TICK_RATE
SNAPSHOT_EVERY = TICK_RATE // SNAPSHOT_RATE

MAX_PLAYERS = 4
MATCH_TIME = 120.0
GOAL_PAUSE = 2.0
WIN_SCORE = 3


@dataclass
class Player:
    user_id: int
    name: str
    team: str
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    kick: bool = False
    kick_cd: float = 0.0

    def to_dict(self):
        return {
            "user_id": self.user_id, "name": self.name,
            "x": round(self.x, 2), "y": round(self.y, 2),
            "team": self.team,
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


def distance(x1, y1, x2, y2):
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def reset_ball(game: HaxballGame):
    game.ball.x = FIELD_HALF_WIDTH
    game.ball.y = FIELD_HALF_HEIGHT
    game.ball.vx = 0.0
    game.ball.vy = 0.0


def reset_positions(game: HaxballGame):
    for p in game.players.values():
        p.vx = p.vy = 0.0
        p.up = p.down = p.left = p.right = p.kick = False
        p.kick_cd = 0.0
        p.x = 150.0 if p.team == "left" else FIELD_WIDTH - 150.0
        p.y = FIELD_HALF_HEIGHT + random.uniform(-60, 60)


def update_player(player: Player, dt: float):
    ax = ay = 0.0
    if player.up:    ay -= ACCELERATION
    if player.down:  ay += ACCELERATION
    if player.left:  ax -= ACCELERATION
    if player.right: ax += ACCELERATION

    player.vx += ax * dt
    player.vy += ay * dt

    speed = math.sqrt(player.vx ** 2 + player.vy ** 2)
    if speed > MAX_SPEED:
        k = MAX_SPEED / speed
        player.vx *= k
        player.vy *= k

    if not (player.up or player.down or player.left or player.right):
        player.vx *= FRICTION
        player.vy *= FRICTION

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


def update_ball(ball: Ball, dt: float):
    ball.vx *= BALL_FRICTION
    ball.vy *= BALL_FRICTION
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


def check_goal(ball: Ball) -> Optional[str]:
    in_goal_y = GOAL_OFFSET_Y < ball.y < GOAL_OFFSET_Y + GOAL_HEIGHT
    if not in_goal_y:
        return None
    if ball.x + BALL_RADIUS < 0:
        return "right"
    if ball.x - BALL_RADIUS > FIELD_WIDTH:
        return "left"
    return None


def resolve_collision_circle(x1, y1, r1, vx1, vy1, m1,
                             x2, y2, r2, vx2, vy2, m2, elasticity):
    dx = x2 - x1
    dy = y2 - y1
    dist = math.sqrt(dx * dx + dy * dy)
    if dist < 0.001 or dist > r1 + r2:
        return vx1, vy1, vx2, vy2
    nx, ny = dx / dist, dy / dist
    dvx = vx2 - vx1
    dvy = vy2 - vy1
    dvn = dvx * nx + dvy * ny
    if dvn >= 0:
        return vx1, vy1, vx2, vy2
    impulse = -(1 + elasticity) * dvn / (1 / m1 + 1 / m2)
    vx1 -= impulse / m1 * nx
    vy1 -= impulse / m1 * ny
    vx2 += impulse / m2 * nx
    vy2 += impulse / m2 * ny
    return vx1, vy1, vx2, vy2


def update_game_physics(game: HaxballGame, dt: float):
    if game.phase != "battle":
        return

    for player in game.players.values():
        update_player(player, dt)

    players_list = list(game.players.values())
    for i in range(len(players_list)):
        for j in range(i + 1, len(players_list)):
            p1, p2 = players_list[i], players_list[j]
            dist = distance(p1.x, p1.y, p2.x, p2.y)
            if dist < PLAYER_RADIUS * 2 and dist > 0.001:
                nx = (p2.x - p1.x) / dist
                ny = (p2.y - p1.y) / dist
                overlap = PLAYER_RADIUS * 2 - dist
                p1.x -= nx * overlap / 2
                p1.y -= ny * overlap / 2
                p2.x += nx * overlap / 2
                p2.y += ny * overlap / 2
                p1.vx, p1.vy, p2.vx, p2.vy = resolve_collision_circle(
                    p1.x, p1.y, PLAYER_RADIUS, p1.vx, p1.vy, 1,
                    p2.x, p2.y, PLAYER_RADIUS, p2.vx, p2.vy, 1,
                    PLAYER_PLAYER_ELASTICITY
                )

    update_ball(game.ball, dt)

    for player in game.players.values():
        dist = distance(player.x, player.y, game.ball.x, game.ball.y)
        if dist < PLAYER_RADIUS + BALL_RADIUS and dist > 0.001:
            dx = (game.ball.x - player.x) / dist
            dy = (game.ball.y - player.y) / dist
            overlap = PLAYER_RADIUS + BALL_RADIUS - dist
            player.x -= dx * overlap / 2
            player.y -= dy * overlap / 2
            game.ball.x += dx * overlap / 2
            game.ball.y += dy * overlap / 2

            if player.kick and player.kick_cd <= 0:
                angle = math.atan2(dy, dx)
                game.ball.vx = math.cos(angle) * KICK_FORCE
                game.ball.vy = math.sin(angle) * KICK_FORCE
                player.kick_cd = KICK_COOLDOWN
            else:
                player.vx, player.vy, game.ball.vx, game.ball.vy = resolve_collision_circle(
                    player.x, player.y, PLAYER_RADIUS, player.vx, player.vy, 1.0,
                    game.ball.x, game.ball.y, BALL_RADIUS, game.ball.vx, game.ball.vy, 0.3,
                    PLAYER_BALL_ELASTICITY
                )

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


async def _kick_from_old_game(user_id: int):
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


async def handle_create(request: web.Request) -> web.Response:
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
    player = Player(user_id=user_id, name=user_name, team="left", x=150.0, y=FIELD_HALF_HEIGHT)
    game.players[user_id] = player
    games[code] = game
    player_games[user_id] = code
    return web.json_response({"ok": True, "code": code})


async def handle_join(request: web.Request) -> web.Response:
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

    await _kick_from_old_game(user_id)

    left_count = sum(1 for p in game.players.values() if p.team == "left")
    right_count = sum(1 for p in game.players.values() if p.team == "right")
    team = "right" if left_count > right_count else "left"
    x = 150.0 if team == "left" else FIELD_WIDTH - 150.0
    y = FIELD_HALF_HEIGHT + random.uniform(-60, 60)

    game.players[user_id] = Player(user_id=user_id, name=user_name, team=team, x=x, y=y)
    player_games[user_id] = code

    if game.phase == "waiting" and len(game.players) >= 2:
        game.phase = "battle"
        game.timer = MATCH_TIME
        game.score = {"left": 0, "right": 0}
        reset_ball(game)
        reset_positions(game)

    return web.json_response({"ok": True, "code": code})


async def handle_list(request: web.Request) -> web.Response:
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


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
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
    while True:
        try:
            tick_counter += 1
            now = loop.time()

            for code in list(games.keys()):
                game = games.get(code)
                if not game:
                    continue

                if game.phase == "battle":
                    game.timer -= DT
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
                        update_game_physics(game, DT)

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

            await asyncio.sleep(DT)
        except Exception as e:
            print(f"[haxball] watchdog error: {e}")
            await asyncio.sleep(0.1)


def register_haxball_routes(app: web.Application):
    app.router.add_post("/api/haxball/create", handle_create)
    app.router.add_post("/api/haxball/join", handle_join)
    app.router.add_get("/api/haxball/list", handle_list)
    app.router.add_get("/ws/haxball/{code}", handle_websocket)
