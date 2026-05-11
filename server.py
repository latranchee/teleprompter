"""Multi-room teleprompter server.

Path-based room isolation: each /{slug} is its own room with its own state.
Visiting / generates a fresh mkname slug and redirects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).parent
WWW = ROOT / "www"
LIB = ROOT / "lib"
SAMPLE = ROOT / "sample.txt"

PORT = 8090

ROOM_IDLE_TTL_SECONDS = 60 * 60  # purge rooms with no clients after 1h
ROOM_GC_INTERVAL_SECONDS = 5 * 60
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "font_size": 48,
    "scroll_speed": 50,
    "scroll_step": 100,
    "margin_left": 10,
    "margin_right": 10,
    "line_height": 1.5,
    "paragraph_spacing": 1,
    "text_align": "center",
    "font_family": "system",
    "font_contrast": 100,
}

SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "font_size": (12, 200),
    "scroll_speed": (5, 500),
    "scroll_step": (20, 500),
    "margin_left": (0, 45),
    "margin_right": (0, 45),
    "line_height": (1.0, 3.0),
    "paragraph_spacing": (0, 3),
    "font_contrast": (30, 100),
}

VALID_TEXT_ALIGNS = {"left", "center", "justify"}
VALID_FONT_FAMILIES = {
    "system", "serif", "sans-serif", "monospace",
    "Georgia", "Arial", "Verdana", "Times New Roman",
}

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "p", "br",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote",
}

log = logging.getLogger("teleprompter")


@dataclass
class Room:
    slug: str
    text: str = ""
    is_playing: bool = False
    scroll_position: float = 0
    pedal_value: int = 0
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    clients: set[asyncio.Queue] = field(default_factory=set)
    last_active: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "state",
            "text": self.text,
            "is_playing": self.is_playing,
            "scroll_position": self.scroll_position,
            "pedal_value": self.pedal_value,
            "settings": dict(self.settings),
        }

    def touch(self) -> None:
        self.last_active = time.monotonic()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def sanitize_html(raw: str) -> str:
    """Strip disallowed tags + all attributes. Whitelist approach."""
    if not raw:
        return ""

    def repl(match: re.Match) -> str:
        full = match.group(0)
        closing = match.group(1) == "/"
        tag = match.group(2).lower()
        if tag not in ALLOWED_TAGS:
            return ""
        return f"</{tag}>" if closing else f"<{tag}>"

    cleaned = re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", repl, raw)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    return cleaned


def clamp_settings(incoming: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    out = dict(current)
    for key, value in incoming.items():
        if key in SETTINGS_BOUNDS and isinstance(value, (int, float)):
            lo, hi = SETTINGS_BOUNDS[key]
            out[key] = clamp(float(value), lo, hi)
            if key in {"font_size", "scroll_speed", "scroll_step", "margin_left",
                       "margin_right", "paragraph_spacing", "font_contrast"}:
                out[key] = int(out[key])
        elif key == "text_align" and value in VALID_TEXT_ALIGNS:
            out[key] = value
        elif key == "font_family" and value in VALID_FONT_FAMILIES:
            out[key] = value
    return out


def generate_slug() -> str:
    """Run `mkname --sfw` to produce a memorable slug. Falls back to timestamp."""
    try:
        result = subprocess.run(
            ["mkname", "--sfw"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        candidate = result.stdout.strip().lower()
        candidate = re.sub(r"[^a-z0-9_-]", "_", candidate)
        if SLUG_RE.match(candidate):
            return candidate
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return f"room-{int(time.time())}"


def get_or_create_room(rooms: dict[str, Room], slug: str) -> Room:
    room = rooms.get(slug)
    if room is None:
        room = Room(slug=slug)
        rooms[slug] = room
        log.info("room created: %s", slug)
    room.touch()
    return room


async def broadcast(room: Room, message: dict[str, Any]) -> None:
    payload = json.dumps(message)
    dead: list[asyncio.Queue] = []
    for q in room.clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        room.clients.discard(q)


# ---------- handlers ----------

async def handle_root(request: web.Request) -> web.Response:
    slug = generate_slug()
    raise web.HTTPFound(f"/{slug}")


async def handle_room_index(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    get_or_create_room(rooms, slug)
    return web.FileResponse(WWW / "index.html")


async def handle_remote(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    get_or_create_room(rooms, slug)
    return web.FileResponse(WWW / "remote.html")


async def handle_status(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    room = get_or_create_room(rooms, slug)
    return web.json_response({
        "ok": True,
        "slug": slug,
        "client_count": len(room.clients),
        "is_playing": room.is_playing,
        "scroll_position": room.scroll_position,
        "server_ip": request.host.split(":")[0],
        "server_port": PORT,
    })


async def handle_sample(request: web.Request) -> web.Response:
    if not SAMPLE.exists():
        return web.Response(text="", content_type="text/plain")
    return web.FileResponse(SAMPLE)


async def handle_text(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    room = get_or_create_room(rooms, slug)

    if request.method == "DELETE":
        room.text = ""
        await broadcast(room, {"type": "text", "text": ""})
        return web.json_response({"ok": True})

    body = await request.json()
    text = sanitize_html(str(body.get("text", "")))
    room.text = text
    await broadcast(room, {"type": "text", "text": text})
    return web.json_response({"ok": True})


async def handle_control(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    room = get_or_create_room(rooms, slug)

    body = await request.json()
    action = body.get("action")

    position = body.get("position")
    if action == "play":
        room.is_playing = True
        room.pedal_value = 0
        if isinstance(position, (int, float)):
            room.scroll_position = max(0, float(position))
    elif action == "pause":
        room.is_playing = False
        room.pedal_value = 0
        if isinstance(position, (int, float)):
            room.scroll_position = max(0, float(position))
    elif action == "reset":
        room.is_playing = False
        room.scroll_position = 0
        room.pedal_value = 0
    elif action == "scroll":
        delta = body.get("delta")
        if isinstance(delta, (int, float)):
            room.scroll_position = max(0, room.scroll_position + float(delta))
    elif action == "sync":
        if isinstance(position, (int, float)):
            room.scroll_position = max(0, float(position))
    elif action == "pedal":
        value = body.get("value")
        if isinstance(value, (int, float)):
            room.pedal_value = int(clamp(int(value), -100, 100))
            room.is_playing = room.pedal_value != 0
    else:
        return web.json_response({"ok": False, "error": "unknown action"}, status=400)

    await broadcast(room, {
        "type": "control",
        "is_playing": room.is_playing,
        "scroll_position": room.scroll_position,
        "pedal_value": room.pedal_value,
    })
    return web.json_response({"ok": True})


async def handle_settings(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    room = get_or_create_room(rooms, slug)

    body = await request.json()
    incoming = body.get("settings") if isinstance(body.get("settings"), dict) else body
    if not isinstance(incoming, dict):
        return web.json_response({"ok": False, "error": "invalid body"}, status=400)

    room.settings = clamp_settings(incoming, room.settings)
    await broadcast(room, {"type": "settings", "settings": dict(room.settings)})
    return web.json_response({"ok": True, "settings": dict(room.settings)})


async def handle_events(request: web.Request) -> web.StreamResponse:
    slug = request.match_info["slug"]
    if not SLUG_RE.match(slug):
        raise web.HTTPNotFound()
    rooms: dict[str, Room] = request.app["rooms"]
    room = get_or_create_room(rooms, slug)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    room.clients.add(queue)
    log.info("sse connect: room=%s clients=%d", slug, len(room.clients))

    try:
        snapshot = json.dumps(room.snapshot())
        await response.write(f"data: {snapshot}\n\n".encode())

        last_ping = time.monotonic()
        while True:
            timeout = max(1.0, 15.0 - (time.monotonic() - last_ping))
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=timeout)
                await response.write(f"data: {payload}\n\n".encode())
                room.touch()
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
                last_ping = time.monotonic()
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    finally:
        room.clients.discard(queue)
        log.info("sse disconnect: room=%s clients=%d", slug, len(room.clients))

    return response


async def gc_loop(app: web.Application) -> None:
    rooms: dict[str, Room] = app["rooms"]
    while True:
        try:
            await asyncio.sleep(ROOM_GC_INTERVAL_SECONDS)
            now = time.monotonic()
            stale = [
                slug for slug, room in rooms.items()
                if not room.clients and (now - room.last_active) > ROOM_IDLE_TTL_SECONDS
            ]
            for slug in stale:
                rooms.pop(slug, None)
                log.info("room gc: %s", slug)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("gc error: %s", exc)


async def on_startup(app: web.Application) -> None:
    app["rooms"] = {}
    app["gc_task"] = asyncio.create_task(gc_loop(app))


async def on_cleanup(app: web.Application) -> None:
    task = app.get("gc_task")
    if task:
        task.cancel()


def make_app() -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", lambda r: web.json_response({"ok": True}))
    app.router.add_static("/lib/", path=LIB, show_index=False)

    app.router.add_get("/{slug}", handle_room_index)
    app.router.add_get("/{slug}/", handle_room_index)
    app.router.add_get("/{slug}/remote", handle_remote)
    app.router.add_get("/{slug}/events", handle_events)
    app.router.add_get("/{slug}/status", handle_status)
    app.router.add_get("/{slug}/sample", handle_sample)
    app.router.add_post("/{slug}/text", handle_text)
    app.router.add_delete("/{slug}/text", handle_text)
    app.router.add_post("/{slug}/control", handle_control)
    app.router.add_post("/{slug}/settings", handle_settings)

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
