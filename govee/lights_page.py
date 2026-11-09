#!/usr/bin/env python3
"""A phone page for the Govee lights, served by your Mac to the home network.

On the iPhone, open http://<the Mac's name>.local:8737 in Safari, then Share > Add to Home Screen.
It answers only devices on the home network. The lights already take commands from anything on the Wi-Fi,
so the page opens nothing new.
"""
from __future__ import annotations

import ipaddress
import json
import os
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import govee_lan  # noqa: E402
import govee_mcp  # noqa: E402
import govee_scenes  # noqa: E402
import spotify_follow  # noqa: E402

PORT = int(os.environ.get("LIGHTS_PAGE_PORT", "8737"))
PAGE = os.path.join(HERE, "lights_page.html")
LOCK = threading.Lock()  # one conversation with the lights at a time: they all answer on one UDP port
# One-tap scenes for the page, the menu bar app and the Shortcuts: POST /api/scene/<name>, optional ?target=.
SCENES = {
    "on": {"power": "on"},
    "off": {"power": "off"},
    "warm": {"temperature": "warm"},
    "bright": {"brightness": 100, "temperature": "neutral"},
    "dim": {"brightness": 15, "temperature": "warm"},
}
FOLLOW_FILE = os.path.join(govee_lan.DATA_DIR, "follow-spotify.json")
MANUAL = {"/api/set", "/api/govee-scene", "/api/tool/set_lights", "/api/tool/start_scene"}  # these end following


class Follower:
    """While switched on and Spotify plays, give the lights that are on the colors of the track's album art: one
    change per song, never per poll. Lights that are off stay off. Paused, stopped, ads and tracks without usable art
    leave the lights as they are."""

    def __init__(self, path: str = FOLLOW_FILE):
        self.path = path
        self.enabled = False
        self.track: dict = {}
        self.colors: list[str] = []
        self.problem = ""
        self.last_id = ""
        try:
            with open(path) as f:
                self.enabled = bool(json.load(f).get("enabled"))
        except (OSError, ValueError):
            pass

    def set(self, enabled: bool) -> dict:
        if enabled != self.enabled:
            self.enabled, self.last_id, self.problem = enabled, "", ""
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump({"enabled": enabled}, f)
        return self.status()

    def status(self) -> dict:
        return {"enabled": self.enabled, "track": self.track, "colors": self.colors, "problem": self.problem}

    def describe(self) -> str:
        if not self.enabled:
            return "Following Spotify is off."
        text = "Following Spotify."
        if self.track:
            text += f" Now: {self.track.get('name')} by {self.track.get('artist')}"
            text += f" ({', '.join(self.colors)})." if self.colors else "."
        return text + (f" Problem: {self.problem}" if self.problem else "")

    def tick(self) -> bool:
        """One poll. True if the lights were changed."""
        if not self.enabled:
            return False
        try:
            playing = spotify_follow.now_playing()
        except spotify_follow.SpotifyNotAllowed as e:
            self.problem = str(e)
            return False
        if not playing or playing["state"] != "playing" or playing["id"] == self.last_id:
            return False
        self.last_id, self.problem = playing["id"], ""
        self.track = {key: playing[key] for key in ("name", "artist", "id")}
        try:
            colors = spotify_follow.palette(spotify_follow.pixels(spotify_follow.fetch(playing["artwork"])))
        except (ValueError, OSError, subprocess.SubprocessError):
            self.colors = []  # an ad, a podcast, art that won't load: leave the lights alone
            return False
        self.colors = [spotify_follow.hex_color(c) for c in colors]
        with LOCK:
            if not self.enabled:  # switched off while the art was loading
                return False
            lights = govee_lan.known_lights()
            states = govee_lan.status(lights)
            lit = [light for light in lights if states.get(light.device, {}).get("onOff") == 1]  # off stays off
            if not lit:
                return False
            plan = spotify_follow.assign(lit, colors)
            groups: dict[tuple, list] = {}
            for device, color in plan.items():
                groups.setdefault(color, []).append(device)
            missed = []
            for color, devices in groups.items():
                result = govee_lan.set_lights_result(",".join(devices), color=spotify_follow.hex_color(color))
                missed += result["failed"]
        print(f"Spotify: {self.track['name']} by {self.track['artist']} -> {', '.join(self.colors)}"
              + (f"; missed {', '.join(missed)}" if missed else ""), file=sys.stderr, flush=True)
        return True

    def run(self, every: float = 3.0) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # keep following through a bad moment
                traceback.print_exc(file=sys.stderr)
            time.sleep(every)


FOLLOWER = Follower()
govee_mcp.FOLLOWER = FOLLOWER  # so the chat tools follow_spotify and spotify_status reach it
MANIFEST = {
    "name": "Lights", "short_name": "Lights", "start_url": "/", "display": "standalone",
    "background_color": "#1c1a21", "theme_color": "#1c1a21",
    "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}],
}


def icon_png(size: int = 180) -> bytes:
    """A glowing bulb on a dark square, drawn here so nothing needs installing."""
    bg, glow, base = (28, 26, 33), (255, 196, 110), (150, 146, 156)
    cx, cy, r = size / 2, size * 0.44, size * 0.25
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter type 0 for this row
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d <= r:
                pixel = glow
            elif abs(x - cx) <= r * 0.42 and cy + r * 0.85 <= y <= cy + r * 1.35:
                pixel = base
            elif d <= r * 1.7:
                a = ((r * 1.7 - d) / (r * 0.7)) ** 2 * 0.4  # the halo fades out
                pixel = tuple(int(b + (g - b) * a) for b, g in zip(bg, glow))
            else:
                pixel = bg
            row += bytes(pixel)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


ICON = icon_png()


def is_home(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def lights_data(rescan: bool = False) -> dict:
    lights = govee_lan.known_lights(rescan=rescan)
    states = govee_lan.status(lights) if lights else {}
    kinds = sorted({light.kind + "s" for light in lights})
    rooms = sorted({light.room for light in lights if light.room})
    return {
        "groups": ["all"] + kinds + rooms,
        "lights": [{"device": light.device, "label": light.label, "room": light.room, "kind": light.kind,
                    "sku": light.sku, "state": states.get(light.device)} for light in lights],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeLights/0.1"

    def log_request(self, code="-", size="-"):
        if self.command != "GET" or (isinstance(code, int) and code >= 400):  # changes and errors only
            super().log_request(code, size)

    def _send(self, status: int, body: bytes, kind: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict, kind: str = "application/json") -> None:
        self._send(status, json.dumps(data).encode(), kind)

    def _home(self) -> bool:
        if is_home(self.client_address[0]):
            return True
        self._send(403, b"home network only", "text/plain")
        return False

    def do_GET(self):
        if not self._home():
            return
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/lights":
            try:
                with LOCK:
                    self._json(200, lights_data(rescan="rescan=1" in query))
            except govee_lan.GoveeError as e:
                self._json(503, {"error": str(e)})
        elif path == "/api/scenes":
            try:
                with LOCK:
                    self._json(200, govee_scenes.scene_lists(parse_qs(query).get("target", ["all"])[0]))
            except govee_lan.GoveeError as e:
                self._json(503, {"error": str(e)})
        elif path == "/api/govee-key":
            self._json(200, {"saved": bool(govee_scenes.cloud_key())})  # never the key itself
        elif path == "/api/spotify":
            self._json(200, FOLLOWER.status())
        elif path == "/icon.png":
            self._send(200, ICON, "image/png", cache=True)
        elif path == "/manifest.webmanifest":
            self._json(200, MANIFEST, "application/manifest+json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._home():
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._json(400, {"ok": False, "text": "That request didn't make sense."})
        path, _, query = self.path.partition("?")
        target = body.get("target") or parse_qs(query).get("target", ["all"])[0]
        if path == "/api/spotify":
            return self._json(200, {"ok": True, **FOLLOWER.set(bool(body.get("enabled")))})
        if path in MANUAL or path.startswith("/api/scene/"):
            FOLLOWER.set(False)  # a look picked by hand wins: stop following so they don't fight
        if path == "/api/set":
            def action():
                return govee_lan.set_lights_result(target, body.get("power"), body.get("brightness"),
                                                   body.get("color"), body.get("temperature"))
        elif path.startswith("/api/scene/"):
            scene = SCENES.get(path[len("/api/scene/"):])
            if scene is None:
                return self._json(404, {"ok": False, "text": "No such scene. Try: " + ", ".join(SCENES)})

            def action():
                return govee_lan.set_lights_result(target, **scene)
        elif path == "/api/blink":
            def action():
                return {"text": govee_lan.identify_light(str(body.get("target") or "")), "failed": []}
        elif path == "/api/govee-scene":
            def action():
                return govee_scenes.start_scene(target, body.get("name") or "")
        elif path == "/api/govee-key":
            def action():  # the body holds the key: it is saved, never logged or sent back
                return {"text": govee_scenes.save_key(body.get("key") or ""), "failed": []}
        elif path.startswith("/api/tool/"):  # the chat and voice tools (govee_mcp.py) run here
            name = path[len("/api/tool/"):]
            if name not in govee_mcp.HANDLERS:
                return self._json(404, {"ok": False, "text": f"Unknown tool: {name}"})

            def action():
                return {"text": govee_mcp.run_local(name, body), "failed": []}
        else:
            return self._send(404, b"not found", "text/plain")
        try:
            with LOCK:
                result = action()
        except govee_lan.GoveeError as e:
            return self._json(200, {"ok": False, "text": str(e)})
        except Exception as e:  # say so on the page rather than drop the connection
            traceback.print_exc(file=sys.stderr)
            return self._json(500, {"ok": False, "text": f"Unexpected error: {e}"})
        # So a light that misses a change shows in the log. One line, trimmed: scene lists are long.
        self.log_message("%s -> %s", path, " ".join(result["text"].split())[:240])
        self._json(200, {"ok": not result["failed"], **result})


def main() -> None:
    server = ThreadingHTTPServer(("", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=FOLLOWER.run, daemon=True).start()
    print(f"Lights page on port {PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
