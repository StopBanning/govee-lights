"""Tests for spotify_follow: never runs real osascript or sips, never downloads."""
import struct
import subprocess
import unittest
from dataclasses import dataclass

import spotify_follow as sf


def bmp(pixels_rows, bits=24, top_down=False):
    """A tiny uncompressed BMP. pixels_rows: rows top to bottom of (r, g, b)."""
    height, width = len(pixels_rows), len(pixels_rows[0])
    step = bits // 8
    row_size = (width * step + 3) & ~3
    rows = pixels_rows if top_down else list(reversed(pixels_rows))
    body = b""
    for row in rows:
        line = b"".join(bytes([b, g, r]) + (b"\xff" if bits == 32 else b"") for r, g, b in row)
        body += line + b"\0" * (row_size - len(line))
    header = struct.pack("<iiHHIIiiII", width, -height if top_down else height, 1, bits, 0, len(body), 0, 0, 0, 0)
    info = struct.pack("<I", 40) + header
    return b"BM" + struct.pack("<IHHI", 14 + len(info) + len(body), 0, 0, 14 + len(info)) + info + body


def done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@dataclass
class L:
    device: str
    label: str
    kind: str


class NowPlaying(unittest.TestCase):
    def test_fields(self):
        sep = sf.SEP
        out = f"playing{sep}spotify:track:9{sep}https://i.scdn.co/image/x{sep}Hey, \"You\"{sep}A, B & C\n"
        self.assertEqual(sf.now_playing(lambda *a, **k: done(out)),
                         {"state": "playing", "id": "spotify:track:9", "artwork": "https://i.scdn.co/image/x",
                          "name": 'Hey, "You"', "artist": "A, B & C"})

    def test_not_running_paused_and_failures(self):
        self.assertIsNone(sf.now_playing(lambda *a, **k: done("")))
        out = sf.SEP.join(["paused", "id", "url", "n", "a"])
        self.assertEqual(sf.now_playing(lambda *a, **k: done(out))["state"], "paused")
        self.assertIsNone(sf.now_playing(lambda *a, **k: done(code=1, stderr="syntax error")))

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("osascript", 5)
        self.assertIsNone(sf.now_playing(boom))

    def test_not_allowed(self):
        with self.assertRaises(sf.SpotifyNotAllowed):
            sf.now_playing(lambda *a, **k: done(code=1, stderr="execution error: Not authorized to send Apple events "
                                                               "to Spotify. (-1743)"))

    def test_never_launches_spotify(self):
        self.assertTrue(sf.SCRIPT.startswith('if application "Spotify" is running'))


class Fetch(unittest.TestCase):
    class Reply:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.data[:n]

    def test_only_spotify_art_over_https(self):
        ok = lambda url, timeout: self.Reply(b"jpg")  # noqa: E731
        self.assertEqual(sf.fetch("https://i.scdn.co/image/ab67", ok), b"jpg")
        self.assertEqual(sf.fetch("https://mosaic.scdn.co/640/x", ok), b"jpg")
        for bad in ("http://i.scdn.co/image/x", "https://evil.example/x.jpg", "https://scdn.co.evil.com/x", "", None):
            with self.assertRaises(ValueError, msg=bad):
                sf.fetch(bad, ok)

    def test_too_big(self):
        with self.assertRaises(ValueError):
            sf.fetch("https://i.scdn.co/image/x", lambda url, timeout: self.Reply(b"x" * (sf.MAX_ART + 5)))


class Pixels(unittest.TestCase):
    ROWS = [[(255, 0, 0), (0, 255, 0), (0, 0, 255)], [(10, 20, 30), (40, 50, 60), (70, 80, 90)]]

    def fake_sips(self, image):
        def run(command, **kwargs):
            with open(command[command.index("--out") + 1], "wb") as f:
                f.write(image)
            return done()
        return run

    def test_24_bit_bottom_up_with_padding(self):
        got = sf.pixels(b"jpeg", self.fake_sips(bmp(self.ROWS)))
        self.assertEqual(sorted(got), sorted(p for row in self.ROWS for p in row))

    def test_32_bit_top_down(self):
        got = sf.pixels(b"jpeg", self.fake_sips(bmp(self.ROWS, bits=32, top_down=True)))
        self.assertEqual(got, [p for row in self.ROWS for p in row])

    def test_not_a_bmp(self):
        with self.assertRaises(ValueError):
            sf.read_bmp(b"GIF89a" + b"\0" * 60)


class Palette(unittest.TestCase):
    def test_drops_black_white_grey_and_ranks(self):
        px = [(0, 0, 0)] * 50 + [(255, 255, 255)] * 50 + [(128, 128, 128)] * 50 + \
             [(200, 30, 30)] * 20 + [(30, 30, 200)] * 10 + [(30, 200, 30)] * 5
        self.assertEqual(sf.palette(px), [(255, 38, 38), (38, 38, 255), (38, 255, 38)])

    def test_near_hues_count_once(self):
        px = [(255, 0, 0)] * 20 + [(255, 40, 0)] * 15 + [(0, 0, 255)] * 5
        got = sf.palette(px)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1], (0, 0, 255))

    def test_count_and_fallback(self):
        rainbow = [(255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255), (0, 0, 255), (255, 0, 255)] * 3
        self.assertEqual(len(sf.palette(rainbow, count=4)), 4)
        self.assertEqual(sf.palette([(0, 0, 0), (250, 250, 250), (90, 90, 95)]), [sf.WARM_WHITE])
        self.assertEqual(sf.palette([]), [sf.WARM_WHITE])


class Assign(unittest.TestCase):
    def test_spread(self):
        lights = [L("b10", "bulb 10", "bulb"), L("f1", "floor lamp 1", "floor lamp"), L("b2", "bulb 2", "bulb"),
                  L("b1", "bulb 1", "bulb"), L("s1", "strip 1", "strip")]
        a, b, c = (1, 0, 0), (0, 1, 0), (0, 0, 1)
        self.assertEqual(sf.assign(lights, [a, b, c]), {"f1": a, "b1": b, "b2": c, "b10": b, "s1": c})
        self.assertEqual(set(sf.assign(lights, [a]).values()), {a})

    def test_hex(self):
        self.assertEqual(sf.hex_color((255, 128, 0)), "#ff8000")


if __name__ == "__main__":
    unittest.main()
