"""
Haxball-клон для Telegram Mini App.
Экспорт: register_haxball_routes(app), haxball_watchdog()
"""

import asyncio
import json
import logging
import math
import os
import random
import sqlite3
import string
import time

from aiohttp import web, WSMsgType

log = logging.getLogger("haxball")

FIELD_W = 840
FIELD_H = 400
GOAL_TOP = 150
GOAL_BOTTOM = 250
GOAL_DEPTH = 18
CORNER_R = 40.0

PLAYER_R = 15
BALL_R = 8

KICK_RANGE = PLAYER_R + BALL_R + 8
KICK_POWER = 400.0
KICK_VEL_BONUS = 1.1
KICK_COOLDOWN = 0.28
KICK_GLOW_TIME = 0.15

BALL_FRICTION = 0.994
WALL_BOUNCE = 0.85
PLAYER_BALL_BOUNCE = 0.5
PLAYER_PLAYER_BOUNCE = 0.55

BOUNCE_VALUES = {"low": 0.70, "normal": 0.85, "high": 0.95}
FIELD_COLORS = ("gray", "green", "blue", "dark")
SPEED_VALUES = (80, 100, 120)

TEAM_COLORS = (
    "#5689e5",
    "#e56e56",
    "#4eaa5e",
    "#e0c93f",
    "#9b5de5",
    "#e88c3f",
)
DEFAULT_LEFT_COLOR = TEAM_COLORS[0]
DEFAULT_RIGHT_COLOR = TEAM_COLORS[1]

TEAM_NAME_MAX = 12

TELEPORT_GUARD = 200.0

TICK_HZ = 60
TICK_DT = 1.0 / TICK_HZ
MAX_SUBSTEP = 0.005
BROADCAST_INTERVAL = 0.033

GOAL_PAUSE_TIME = 2.0
COUNTDOWN_TIME = 5.0
ROOM_TTL_EMPTY = 60.0

PROFILE_NAME_MAX = 16
JERSEY_MAX = 99

CGROUP_WALL = 1
CGROUP_BALL = 2
CGROUP_RED = 4
CGROUP_BLUE = 8

MASK_BALL = CGROUP_WALL | CGROUP_RED | CGROUP_BLUE
MASK_RED = CGROUP_WALL | CGROUP_BALL | CGROUP_BLUE
MASK_BLUE = CGROUP_WALL | CGROUP_BALL | CGROUP_RED

SLOT_X = {
    "left": {1: 330.0, 2: 170.0, 3: 60.0},
    "right": {1: 510.0, 2: 670.0, 3: 780.0},
}

CORNERS = (
    (CORNER_R, CORNER_R, -1, -1),
    (FIELD_W - CORNER_R, CORNER_R, 1, -1),
    (CORNER_R, FIELD_H - CORNER_R, -1, 1),
    (FIELD_W - CORNER_R, FIELD_H - CORNER_R, 1, 1),
)

ROOMS = {}

DB_PATH = "/app/data/haxball.db"

PROFILE_COLS = ("user_id", "name", "jersey", "matches", "goals", "wins", "losses", "draws")


def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS haxball_profiles ("
        "user_id TEXT PRIMARY KEY, "
        "name TEXT, "
        "jersey INTEGER DEFAULT 0, "
        "matches INTEGER DEFAULT 0, "
        "goals INTEGER DEFAULT 0, "
        "wins INTEGER DEFAULT 0, "
        "losses INTEGER DEFAULT 0, "
        "draws INTEGER DEFAULT 0)"
    )
    conn.commit()
    return conn


DB = _init_db()


def empty_profile(uid):
    return {
        "user_id": uid,
        "name": "",
        "jersey": 0,
        "matches": 0,
        "goals": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
    }


def db_get_profile(uid):
    try:
        cur = DB.execute(
            "SELECT user_id, name, jersey, matches, goals, wins, losses, draws "
            "FROM haxball_profiles WHERE user_id = ?",
            (uid,),
        )
        row = cur.fetchone()
    except sqlite3.Error:
        log.exception("db_get_profile failed")
        return empty_profile(uid)
    if row is None:
        return empty_profile(uid)
    prof = dict(zip(PROFILE_COLS, row))
    if prof["name"] is None:
        prof["name"] = ""
    for key in ("jersey", "matches", "goals", "wins", "losses", "draws"):
        if prof[key] is None:
            prof[key] = 0
    return prof


def db_get_jersey(uid):
    if not uid:
        return 0
    try:
        cur = DB.execute("SELECT jersey FROM haxball_profiles WHERE user_id = ?", (uid,))
        row = cur.fetchone()
    except sqlite3.Error:
        log.exception("db_get_jersey failed")
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return int(clamp(int(row[0]), 0, JERSEY_MAX))
    except (TypeError, ValueError):
        return 0


def db_save_profile(uid, name, jersey):
    try:
        DB.execute(
            "INSERT INTO haxball_profiles (user_id, name, jersey) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, jersey = excluded.jersey",
            (uid, name, jersey),
        )
        DB.commit()
        return True
    except sqlite3.Error:
        log.exception("db_save_profile failed")
        return False


def _ensure_row(uid):
    DB.execute(
        "INSERT OR IGNORE INTO haxball_profiles (user_id, name) VALUES (?, '')",
        (uid,),
    )


def add_goal_for_player(uid):
    try:
        _ensure_row(uid)
        DB.execute("UPDATE haxball_profiles SET goals = goals + 1 WHERE user_id = ?", (uid,))
        DB.commit()
    except sqlite3.Error:
        log.exception("add_goal_for_player failed")


def update_stats_for_player(uid, result):
    col = {"win": "wins", "loss": "losses", "draw": "draws"}.get(result)
    if col is None:
        return
    try:
        _ensure_row(uid)
        DB.execute(
            "UPDATE haxball_profiles SET matches = matches + 1, "
            + col + " = " + col + " + 1 WHERE user_id = ?",
            (uid,),
        )
        DB.commit()
    except sqlite3.Error:
        log.exception("update_stats_for_player failed")


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


def clean_team_name(raw, default):
    s = " ".join((raw or "").split())
    if not s:
        return default
    return s[:TEAM_NAME_MAX]


def clean_team_color(raw, default):
    if raw in TEAM_COLORS:
        return raw
    return default


class Player:
    def __init__(self, user_id, name, team, slot, jersey=0):
        self.user_id = user_id
        self.name = (name or "Игрок")[:16]
        self.team = team
        self.slot = slot
        self.jersey = jersey
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.kick = False
        self.kick_glow_until = 0.0
        self.kick_cooldown_until = 0.0
        self.online = True
        self.ws = None
        if team == "left":
            self.c_group = CGROUP_RED
            self.c_mask = MASK_RED
        else:
            self.c_group = CGROUP_BLUE
            self.c_mask = MASK_BLUE
        self.spawn()

    def spawn(self):
        default_x = 330.0 if self.team == "left" else 510.0
        self.x = SLOT_X[self.team].get(self.slot, default_x)
        self.y = FIELD_H / 2.0
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
            "jersey": self.jersey,
            "kick_id].jersey =_glow": 1 if now < self.kick_glow_until else 0,
            "online": self.online,
        }


class Room:
    def __init__(self, code, name, host_id, max_players, match_time,
                 field_color="gray", player_speed=100, ball_bounce="normal",
                 is_private=False, left_name="СИНИЕ", right_name="КРАСНЫЕ",
                 left_color=DEFAULT_LEFT_COLOR, right_color=DEFAULT_RIGHT_COLOR):
        self.code = code
        self.name = (name or "Игра")[:24]
        self.host_id = host_id
        self.max_players = max_players
        self.match_time_total = match_time
        self.timer = float(match_time)
        self.field_color = field_color
        self.player_speed = player_speed
        self.ball_bounce = ball_bounce
        self.wall_bounce = BOUNCE_VALUES.get(ball_bounce, WALL_BOUNCE)
        self.is_private = bool(is_private)
        self.left_name = left_name
        self.right_name = right_name
        self.left_color = left_color
        self.right_color = right_color

        self.phase = "waiting"
        self.score = {"left": 0, "right": 0}
        self.winner = None
        self.players = {}
        self.ball = {"x": FIELD_W / 2.0, "y": FIELD_H / 2.0, "vx": 0.0, "vy": 0.0}
        self.ball_group = CGROUP_BALL
        self.ball_mask = MASK_BALL
        self.last_kicker_uid = None
        self.goal_pause_until = 0.0
        self.countdown_end = 0.0
        self.match_started_at = 0.0
        self.goal_log = []
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
            self.players[user db_get_jersey(user_id)
            return True, None
        if self.phase == "over":
            return False, "game_over"
        if len(self.players) >= self.max_players:
            return False, "room_full"
        team, slot = self.next_team_slot()
        if team is None:
            return False, "room_full"
        p = Player(user_id, name, team, slot, db_get_jersey(user_id))
        self.players[user_id] = p
        self.empty_since = None
        return True, None

    def begin_countdown(self, now):
        self.reset_positions()
        self.phase = "countdown"
        self.countdown_end = now + COUNTDOWN_TIME

    def begin_match(self):
        if self.phase != "waiting":
            return False, "bad_phase"
        if len(self.players) < 2:
            return False, "not_enough_players"
        self.score = {"left": 0, "right": 0}
        self.winner = None
        self.timer = float(self.match_time_total)
        self.match_started_at = time.monotonic()
        self.goal_log = []
        self.begin_countdown(time.monotonic())
        return True, None

    def restart(self):
        if self.phase != "over":
            return False, "bad_phase"
        self.score = {"left": 0, "right": 0}
        self.winner = None
        self.timer = float(self.match_time_total)
        self.match_started_at = time.monotonic()
        self.goal_log = []
        self.begin_countdown(time.monotonic())
        return True, None

    def reset_positions(self):
        self.ball = {"x": FIELD_W / 2.0, "y": FIELD_H / 2.0, "vx": 0.0, "vy": 0.0}
        self.last_kicker_uid = None
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
        if self.phase == "countdown":
            countdown_left = round(max(0.0, self.countdown_end - now), 2)
        else:
            countdown_left = 0.0
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
                "max_players": self.max_players,
                "countdown_left": countdown_left,
                "field_color": self.field_color,
                "player_speed": self.player_speed,
                "ball_bounce": self.ball_bounce,
                "host_id": self.host_id,
                "is_private": self.is_private,
                "left_name": self.left_name,
                "right_name": self.right_name,
                "left_color": self.left_color,
                "right_color": self.right_color,
                "goal_log": list(self.goal_log),
            },
        }

    def clamp_player_pos(self, x, y):
        y = clamp(y, PLAYER_R, FIELD_H - PLAYER_R)
        in_mouth = (GOAL_TOP + 2) <= y <= (GOAL_BOTTOM - 2)
        if in_mouth:
            x = clamp(x, -GOAL_DEPTH + 2, FIELD_W + GOAL_DEPTH - 2)
        else:
            x = clamp(x, PLAYER_R, FIELD_W - PLAYER_R)
        cx = None
        cy = None
        if x < CORNER_R:
            cx = CORNER_R
        elif x > FIELD_W - CORNER_R:
            cx = FIELD_W - CORNER_R
        if y < CORNER_R:
            cy = CORNER_R
        elif y > FIELD_H - CORNER_R:
            cy = FIELD_H - CORNER_R
        if cx is not None and cy is not None:
            dx = x - cx
            dy = y - cy
            d = math.hypot(dx, dy)
            lim = CORNER_R - PLAYER_R
            if d > lim and d > 0:
                x = cx + dx / d * lim
                y = cy + dy / d * lim
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
                self.last_kicker_uid = p.user_id

    def physics_step(self, dt):
        remaining = dt
        while remaining > 1e-9:
            step = MAX_SUBSTEP if remaining > MAX_SUBSTEP else remaining
            remaining -= step
            self._substep(step)
        self.check_goal()

    def _corner_collide(self, b):
        bounce = self.wall_bounce
        lim = CORNER_R - BALL_R
        for cx, cy, sx, sy in CORNERS:
            dx = b["x"] - cx
            dy = b["y"] - cy
            if dx * sx > 0 or dy * sy > 0:
                continue
            d = math.hypot(dx, dy)
            if d < lim and d > 0.001:
                ux = dx / d
                uy = dy / d
                b["x"] = cx + ux * lim
                b["y"] = cy + uy * lim
                dot = b["vx"] * ux + b["vy"] * uy
                if dot < 0:
                    b["vx"] -= (1 + bounce) * dot * ux
                    b["vy"] -= (1 + bounce) * dot * uy

    def _substep(self, dt):
        b = self.ball
        bounce = self.wall_bounce
        b["x"] += b["vx"] * dt
        b["y"] += b["vy"] * dt
        b["vx"] *= BALL_FRICTION
        b["vy"] *= BALL_FRICTION
        self._corner_collide(b)
        if b["y"] - BALL_R < 0:
            b["y"] = BALL_R
            b["vy"] = -b["vy"] * bounce
        elif b["y"] + BALL_R > FIELD_H:
            b["y"] = FIELD_H - BALL_R
            b["vy"] = -b["vy"] * bounce
        in_goal_y = GOAL_TOP < b["y"] < GOAL_BOTTOM
        if not in_goal_y:
            if b["x"] - BALL_R < 0:
                b["x"] = BALL_R
                b["vx"] = -b["vx"] * bounce
            elif b["x"] + BALL_R > FIELD_W:
                b["x"] = FIELD_W - BALL_R
                b["vx"] = -b["vx"] * bounce
        else:
            if b["x"] - BALL_R < -GOAL_DEPTH:
                b["x"] = -GOAL_DEPTH + BALL_R
                b["vx"] = -b["vx"] * bounce
            elif b["x"] + BALL_R > FIELD_W + GOAL_DEPTH:
                b["x"] = FIELD_W + GOAL_DEPTH - BALL_R
                b["vx"] = -b["vx"] * bounce
        for p in self.players.values():
            if not (self.ball_mask & p.c_group):
                continue
            if not (p.c_mask & self.ball_group):
                continue
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
                if not (p1.c_mask & p2.c_group):
                    continue
                if not (p2.c_mask & p1.c_group):
                    continue
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

    def credit_goal(self, scorer):
        uid = self.last_kicker_uid
        self.last_kicker_uid = None
        if not uid:
            return
        p = self.players.get(uid)
        if p is None or p.team != scorer:
            return
        add_goal_for_player(uid)
        if self.match_started_at > 0:
            at_sec = max(0, int(time.monotonic() - self.match_started_at))
        else:
            at_sec = 0
        self.goal_log.append({
            "team": scorer,
            "uid": p.user_id,
            "name": p.name,
            "jersey": p.jersey if p.jersey > 0 else p.slot,
            "at_sec": at_sec,
        })
        if len(self.goal_log) > 60:
            self.goal_log = self.goal_log[-60:]

    def check_goal(self):
        b = self.ball
        scorer = None
        if b["x"] < 0 and GOAL_TOP < b["y"] < GOAL_BOTTOM:
            scorer = "right"
        elif b["x"] > FIELD_W and GOAL_TOP < b["y"] < GOAL_BOTTOM:
            scorer = "left"
        if scorer:
            self.score[scorer] += 1
            self.phase = "goal_pause"
            self.goal_pause_until = time.monotonic() + GOAL_PAUSE_TIME
            self.credit_goal(scorer)

    def save_match_stats(self):
        for p in self.players.values():
            if self.winner == "draw":
                result = "draw"
            elif p.team == self.winner:
                result = "win"
            else:
                result = "loss"
            update_stats_for_player(p.user_id, result)

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
                self.save_match_stats()

    def update(self, now):
        dt = now - self.last_tick
        if dt <= 0:
            return
        if dt > 0.25:
            dt = 0.25
        self.last_tick = now
        if self.phase == "goal_pause" and now >= self.goal_pause_until:
            self.begin_countdown(now)
        if self.phase == "countdown" and now >= self.countdown_end:
            self.phase = "battle"
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
    match_time = data.get("match_time", 180)
    field_color = data.get("field_color", "gray")
    player_speed = data.get("player_speed", 100)
    ball_bounce = data.get("ball_bounce", "normal")
    is_private = bool(data.get("is_private", False))
    left_name = clean_team_name(data.get("left_name"), "СИНИЕ")
    right_name = clean_team_name(data.get("right_name"), "КРАСНЫЕ")
    left_color = clean_team_color(data.get("left_color"), DEFAULT_LEFT_COLOR)
    right_color = clean_team_color(data.get("right_color"), DEFAULT_RIGHT_COLOR)
    if left_color == right_color:
        right_color = DEFAULT_RIGHT_COLOR if left_color != DEFAULT_RIGHT_COLOR else DEFAULT_LEFT_COLOR

    if not user_id:
        return web.json_response({"ok": False, "error": "no_user_id"}, status=400)
    if max_players not in (2, 4, 6):
        max_players = 4
    if match_time not in (60, 120, 180, 300):
        match_time = 180
    if field_color not in FIELD_COLORS:
        field_color = "gray"
    try:
        player_speed = int(player_speed)
    except (TypeError, ValueError):
        player_speed = 100
    if player_speed not in SPEED_VALUES:
        player_speed = 100
    if ball_bounce not in BOUNCE_VALUES:
        ball_bounce = "normal"

    code = gen_code()
    room = Room(code, game_name, user_id, max_players, match_time,
                field_color, player_speed, ball_bounce,
                is_private, left_name, right_name, left_color, right_color)
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


async def handle_start(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)
    code = str(data.get("code", "")).strip()
    user_id = str(data.get("user_id", "")).strip()
    room = ROOMS.get(code)
    if not room:
        return web.json_response({"ok": False, "error": "not_found"})
    if user_id != room.host_id:
        return web.json_response({"ok": False, "error": "not_host"})
    ok, err = room.begin_match()
    if not ok:
        return web.json_response({"ok": False, "error": err})
    return web.json_response({"ok": True})


async def handle_restart(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)
    code = str(data.get("code", "")).strip()
    user_id = str(data.get("user_id", "")).strip()
    room = ROOMS.get(code)
    if not room:
        return web.json_response({"ok": False, "error": "not_found"})
    if user_id != room.host_id:
        return web.json_response({"ok": False, "error": "not_host"})
    ok, err = room.restart()
    if not ok:
        return web.json_response({"ok": False, "error": err})
    return web.json_response({"ok": True})


async def handle_list(request):
    items = []
    for room in ROOMS.values():
        if room.is_private:
            continue
        if room.phase != "over" and len(room.players) < room.max_players:
            items.append(room.public_summary())
    return web.json_response({"ok": True, "items": items})


async def handle_profile_get(request):
    uid = str(request.query.get("uid", "")).strip()
    if not uid:
        return web.json_response({"ok": False, "error": "no_user_id"}, status=400)
    return web.json_response({"ok": True, "profile": db_get_profile(uid)})


async def handle_profile_post(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)
    uid = str(data.get("user_id", "")).strip()
    if not uid:
        return web.json_response({"ok": False, "error": "no_user_id"}, status=400)
    name = str(data.get("name", "")).strip()[:PROFILE_NAME_MAX]
    try:
        jersey = int(data.get("jersey", 0))
    except (TypeError, ValueError):
        jersey = 0
    jersey = int(clamp(jersey, 0, JERSEY_MAX))
    if not db_save_profile(uid, name, jersey):
        return web.json_response({"ok": False, "error": "db_error"}, status=500)
    return web.json_response({"ok": True})


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
                act = data.get("action")
                if act == "move":
                    room.apply_move(user_id, data)
                elif act == "leave":
                    room.players.pop(user_id, None)
                    if not room.players:
                        ROOMS.pop(code, None)
                    elif user_id == room.host_id:
                        room.host_id = next(iter(room.players))
                    break
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        player.online = False
        if player.ws is ws:
            player.ws = None
        if room.code in ROOMS and all(not p.online for p in room.players.values()):
            room.empty_since = time.monotonic()
    return ws


def register_haxball_routes(app):
    app.router.add_post("/api/haxball/create", handle_create)
    app.router.add_post("/api/haxball/join", handle_join)
    app.router.add_post("/api/haxball/start", handle_start)
    app.router.add_post("/api/haxball/restart", handle_restart)
    app.router.add_get("/api/haxball/list", handle_list)
    app.router.add_get("/api/haxball/profile", handle_profile_get)
    app.router.add_post("/api/haxball/profile", handle_profile_post)
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
        if sends:
            await asyncio.gather(
                *[ws.send_json(snap) for ws, snap in sends],
                return_exceptions=True,
            )
        await asyncio.sleep(TICK_DT)
