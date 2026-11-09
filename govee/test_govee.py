"""Tests for the Govee tool. A fake light on 127.0.0.1 stands in for the real ones, on other ports.

Run: python3 -m unittest discover -s govee -v
"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import govee_ble
import govee_lan
import govee_mcp
import govee_scenes
import lights_page
import spotify_follow

HERE = os.path.dirname(os.path.abspath(__file__))
_ORIGINAL_RUN = govee_ble._run
# Govee's real Rainbow settings for the H6008, and a made-up block for the H60B2's Aurora.
RAINBOW_BULB = bytes.fromhex("8301ffebff0f0007ff0000ff7f00ffff0000ff0000ffff0000ff8b00ff")
TEST_LIBRARY = {
    "H6008": {"Rainbow": {"code": 1175, "param": base64.b64encode(RAINBOW_BULB).decode(), "category": "Natural"},
              "Fire": {"code": 1176, "param": "", "category": "Natural"}},
    "H617A": {"Aurora": {"code": 13898, "param": base64.b64encode(bytes(range(30))).decode(), "category": "Natural"}},
    "H60B2": {"Rainbow": {"code": 22, "param": "", "category": "Natural"},
              "Aurora": {"code": 13898, "param": base64.b64encode(bytes(range(30))).decode(), "category": "Natural"}},
}


class FakeLight(threading.Thread):
    """Acts like a Govee light: applies turn, brightness and colorwc, and answers devStatus."""

    def __init__(self, mute=False, ignore=()):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", govee_lan.CONTROL_PORT))
        self.sock.settimeout(0.1)
        self.mute = mute
        self.ignore = ignore  # commands it pretends to miss
        self.state = {"onOff": 0, "brightness": 50, "color": {"r": 255, "g": 255, "b": 255}, "colorTemInKelvin": 4000}
        self.commands = []
        self.real = []  # ptReal packet lists, as sent
        self.running = True

    def run(self):
        while self.running:
            try:
                data, (ip, _port) = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if self.mute:
                continue
            msg = json.loads(data)["msg"]
            cmd, info = msg["cmd"], msg["data"]
            self.commands.append(cmd)
            if cmd in self.ignore:
                continue
            if cmd == "turn":
                self.state["onOff"] = info["value"]
            elif cmd == "brightness":
                self.state["brightness"] = info["value"]
            elif cmd == "colorwc":
                self.state["color"], self.state["colorTemInKelvin"] = info["color"], info["colorTemInKelvin"]
            elif cmd == "ptReal":
                self.real.append(info["command"])
            elif cmd == "devStatus":
                reply = json.dumps({"msg": {"cmd": "devStatus", "data": self.state}}).encode()
                self.sock.sendto(reply, (ip, govee_lan.LISTEN_PORT))

    def stop(self):
        self.running = False
        self.join()
        self.sock.close()


def patch(test, target, name, value):
    patcher = mock.patch.object(target, name, value)
    test.addCleanup(patcher.stop)
    return patcher.start()


class Base(unittest.TestCase):
    """Points the modules at test ports and throwaway files. Never scans the real network, never reaches Govee,
    and never goes through the real lights hub."""

    registry = {}

    def setUp(self):
        folder = tempfile.mkdtemp()
        lights_file = os.path.join(folder, "lights.json")
        with open(lights_file, "w") as f:
            json.dump(self.registry, f)
        for name, value in (("LISTEN_PORT", 14002), ("CONTROL_PORT", 14003), ("LIGHTS_FILE", lights_file)):
            patch(self, govee_lan, name, value)
        self.scan = patch(self, govee_lan, "scan", mock.Mock(return_value=[]))
        self.key_file = os.path.join(folder, "support", "govee-api-key")
        patch(self, govee_scenes, "KEY_FILE", self.key_file)
        patch(self, govee_scenes, "CACHE_DIR", os.path.join(folder, "scene-cache"))
        patch(self, govee_scenes, "library", lambda sku: TEST_LIBRARY[sku])
        patch(self, govee_scenes, "_http_json", mock.Mock(side_effect=AssertionError("tried to reach Govee")))
        patch(self, govee_mcp, "HUB", "")
        patch(self, govee_ble, "available", lambda: False)  # never the real Bluetooth app or lights
        patch(self, govee_ble, "_run", mock.Mock(side_effect=AssertionError("tried to use real Bluetooth")))
        patch(self, govee_ble, "_states", {})
        self.follower = lights_page.Follower(os.path.join(folder, "follow-spotify.json"))  # never the real switch
        patch(self, lights_page, "FOLLOWER", self.follower)
        patch(self, govee_mcp, "FOLLOWER", self.follower)
        for name in ("now_playing", "fetch", "pixels"):  # never real Spotify, downloads or sips
            patch(self, spotify_follow, name, mock.Mock(side_effect=AssertionError(f"tried real {name}")))
        env = mock.patch.dict(os.environ, {"GOVEE_API_KEY": ""})  # a key in the shell must not leak in
        env.start()
        self.addCleanup(env.stop)

    def start_light(self, mute=False, ignore=()):
        light = FakeLight(mute, ignore)
        light.start()
        self.addCleanup(light.stop)
        return light


class Parsing(unittest.TestCase):
    def test_temperatures(self):
        self.assertEqual(govee_lan.parse_change(temperature="warm"), {"kelvin": 2700})
        self.assertEqual(govee_lan.parse_temperature("3000K"), 3000)
        self.assertEqual(govee_lan.parse_temperature("9500"), 9000)

    def test_colors(self):
        self.assertEqual(govee_lan.parse_change(color="#FF8800"), {"rgb": (255, 136, 0)})
        self.assertEqual(govee_lan.parse_change(color="0, 0, 255"), {"rgb": (0, 0, 255)})
        self.assertEqual(govee_lan.parse_change(color="Warm  White"), {"kelvin": 2700})
        self.assertEqual(govee_lan.parse_change(color="warm"), {"kelvin": 2700})
        self.assertEqual(govee_lan.parse_change(color="blue", brightness="40%"), {"rgb": (0, 0, 255), "brightness": 40})

    def test_off_wins(self):
        self.assertEqual(govee_lan.parse_change(power="off", brightness=50), {"power": False})

    def test_bad_input(self):
        for kwargs in ({}, {"brightness": 0}, {"brightness": "loud"}, {"power": "maybe"},
                       {"color": "chartreuse"}, {"color": "blue", "temperature": "warm"}, {"temperature": "hot"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(govee_lan.GoveeError):
                govee_lan.parse_change(**kwargs)

    def test_message_order(self):
        cmds = [json.loads(p)["msg"]["cmd"] for p in govee_lan._messages({"brightness": 40, "kelvin": 2700})]
        self.assertEqual(cmds, ["turn", "colorwc", "brightness"])

    def test_matches(self):
        on_warm = {"onOff": 1, "brightness": 40, "colorTemInKelvin": 2700, "color": {"r": 0, "g": 0, "b": 0}}
        self.assertTrue(govee_lan.matches(on_warm, {"kelvin": 2700, "brightness": 40}))
        self.assertFalse(govee_lan.matches(on_warm, {"kelvin": 6500}))
        self.assertFalse(govee_lan.matches(on_warm, {"rgb": (0, 0, 0)}))  # white mode isn't a color
        self.assertFalse(govee_lan.matches({}, {"power": True}))
        self.assertTrue(govee_lan.matches({"onOff": 0}, {"power": False}))


class Targets(Base):
    registry = {
        "BB": {"sku": "H6008", "ip": "10.0.0.2"},
        "AA": {"sku": "H6008", "ip": "10.0.0.1"},
        "CC": {"sku": "H60B2", "ip": "10.0.0.3"},
    }

    def labels(self, lights):
        return [light.label for light in lights]

    def test_default_labels_follow_device_id(self):
        self.assertEqual(self.labels(govee_lan.known_lights()), ["bulb 1", "bulb 2", "floor lamp 1"])
        self.scan.assert_not_called()

    def test_resolve(self):
        lights = govee_lan.known_lights()
        self.assertEqual(len(govee_lan.resolve("All", lights)), 3)
        self.assertEqual(len(govee_lan.resolve(None, lights)), 3)
        self.assertEqual(self.labels(govee_lan.resolve("the bulbs", lights)), ["bulb 1", "bulb 2"])
        self.assertEqual(self.labels(govee_lan.resolve("floor lamps, bulb 2", lights)), ["floor lamp 1", "bulb 2"])
        self.assertEqual(self.labels(govee_lan.resolve("10.0.0.3", lights)), ["floor lamp 1"])
        with self.assertRaises(govee_lan.GoveeError):
            govee_lan.resolve("garage", lights)

    def test_names_and_rooms(self):
        govee_lan.name_light("bulb 1", "desk lamp", "office")
        self.assertEqual(self.labels(govee_lan.known_lights()), ["desk lamp", "bulb 2", "floor lamp 1"])
        lights = govee_lan.known_lights()
        self.assertEqual(self.labels(govee_lan.resolve("office lights", lights)), ["desk lamp"])
        with self.assertRaises(govee_lan.GoveeError):
            govee_lan.name_light("bulb 2", "Desk Lamp")
        with self.assertRaises(govee_lan.GoveeError):
            govee_lan.name_light("bulbs", "both")


ONE_FAKE_LIGHT = {"FAKE": {"sku": "H6008", "ip": "127.0.0.1"}}


class AgainstFakeLight(Base):
    registry = ONE_FAKE_LIGHT

    def test_set_and_confirm(self):
        light = self.start_light()
        self.assertEqual(govee_lan.set_lights("bulbs", temperature="warm", brightness=40),
                         "On, white 2700K, 40% for 1 light: 1 confirmed.")
        self.assertEqual((light.state["onOff"], light.state["colorTemInKelvin"], light.state["brightness"]),
                         (1, 2700, 40))
        self.assertIn("1 confirmed", govee_lan.set_lights(power="off"))
        self.assertEqual(light.state["onOff"], 0)

    def test_silent_light_is_reported(self):
        self.start_light(mute=True)
        self.assertEqual(govee_lan.set_lights(power="on"),
                         "On for 1 light: 0 confirmed. Not confirmed: bulb 1 didn't answer (switched off at the wall?).")
        self.scan.assert_called_once()  # it looked for a new address before giving up

    def test_stuck_light_says_what_it_reports(self):
        light = self.start_light(ignore=("colorwc",))
        self.assertEqual(govee_lan.set_lights(temperature="warm"), "On, white 2700K for 1 light: 0 confirmed. "
                         "Not confirmed: bulb 1 reports on, 50%, white 4000K.")
        self.assertEqual(light.commands.count("colorwc"), 3)  # sent once, then resent twice
        self.scan.assert_not_called()  # it answered, so its address is fine

    def test_identify_leaves_light_as_it_was(self):
        light = self.start_light()
        self.assertIn("left it off", govee_lan.identify_light("bulb 1"))
        self.assertEqual(light.commands.count("turn"), 4)
        self.assertEqual(light.state["onOff"], 0)

    def test_mcp_call(self):
        self.start_light()
        reply = govee_mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                  "params": {"name": "set_lights", "arguments": {"color": "blue"}}})
        self.assertEqual(reply["id"], 7)
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("1 confirmed", reply["result"]["content"][0]["text"])


class Page(Base):
    registry = ONE_FAKE_LIGHT

    def setUp(self):
        super().setUp()
        self.light = self.start_light()
        server = lights_page.ThreadingHTTPServer(("127.0.0.1", 0), lights_page.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers["Content-Type"], r.read()

    def post(self, path, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_page_and_icon(self):
        status, kind, body = self.get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<title>Lights</title>", body)
        self.assertTrue(self.get("/icon.png")[2].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(json.loads(self.get("/manifest.webmanifest")[2])["display"], "standalone")

    def test_lights_and_set(self):
        data = json.loads(self.get("/api/lights")[2])
        self.assertEqual(data["groups"], ["all", "bulbs"])
        self.assertEqual(data["lights"][0]["state"]["onOff"], 0)
        self.assertEqual(self.post("/api/set", {"target": "bulbs", "temperature": "warm"}),
                         (200, {"ok": True, "text": "On, white 2700K for 1 light: 1 confirmed.", "total": 1,
                                "failed": []}))
        self.assertEqual(json.loads(self.get("/api/lights")[2])["lights"][0]["state"]["colorTemInKelvin"], 2700)

    def test_scenes(self):
        light = self.light
        status, reply = self.post("/api/scene/dim?target=bulbs", b"")
        self.assertEqual((status, reply["text"]), (200, "On, white 2700K, 15% for 1 light: 1 confirmed."))
        self.assertEqual((light.state["brightness"], light.state["colorTemInKelvin"]), (15, 2700))
        self.assertTrue(self.post("/api/scene/off", {"target": "bulb 1"})[1]["ok"])
        self.assertEqual(light.state["onOff"], 0)
        self.assertEqual(self.post("/api/scene/disco", b"")[0], 404)

    def test_any_color_value(self):
        # The color pickers send a hex code.
        self.assertEqual(self.post("/api/set", {"target": "all", "color": "#3366ff"})[1]["text"],
                         "On, color #3366ff for 1 light: 1 confirmed.")
        self.assertEqual(self.light.state["color"], {"r": 51, "g": 102, "b": 255})

    def test_temperature_slider_value(self):
        # The page and menu bar sliders send kelvin as a number.
        self.assertEqual(self.post("/api/set", {"target": "all", "temperature": 3500})[1]["text"],
                         "On, white 3500K for 1 light: 1 confirmed.")
        self.assertEqual(self.light.state["colorTemInKelvin"], 3500)

    def test_bad_requests(self):
        self.assertEqual(self.post("/api/set", b"not json")[0], 400)
        status, reply = self.post("/api/set", {"color": "chartreuse"})
        self.assertEqual((status, reply["ok"]), (200, False))
        self.assertIn("unknown color", reply["text"])

    def test_home_network_only(self):
        for address, home in (("192.168.1.20", True), ("10.1.2.3", True), ("127.0.0.1", True),
                              ("8.8.8.8", False), ("junk", False)):
            self.assertEqual(lights_page.is_home(address), home, address)

    def test_scene_lists(self):
        self.assertEqual(json.loads(self.get("/api/scenes")[2]),
                         {"common": ["Fire", "Rainbow"], "kinds": {"bulbs": ["Fire", "Rainbow"]},
                          "categories": {"bulbs": [{"name": "Natural", "scenes": ["Rainbow", "Fire"]}]},
                          "need_key": ["bulbs"], "key_saved": False})

    def test_key_is_saved_but_never_sent_back(self):
        govee_scenes._http_json.side_effect = None
        govee_scenes._http_json.return_value = {"code": 200, "data": [{"device": "a"}, {"device": "b"}]}
        status, reply = self.post("/api/govee-key", {"key": "secret-key-123"})
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertNotIn("secret-key-123", json.dumps(reply))
        self.assertEqual(json.loads(self.get("/api/govee-key")[2]), {"saved": True})
        self.assertEqual(oct(os.stat(self.key_file).st_mode & 0o777), "0o600")

    def test_tools_run_here(self):
        self.assertEqual(self.post("/api/tool/set_lights", {"temperature": "warm"}),
                         (200, {"ok": True, "text": "On, white 2700K for 1 light: 1 confirmed.", "failed": []}))
        self.assertEqual(self.post("/api/tool/fly", {})[0], 404)

    def test_chat_tools_go_through_the_hub(self):
        patch(self, govee_mcp, "HUB", self.base)
        with mock.patch.object(lights_page.Handler, "do_POST", autospec=True,
                               side_effect=lights_page.Handler.do_POST) as hub_post:
            reply = govee_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "set_lights", "arguments": {"color": "blue"}}})
        hub_post.assert_called_once()
        self.assertEqual(reply["result"]["content"][0]["text"], "On, color #0000ff for 1 light: 1 confirmed.")
        self.assertEqual(self.light.state["color"], {"r": 0, "g": 0, "b": 255})

    def test_chat_tools_work_without_the_hub(self):
        patch(self, govee_mcp, "HUB", "http://127.0.0.1:9")  # nothing listens there
        reply = govee_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "set_lights", "arguments": {"power": "on"}}})
        self.assertEqual(reply["result"]["content"][0]["text"], "On for 1 light: 1 confirmed.")


class Scenes(Base):
    registry = {"LAMP": {"sku": "H60B2", "ip": "127.0.0.1"}}

    def wait_for(self, check):
        for _ in range(50):
            if check():
                return
            time.sleep(0.02)

    def test_packets(self):
        # The tree lamps took this exact Rainbow packet on 2026-11-01.
        self.assertEqual([p.hex() for p in govee_scenes.lan_packets(22, "")],
                         ["3305041600000000000000000000000000000024"])
        # Settings go first, in a3 rows: 14 bytes in the first, 17 after, the last row marked ff.
        param = base64.b64encode(RAINBOW_BULB).decode()
        self.assertEqual([p.hex(" ") for p in govee_scenes.lan_packets(1175, param)], [
            "a3 00 01 02 02 83 01 ff eb ff 0f 00 07 ff 00 00 ff 7f 00 bc",
            "a3 ff ff ff 00 00 ff 00 00 ff ff 00 00 ff 8b 00 ff 00 00 28",
            "33 05 04 97 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 a1"])
        short = govee_scenes.lan_packets(5, base64.b64encode(b"\x01\x02\x03").decode())
        self.assertEqual(short[0][:8].hex(" "), "a3 ff 01 01 02 01 02 03")

    def test_lamp_scene_goes_over_the_network(self):
        light = self.start_light()
        result = govee_scenes.start_scene("all", "aurora")
        self.assertEqual(result, {"text": "Aurora started on 1 of 1 lights.", "failed": []})
        self.wait_for(lambda: len(light.real) == 2)
        self.assertEqual(light.commands[0], "turn")
        expected = [base64.b64encode(p).decode() for p in govee_scenes.lan_packets(13898, TEST_LIBRARY["H60B2"]["Aurora"]["param"])]
        self.assertEqual(light.real, [expected, expected])  # sent twice, since scenes can't be confirmed

    def test_unknown_scene(self):
        with self.assertRaises(govee_lan.GoveeError):
            govee_scenes.start_scene("all", "disco inferno")

    def test_lists_for_lamps_alone(self):
        self.assertIn("Scenes all of them have (2): Aurora, Rainbow", govee_scenes.list_scenes())


class MixedScenes(Base):
    registry = {"LAMP": {"sku": "H60B2", "ip": "127.0.0.1"}, "BULB": {"sku": "H6008", "ip": "127.0.0.3"}}

    def test_lights_without_the_scene_are_left_alone(self):
        lamp = self.start_light()
        # Aurora is a tree lamp scene only: the lamp gets it, the bulb gets nothing and isn't a failure.
        self.assertEqual(govee_scenes.start_scene("all", "Aurora"),
                         {"text": "Aurora started on 1 of 2 lights. The bulbs don't have it.", "failed": []})
        for _ in range(50):
            if len(lamp.real) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(len(lamp.real), 2)

    def test_categories_keep_govee_order(self):
        lists = govee_scenes.scene_lists()
        self.assertEqual(lists["common"], ["Rainbow"])
        self.assertEqual(lists["categories"]["floor lamps"], [{"name": "Natural", "scenes": ["Rainbow", "Aurora"]}])


class BulbScenes(Base):
    registry = {"BULB": {"sku": "H6008", "ip": "127.0.0.1"}}

    def test_bulbs_need_the_key(self):
        self.start_light()
        result = govee_scenes.start_scene("all", "Rainbow")
        self.assertEqual(result["failed"], ["bulb 1"])
        self.assertIn(govee_scenes.NEED_KEY, result["text"])

    def test_bulbs_use_the_cloud_with_a_key(self):
        self.start_light()
        os.makedirs(os.path.dirname(self.key_file))
        with open(self.key_file, "w") as f:
            f.write("test-key\n")
        sent = []

        def govee(request):
            sent.append(request)
            if request.full_url.endswith("/device/scenes"):
                return {"code": 200, "payload": {"capabilities": [{"instance": "lightScene", "parameters": {
                    "options": [{"name": "Rainbow", "value": {"id": 7, "paramId": 9}}]}}]}}
            return {"code": 200}

        govee_scenes._http_json.side_effect = govee
        self.assertEqual(govee_scenes.start_scene("all", "rainbow")["text"], "Rainbow started on 1 of 1 lights.")
        control = json.loads(sent[-1].data)["payload"]
        self.assertEqual(control["capability"], {"type": "devices.capabilities.dynamic_scene",
                                                 "instance": "lightScene", "value": {"id": 7, "paramId": 9}})
        self.assertEqual(control["device"], "BULB")
        self.assertEqual(sent[-1].get_header("Govee-api-key"), "test-key")

    def test_key_from_the_environment(self):
        self.assertEqual(govee_scenes.cloud_key(), "")
        with mock.patch.dict(os.environ, {"GOVEE_API_KEY": "  env-key  "}):
            self.assertEqual(govee_scenes.cloud_key(), "env-key")

    def test_data_folder(self):
        with mock.patch.dict(os.environ, {"GOVEE_LIGHTS_HOME": "~/somewhere"}):
            self.assertEqual(govee_lan._data_dir(), os.path.expanduser("~/somewhere"))
        with mock.patch.dict(os.environ, {"GOVEE_LIGHTS_HOME": ""}):
            self.assertTrue(govee_lan._data_dir().endswith("govee-lights"))

    def test_save_key(self):
        govee_scenes._http_json.side_effect = None
        govee_scenes._http_json.return_value = {"code": 200, "data": [{"device": "a"}]}
        text = govee_scenes.save_key(" abc-123-key ")
        self.assertNotIn("abc-123-key", text)
        with open(self.key_file) as f:
            self.assertEqual(f.read(), "abc-123-key\n")
        with self.assertRaises(govee_lan.GoveeError):
            govee_scenes.save_key("has a space")


class Detection(Base):
    """Finding every light: model labels, every network, listed addresses, and the Govee app's names."""

    registry = {"AA:01": {"ip": "10.0.0.5", "sku": "H6008"}, "AA:02": {"ip": "10.0.0.6", "sku": "H619A"},
                "AA:03": {"ip": "10.0.0.7", "sku": "H6199"}, "AA:04": {"ip": "10.0.0.8", "sku": "H9999"}}

    def test_kinds(self):
        self.assertEqual([govee_lan.kind_of(sku) for sku in ("H6008", "h619a", "H6199", "H7041", "H9999", "")],
                         ["bulb", "strip", "tv backlight", "outdoor light", "light", "light"])
        labels = [light.label for light in govee_lan.known_lights()]
        self.assertEqual(sorted(labels), ["bulb 1", "light 1", "strip 1", "tv backlight 1"])
        self.assertEqual([light.device for light in govee_lan.resolve("strips", govee_lan.known_lights())], ["AA:02"])
        self.assertEqual(len(govee_lan.resolve("TV Backlights", govee_lan.known_lights())), 1)

    def test_local_addresses(self):
        ifconfig = ("lo0: flags=8049<UP,LOOPBACK>\n\tinet 127.0.0.1 netmask 0xff000000\n"
                    "en0: flags=8863<UP>\n\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255\n"
                    "en1: flags=8863<UP>\n\tinet 169.254.3.4 netmask 0xffff0000\n"
                    "utun4: flags=8051<UP>\n\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff\n"
                    "en5: flags=8863<UP>\n\tinet 8.8.4.4 netmask 0xffffff00\n")
        run = patch(self, govee_lan.subprocess, "run", mock.Mock(return_value=mock.Mock(stdout=ifconfig)))
        self.assertEqual(govee_lan.local_addresses(), ["192.168.1.23", "10.8.0.2"])
        run.side_effect = OSError
        self.assertEqual(govee_lan.local_addresses(), [])

    def test_manual_addresses(self):
        with mock.patch.dict(os.environ, {"GOVEE_LIGHT_IPS": " 10.0.0.9, 10.0.0.10 10.0.0.11,"}):
            self.assertEqual(govee_lan.manual_addresses(), ["10.0.0.9", "10.0.0.10", "10.0.0.11"])
        with mock.patch.dict(os.environ, {"GOVEE_LIGHT_IPS": ""}):
            self.assertEqual(govee_lan.manual_addresses(), [])

    def test_probe_goes_out_on_every_network_and_to_listed_lights(self):
        sock = mock.Mock()
        govee_lan._probe(sock, b"probe", ["192.168.1.23", "10.8.0.2"], ["10.0.0.9"])
        self.assertEqual([c.args[2] for c in sock.setsockopt.call_args_list],
                         [socket.inet_aton("192.168.1.23"), socket.inet_aton("10.8.0.2")])
        self.assertEqual([c.args[1] for c in sock.sendto.call_args_list],
                         [govee_lan.SCAN_ADDR, govee_lan.SCAN_ADDR, ("10.0.0.9", 4001)])

    def test_probe_falls_back_to_the_default_network(self):
        sock = mock.Mock()
        sock.setsockopt.side_effect = OSError
        govee_lan._probe(sock, b"probe", ["192.168.1.23"], [])
        self.assertEqual([c.args[1] for c in sock.sendto.call_args_list], [govee_lan.SCAN_ADDR])

    def test_names_from_the_govee_app(self):
        registry = {"AA:01": {"sku": "H6008"}, "AA:02": {"sku": "H6008", "name": "desk"},
                    "AA:03": {"sku": "H6008"}, "AA:04": {"sku": "H6008"}, "AA:05": {"sku": "H6008"}}
        patch(self, govee_lan, "account_devices", lambda: [
            {"device": "AA:01", "deviceName": "  Bedroom   Lamp "}, {"device": "AA:02", "deviceName": "Kitchen"},
            {"device": "AA:03", "deviceName": "Twin"}, {"device": "AA:04", "deviceName": "twin"},
            {"device": "AA:05", "deviceName": "Desk"}])
        self.assertTrue(govee_lan._name_from_govee_app(registry))
        self.assertEqual({d: e.get("name", "") for d, e in registry.items()},
                         {"AA:01": "Bedroom Lamp", "AA:02": "desk", "AA:03": "", "AA:04": "", "AA:05": ""})
        self.assertFalse(govee_lan._name_from_govee_app(registry))

    def test_lights_missing_from_the_network_are_listed(self):
        patch(self, govee_lan, "account_devices", lambda: [{"device": "AA:01", "sku": "H6008", "deviceName": "a"},
                                                            {"device": "ZZ:09", "sku": "H6159", "deviceName": "Shelf"}])
        lines = govee_lan._not_on_the_network(govee_lan.known_lights())
        self.assertIn("- Shelf (H6159)", lines)
        self.assertEqual(len(lines), 3)
        patch(self, govee_lan, "account_devices", lambda: [])
        self.assertEqual(govee_lan._not_on_the_network(govee_lan.known_lights()), [])

    def test_account_devices(self):
        self.assertEqual(govee_scenes.account_devices(), [])  # no key: never asks Govee
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
        with open(self.key_file, "w") as f:
            f.write("key\n")
        cloud = patch(self, govee_scenes, "_cloud", mock.Mock(return_value={"data": [
            {"device": "AA:01", "sku": "H6008", "deviceName": "Lamp", "type": "devices.types.light"},
            {"device": "PP:01", "sku": "H5080", "deviceName": "Plug", "type": "devices.types.socket"},
            {"device": "AA:02", "sku": "H619A", "deviceName": "Strip"},
            {"device": "GG:01", "sku": "SameModeGroup", "deviceName": "Same-Model", "type": "devices.types.light"},
            {"device": "GG:02", "sku": "DreamViewScenic", "deviceName": "Scenic", "type": "devices.types.light"}]}))
        self.assertEqual([item["device"] for item in govee_scenes.account_devices()], ["AA:01", "AA:02"])
        self.assertEqual([item["device"] for item in govee_scenes.account_devices()], ["AA:01", "AA:02"])
        self.assertEqual(cloud.call_count, 1)  # cached


class Bluetooth(Base):
    """Bluetooth-only lights, through a stand-in for the Govee Bluetooth app."""

    registry = {"04:CB:5C:E7:53:6C:EA:44": {"ip": "10.0.0.5", "sku": "H6008"},
                "40EBDD3C-6650-1DE7-6F17-E9B5EE13C871": {"sku": "H617A", "bluetooth": True}}
    STRIP = "40EBDD3C-6650-1DE7-6F17-E9B5EE13C871"

    def setUp(self):
        super().setUp()
        self.sent = []  # (device, [hex packets], read)
        self.reply = {"onOff": 1, "brightness": 40}

        def fake_send(device, data, read=False):
            self.sent.append((device, [p.hex() for p in data], read))
            return dict(self.reply) if read else {}
        patch(self, govee_ble, "send", fake_send)

    def test_packets_match_what_the_strip_took(self):  # the bytes an H617A accepted on 2026-11-09
        self.assertEqual(govee_ble.packet(0x33, 0x01, 0x00).hex(), "3301" + "00" * 17 + "32")
        self.assertEqual(govee_ble.packet(0xAA, 0x01).hex(), "aa01" + "00" * 17 + "ab")
        self.assertEqual([p.hex()[:8] for p in govee_ble.packets({"power": True, "rgb": (255, 0, 0), "brightness": 40})],
                         ["33010100", "33051501", "33042800"])
        color = govee_ble.packets({"power": True, "rgb": (1, 2, 3)})[1]
        self.assertEqual(color[:14], bytes([0x33, 0x05, 0x15, 0x01, 1, 2, 3, 0, 0, 0, 0, 0, 0xFF, 0x7F]))
        self.assertEqual(len(color), 20)
        self.assertEqual(govee_ble.packets({"power": False}), [govee_ble.packet(0x33, 0x01, 0x00)])
        warm, cool = govee_ble.kelvin_rgb(2700), govee_ble.kelvin_rgb(6500)
        self.assertGreater(warm[0] - warm[2], cool[0] - cool[2])

    def test_names(self):
        self.assertEqual(govee_ble.parse_name("Govee_H617A_0A45"), ("H617A", "0A45"))
        self.assertEqual(govee_ble.parse_name("GVH6008EA45"), ("H6008", "EA45"))
        self.assertEqual(govee_ble.parse_name("ihoment_H6182_ab12"), ("H6182", "AB12"))
        for other in ("Someone's iPhone", "[TV] Living Room", "Govee_", ""):
            self.assertIsNone(govee_ble.parse_name(other))

    def test_replies(self):
        self.assertEqual(govee_ble.parse_state(["aa010100000000000000000000000000000000aa",
                                                "aa04280000000000000000000000000000000086", "3301" + "00" * 18, "zz"]),
                         {"onOff": 1, "brightness": 40})

    def test_run_reads_the_apps_file(self):
        patch(self, govee_ble, "available", lambda: True)
        patch(self, govee_ble, "_run", _ORIGINAL_RUN)  # the real one, with `open` faked below

        def fake_open(command, **kwargs):
            out = command[command.index("--out") + 1]
            with open(out, "w") as f:
                f.write(self.app_output)
        patch(self, govee_ble.subprocess, "run", fake_open)
        self.app_output = '{"id": "X1", "name": "Govee_H617A_0A45", "rssi": -30}\n{"id": "X2", "name": "phone"}\nexit 0\n'
        self.assertEqual(govee_ble.scan(1), [{"id": "X1", "name": "Govee_H617A_0A45", "rssi": -30, "sku": "H617A",
                                              "suffix": "0A45"}])
        self.app_output = "error couldn't connect: busy. Is the Govee app connected to it?\nexit 5\n"
        with self.assertRaisesRegex(govee_ble.BluetoothError, "Govee app connected"):
            govee_ble.scan(1)

    def test_wifi_lights_heard_over_bluetooth_are_skipped(self):
        registry = {"04:CB:5C:E7:53:6C:EA:44": {"ip": "10.0.0.5", "sku": "H6008"},
                    "1A:B3:E8:6E:81:06:7B:57": {"ip": "10.0.0.6", "sku": "H60B2"}}
        heard = [{"id": "B1", "name": "GVH6008EA45", "sku": "H6008", "suffix": "EA45"},
                 {"id": "B2", "name": "Govee_H60B2_7B57", "sku": "H60B2", "suffix": "7B57"},
                 {"id": "B3", "name": "Govee_H617A_0A45", "sku": "H617A", "suffix": "0A45"},
                 {"id": "B4", "name": "GVH6008EA45", "sku": "H6008", "suffix": "1234"}]
        self.assertTrue(govee_lan._add_bluetooth_lights(registry, heard))
        self.assertEqual(sorted(d for d, e in registry.items() if e.get("bluetooth")), ["B3", "B4"])
        self.assertFalse(govee_lan._add_bluetooth_lights(registry, heard))

    def test_listed_with_the_rest(self):
        lights = govee_lan.known_lights()
        strip = next(light for light in lights if light.bluetooth)
        self.assertEqual((strip.label, govee_lan.where(strip)), ("strip 1", "over Bluetooth"))
        self.assertEqual(govee_lan.describe({"onOff": 1, "brightness": 40}), "on, 40%")
        self.assertEqual(govee_lan.resolve("strips", lights), [strip])

    def test_set_over_bluetooth(self):
        text = govee_lan.set_lights("strip 1", color="blue", brightness=40)
        self.assertEqual(text, "On, color #0000ff, 40% for 1 light: 1 confirmed.")
        device, packets, read = self.sent[-1]
        self.assertEqual((device, read), (self.STRIP, True))
        self.assertEqual([p[:8] for p in packets], ["33010100", "33051501", "33042800"])

    def test_bluetooth_light_that_does_not_take_it(self):
        self.reply = {"onOff": 0}
        text = govee_lan.set_lights("strip 1", power="on")
        self.assertIn("Not confirmed: strip 1 reports off", text)
        self.assertEqual(len(self.sent), 2)  # tried twice

    def test_unreachable_bluetooth_light(self):
        def busy(device, data, read=False):
            raise govee_ble.BluetoothError("the light wasn't found")
        patch(self, govee_ble, "send", busy)
        self.assertIn("strip 1 didn't answer", govee_lan.set_lights("strip 1", power="off"))

    def test_scene_over_bluetooth(self):
        reply = govee_scenes.start_scene("strip 1", "aurora")
        self.assertEqual(reply["failed"], [])
        device, packets, _read = self.sent[-1]
        self.assertEqual(device, self.STRIP)
        self.assertEqual(packets, [p.hex() for p in govee_scenes.lan_packets(13898, TEST_LIBRARY["H617A"]["Aurora"]["param"])])
        self.assertNotIn("strips", govee_scenes.scene_lists("strip 1")["need_key"])


class FollowSpotify(Base):
    """The hub following Spotify, with Spotify, the artwork and sips stood in for."""

    registry = ONE_FAKE_LIGHT
    get, post = Page.get, Page.post

    def setUp(self):
        super().setUp()
        self.light = self.start_light()
        server = lights_page.ThreadingHTTPServer(("127.0.0.1", 0), lights_page.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.song = {"state": "playing", "id": "spotify:track:1", "artwork": "https://i.scdn.co/image/a",
                     "name": "Song", "artist": "Band"}
        patch(self, spotify_follow, "now_playing", lambda: dict(self.song))
        patch(self, spotify_follow, "fetch", lambda url: b"art")
        patch(self, spotify_follow, "pixels", lambda data: [(250, 20, 20)] * 30 + [(20, 20, 250)] * 10)
        self.sets = []
        real = govee_lan.set_lights_result
        def recording(*args, **kwargs):
            self.sets.append((args[0] if args else kwargs.get("target"), kwargs.get("color")))
            return real(*args, **kwargs)
        patch(self, govee_lan, "set_lights_result", recording)

    def test_one_change_per_song(self):
        self.assertFalse(self.follower.tick())  # off by default
        self.light.state["onOff"] = 1
        self.post("/api/spotify", {"enabled": True})
        self.assertTrue(self.follower.tick())
        self.assertEqual(self.sets, [("FAKE", "#0202ff")])  # a bulb takes the second color, vivid
        self.assertEqual(self.light.state["color"], {"r": 2, "g": 2, "b": 255})
        self.assertFalse(self.follower.tick())  # same song: nothing
        self.song["id"] = "spotify:track:2"
        self.assertTrue(self.follower.tick())
        self.assertEqual(len(self.sets), 2)
        status = json.loads(self.get("/api/spotify")[2])
        self.assertEqual((status["enabled"], status["track"]["name"], status["colors"]),
                         (True, "Song", ["#ff0202", "#0202ff"]))

    def test_lights_that_are_off_stay_off(self):
        self.light.state["onOff"] = 0
        self.follower.set(True)
        self.assertFalse(self.follower.tick())
        self.assertEqual((self.sets, self.light.state["onOff"]), ([], 0))

    def test_paused_ads_and_bad_art_leave_the_lights_alone(self):
        self.follower.set(True)
        self.song["state"] = "paused"
        self.assertFalse(self.follower.tick())
        self.song["state"] = "playing"
        patch(self, spotify_follow, "fetch", mock.Mock(side_effect=ValueError("not Spotify artwork")))
        self.assertFalse(self.follower.tick())
        patch(self, spotify_follow, "now_playing", lambda: None)
        self.assertFalse(self.follower.tick())
        self.assertEqual(self.sets, [])

    def test_a_look_picked_by_hand_turns_it_off(self):
        for path, body in (("/api/set", {"color": "red"}), ("/api/scene/warm", {}),
                           ("/api/tool/set_lights", {"color": "red"})):
            self.follower.set(True)
            self.post(path, body)
            self.assertFalse(self.follower.enabled, path)
        self.follower.set(True)
        self.post("/api/blink", {"target": "bulb 1"})  # blinking isn't a look
        self.assertTrue(self.follower.enabled)

    def test_switch_is_remembered(self):
        self.follower.set(True)
        self.assertTrue(lights_page.Follower(self.follower.path).enabled)
        self.follower.set(False)
        self.assertFalse(lights_page.Follower(self.follower.path).enabled)

    def test_not_allowed_is_reported(self):
        self.follower.set(True)
        patch(self, spotify_follow, "now_playing",
              mock.Mock(side_effect=spotify_follow.SpotifyNotAllowed("allow it in Automation")))
        self.assertFalse(self.follower.tick())
        self.assertIn("Automation", self.follower.describe())

    def test_chat_tools(self):
        self.assertIn("is off", govee_mcp.run_local("spotify_status", {}))
        self.assertTrue(govee_mcp.run_local("follow_spotify", {"on": True}).startswith("Following Spotify"))
        self.assertTrue(self.follower.enabled)
        reply = govee_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "follow_spotify", "arguments": {"on": False}}})
        self.assertFalse(reply["result"]["isError"])  # false is an answer, not a missing argument
        self.assertFalse(self.follower.enabled)


class Mcp(unittest.TestCase):
    def setUp(self):
        patch(self, govee_mcp, "HUB", "")

    def call(self, method, params=None, mid=1):
        return govee_mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize(self):
        self.assertEqual(self.call("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"],
                         "2025-06-18")
        self.assertEqual(self.call("initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"],
                         govee_mcp.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply(self):
        self.assertIsNone(govee_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools(self):
        names = [tool["name"] for tool in self.call("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["list_lights", "set_lights", "identify_light", "name_light", "list_scenes",
                                 "start_scene", "follow_spotify", "spotify_status"])
        self.assertEqual(self.call("tools/call", {"name": "fly"})["error"]["code"], -32602)
        missing = self.call("tools/call", {"name": "identify_light", "arguments": {}})["result"]
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["content"][0]["text"], "Missing: target")
        self.assertEqual(self.call("resources/list")["error"]["code"], -32601)

    def test_stdio(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = subprocess.run([sys.executable, os.path.join(HERE, "govee_mcp.py")], check=True, timeout=10,
                             input="".join(json.dumps(line) + "\n" for line in lines),
                             capture_output=True, text=True, env={**os.environ, "GOVEE_HUB": ""}).stdout
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)


if __name__ == "__main__":
    unittest.main()
