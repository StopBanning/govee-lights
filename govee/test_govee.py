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

import govee_lan
import govee_mcp
import govee_scenes
import lights_page

HERE = os.path.dirname(os.path.abspath(__file__))
# Govee's real Rainbow settings for the H6008, and a made-up block for the H60B2's Aurora.
RAINBOW_BULB = bytes.fromhex("8301ffebff0f0007ff0000ff7f00ffff0000ff0000ffff0000ff8b00ff")
TEST_LIBRARY = {
    "H6008": {"Rainbow": {"code": 1175, "param": base64.b64encode(RAINBOW_BULB).decode(), "category": "Natural"},
              "Fire": {"code": 1176, "param": "", "category": "Natural"}},
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
                                 "start_scene"])
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
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main()
