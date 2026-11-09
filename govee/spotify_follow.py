"""Colors from the album art of whatever Spotify is playing on this Mac. Reads only: nothing here changes a light.

Spotify is asked over AppleScript (no Spotify developer key), the artwork comes from Spotify's image CDN, and macOS's
own `sips` shrinks it to a 24x24 BMP that Python reads. The hub (lights_page.py) runs the loop and sends the colors.
"""
from __future__ import annotations

import colorsys
import os
import re
import struct
import subprocess
import tempfile
import urllib.parse
import urllib.request

SEP = "\x1f"  # between fields: can't appear in a track or artist name
SCRIPT = f'''if application "Spotify" is running then
  tell application "Spotify"
    set t to current track
    return (player state as text) & "{SEP}" & (id of t) & "{SEP}" & (artwork url of t) & "{SEP}" & (name of t) & "{SEP}" & (artist of t)
  end tell
end if
return ""'''
ART_HOSTS = (".scdn.co", ".spotifycdn.com")
MAX_ART = 2_000_000
WARM_WHITE = (255, 180, 107)


class SpotifyNotAllowed(Exception):
    """macOS hasn't let this app control Spotify."""


last_problem = ""  # why the last now_playing() got nothing, for the status line; "" when it worked


def now_playing(run=subprocess.run) -> dict | None:
    """{"state", "id", "artwork", "name", "artist"} for Spotify's current track, or None when it isn't running.
    Never launches Spotify."""
    global last_problem
    try:
        done = run(["osascript", "-e", SCRIPT], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        # Most often macOS's "wants to control Spotify" question, waiting on the screen.
        last_problem = ("Spotify didn't answer. If macOS is asking whether Lights Hub may control Spotify, "
                        "click OK.")
        return None
    except (OSError, subprocess.SubprocessError) as e:
        last_problem = f"couldn't ask Spotify ({e})"
        return None
    if done.returncode != 0:
        if "-1743" in done.stderr or "not authorized" in done.stderr.lower():
            raise SpotifyNotAllowed("macOS hasn't let Lights Hub control Spotify. Allow it in System Settings > "
                                    "Privacy & Security > Automation.")
        last_problem = "Spotify answered with an error: " + " ".join(done.stderr.split())[:160]
        return None
    last_problem = ""
    fields = done.stdout.rstrip("\n").split(SEP)
    if len(fields) != 5:
        return None
    state, track, artwork, name, artist = fields
    return {"state": state.strip().lower(), "id": track, "artwork": artwork, "name": name, "artist": artist}


def fetch(url: str, opener=urllib.request.urlopen) -> bytes:
    """The artwork's bytes. Only Spotify's image hosts over https; anything else raises ValueError."""
    parts = urllib.parse.urlparse(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(ART_HOSTS):
        raise ValueError(f"not Spotify artwork: {url!r}")
    with opener(url, timeout=10) as reply:
        data = reply.read(MAX_ART + 1)
    if len(data) > MAX_ART:
        raise ValueError("the artwork is too big")
    return data


def read_bmp(data: bytes) -> list[tuple[int, int, int]]:
    """RGB pixels from an uncompressed 24- or 32-bit BMP, either way up."""
    if data[:2] != b"BM" or len(data) < 54:
        raise ValueError("not a BMP")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height, _planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        raise ValueError(f"unsupported BMP ({bits}-bit, compression {compression})")
    step = bits // 8
    row_size = (width * step + 3) & ~3
    rows = range(abs(height))
    pixels = []
    for row in rows:
        start = offset + row * row_size
        for x in range(width):
            b, g, r = data[start + x * step:start + x * step + 3]
            pixels.append((r, g, b))
    return pixels  # row order doesn't matter for picking colors


def pixels(image: bytes, run=subprocess.run) -> list[tuple[int, int, int]]:
    """The artwork shrunk to 24x24 by macOS's sips, as RGB pixels."""
    with tempfile.TemporaryDirectory() as folder:
        source, out = os.path.join(folder, "art.jpg"), os.path.join(folder, "art.bmp")
        with open(source, "wb") as f:
            f.write(image)
        run(["sips", "-z", "24", "24", "-s", "format", "bmp", source, "--out", out],
            capture_output=True, timeout=10, check=True)
        with open(out, "rb") as f:
            return read_bmp(f.read())


def palette(pixels: list[tuple[int, int, int]], count: int = 4) -> list[tuple[int, int, int]]:
    """Up to `count` vivid, distinct colors, the most present first, at full brightness for the lights."""
    buckets: dict[int, list] = {}
    for r, g, b in pixels:
        if max(r, g, b) < 40 or min(r, g, b) > 215:
            continue  # near-black, near-white
        h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25:
            continue  # grey
        buckets.setdefault(int(h * 12) % 12, []).append((r, g, b, s, h))
    ranked = sorted(buckets.values(), key=lambda px: len(px) * sum(p[3] for p in px) / len(px), reverse=True)
    chosen: list[tuple] = []
    hues: list[float] = []
    for px in ranked:
        mean = [sum(p[i] for p in px) / len(px) for i in range(3)]
        hue = colorsys.rgb_to_hsv(*(c / 255 for c in mean))[0] * 360
        if any(min(abs(hue - other), 360 - abs(hue - other)) < 30 for other in hues):
            continue
        top = max(mean) or 1
        chosen.append(tuple(min(255, round(c * 255 / top)) for c in mean))
        hues.append(hue)
        if len(chosen) == count:
            break
    return chosen or [WARM_WHITE]


def _natural(label: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", label.lower())]


def assign(lights, colors: list[tuple]) -> dict[str, tuple]:
    """{device: color}. Floor lamps take the main color; the rest, in label order, take turns with the others."""
    rest = colors[1:] or colors[:1]
    plan: dict[str, tuple] = {}
    others = sorted((light for light in lights if light.kind != "floor lamp"), key=lambda light: _natural(light.label))
    for light in lights:
        if light.kind == "floor lamp":
            plan[light.device] = colors[0]
    for index, light in enumerate(others):
        plan[light.device] = rest[index % len(rest)]
    return plan


def hex_color(rgb: tuple) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)
