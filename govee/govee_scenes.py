"""Govee's own scenes (Rainbow, Aurora and the rest) for the lights.

The tree lamps (H60B2) take scenes over the home network: the scene's settings and code go to the lamp in Govee's
"ptReal" packets, laid out the way govee2mqtt sends them. The bulbs (H6008) ignore ptReal packets (tested
2026-11-01), so their scenes go through Govee's cloud API, which needs your own free Govee API key: set
GOVEE_API_KEY, or paste it on the Lights page, which keeps it in the data folder readable only by you.

Scene lists come from Govee's public scene library, one per model, cached in the data folder's scene-cache/ for a
week. Lights don't report which scene they're on, so a started scene can't be confirmed the way a color can.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import govee_ble
import govee_lan
from govee_lan import GoveeError

LIBRARY_URL = "https://app2.govee.com/appsku/v1/light-effect-libraries?sku={sku}"
CLOUD_URL = "https://openapi.api.govee.com/router/api/v1"
KEY_FILE = os.path.join(govee_lan.DATA_DIR, "govee-api-key")
CACHE_DIR = os.path.join(govee_lan.DATA_DIR, "scene-cache")
LAN_SCENE_MODELS = {"H60B2"}  # the H6008 bulbs ignore ptReal packets
NEED_KEY = "bulb scenes go through Govee's cloud and need the Govee API key; add it on the Lights page"
WEEK, DAY = 7 * 86400, 86400


# --- fetching, with a local copy ---

def _http_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GoveeError("Govee didn't accept the API key") from None
        raise GoveeError(f"Govee answered {e.code} ({e.reason})") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GoveeError(f"couldn't reach Govee ({getattr(e, 'reason', e)})") from None


def _cached(name: str, max_age: float, fetch) -> dict:
    """fetch(), keeping a copy; a fresh copy skips the fetch, and an old one stands in when Govee can't be reached."""
    path = os.path.join(CACHE_DIR, name)
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        data = fetch()
    except GoveeError as problem:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            raise problem from None
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def library(sku: str) -> dict:
    """{scene name: {"code", "param", "category"}} for a model, from Govee's public scene library."""
    def fetch():
        reply = _http_json(urllib.request.Request(LIBRARY_URL.format(sku=sku), headers={"AppVersion": "6.5.02"}))
        scenes = {}
        for category in (reply.get("data") or {}).get("categories") or []:
            for scene in category.get("scenes") or []:
                effect = next((e for e in scene.get("lightEffects") or [] if e.get("sceneCode")), None)
                if effect and scene.get("sceneName") and scene["sceneName"] not in scenes:
                    scenes[scene["sceneName"]] = {"code": effect["sceneCode"], "param": effect.get("scenceParam") or "",
                                                  "category": category.get("categoryName") or ""}
        if not scenes:
            raise GoveeError(f"Govee's scene library has nothing for {sku}")
        return scenes
    return _cached(f"library-{sku}.json", WEEK, fetch)


def _find(scenes: dict, name: str):
    wanted = " ".join(str(name).lower().split())
    return next((value for scene, value in scenes.items() if scene.lower() == wanted), None)


# --- over the home network (ptReal) ---

def _finish(body: list) -> bytes:
    """Pad to 19 bytes and add the XOR checksum: one 20-byte packet."""
    body = body + [0] * (19 - len(body))
    check = 0
    for b in body:
        check ^= b
    return bytes(body + [check])


def lan_packets(code: int, param: str) -> list[bytes]:
    """The scene's settings (if any) in a3 rows, then the 33 05 04 packet that starts it.

    First row: a3 00 01 <rows> 02 + 14 bytes. Then a3 <n> + 17 bytes per row; the last row's index is ff."""
    data = base64.b64decode(param or "")
    packets = []
    if data:
        chunks = [data[:14]] + [data[i:i + 17] for i in range(14, len(data), 17)]
        for i, chunk in enumerate(chunks):
            index = 0xFF if i == len(chunks) - 1 else i
            head = [0xA3, index, 0x01, len(chunks), 0x02] if i == 0 else [0xA3, index]
            packets.append(_finish(head + list(chunk)))
    packets.append(_finish([0x33, 0x05, 0x04, code & 0xFF, code >> 8]))
    return packets


def lan_start(light: govee_lan.Light, scene: dict) -> None:
    packets = lan_packets(scene["code"], scene["param"])
    message = govee_lan._packet("ptReal", {"command": [base64.b64encode(p).decode() for p in packets]})
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _ in range(2):  # a scene can't be confirmed, so say it twice in case one is dropped
            govee_lan._sendto(s, message, (light.ip, govee_lan.CONTROL_PORT))
            time.sleep(0.15)


# --- through Govee's cloud (needs the API key) ---

def cloud_key() -> str:
    """$GOVEE_API_KEY if it's set, else the key saved from the Lights page."""
    if os.environ.get("GOVEE_API_KEY", "").strip():
        return os.environ["GOVEE_API_KEY"].strip()
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _cloud(path: str, body: dict | None = None, key: str = "") -> dict:
    key = key or cloud_key()
    if not key:
        raise GoveeError(NEED_KEY)
    request = urllib.request.Request(CLOUD_URL + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Govee-API-Key": key, "Content-Type": "application/json"})
    reply = _http_json(request)
    if reply.get("code", 200) != 200:
        raise GoveeError(f"Govee said: {reply.get('message') or reply.get('msg') or reply.get('code')}")
    return reply


def save_key(key: str) -> str:
    """Check the key with Govee, then keep it where only this account can read it. Never echoes it."""
    key = str(key or "").strip()
    if not key or len(key) > 200 or any(c.isspace() for c in key):
        raise GoveeError("that doesn't look like an API key")
    devices = _cloud("/user/devices", key=key).get("data") or []
    os.makedirs(os.path.dirname(KEY_FILE), mode=0o700, exist_ok=True)
    tmp = KEY_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.replace(tmp, KEY_FILE)
    return f"Saved. Govee's cloud knows {len(devices)} of your devices, so bulb scenes can start."


def account_devices() -> list:
    """The lights in the Govee account, cached for a day; [] without a key or when Govee can't be reached.
    Only real lights: plugs, sensors, groups and DreamView scenes are left out."""
    if not cloud_key():
        return []

    def fetch():
        return [{"device": item.get("device"), "sku": item.get("sku"), "deviceName": item.get("deviceName")}
                for item in _cloud("/user/devices").get("data") or []
                if item.get("device") and str(item.get("type") or "devices.types.light").endswith(".light")
                and re.fullmatch(r"H[0-9A-Z]{4,6}", str(item.get("sku") or ""))]  # not groups or DreamView scenes
    try:
        return _cached("account-devices.json", DAY, fetch)
    except GoveeError:
        return []


def cloud_scenes(light: govee_lan.Light) -> dict:
    """{scene name: value} for one light from Govee's cloud, cached for a day."""
    def fetch():
        reply = _cloud("/device/scenes", {"requestId": str(uuid.uuid4()),
                                          "payload": {"sku": light.sku, "device": light.device}})
        scenes = {}
        for capability in (reply.get("payload") or {}).get("capabilities") or []:
            if capability.get("instance") == "lightScene":
                for option in (capability.get("parameters") or {}).get("options") or []:
                    scenes[option["name"]] = option["value"]
        if not scenes:
            raise GoveeError(f"Govee's cloud lists no scenes for {light.label}")
        return scenes
    return _cached(f"cloud-{light.device.replace(':', '')}.json", DAY, fetch)


def cloud_start(light: govee_lan.Light, name: str) -> None:
    value = _find(cloud_scenes(light), name)
    if value is None:
        raise GoveeError(f"Govee's cloud has no scene called {name} for the {light.kind}s")
    _cloud("/device/control", {"requestId": str(uuid.uuid4()), "payload": {
        "sku": light.sku, "device": light.device,
        "capability": {"type": "devices.capabilities.dynamic_scene", "instance": "lightScene", "value": value}}})


# --- what a person asks for ---

def scene_lists(target="all") -> dict:
    """The scene names for the chosen lights: the ones they all have, each kind's full list, and each kind's list
    in Govee's own groups and order ("Planet": Mercury, Venus, ...)."""
    lights = govee_lan.resolve(target, govee_lan.known_lights())
    kinds: dict[str, list] = {}
    categories: dict[str, list] = {}
    need_key = set()
    for light in lights:
        group = light.kind + "s"
        if group not in kinds:
            try:
                scenes = library(light.sku)
            except GoveeError:
                scenes = {}
            kinds[group] = sorted(scenes, key=str.lower)
            by_category: dict[str, list] = {}
            for name, scene in scenes.items():
                by_category.setdefault(scene.get("category") or "Other", []).append(name)
            categories[group] = [{"name": category, "scenes": names} for category, names in by_category.items()]
        if light.sku not in LAN_SCENE_MODELS and not light.bluetooth:
            need_key.add(group)
    common = sorted(set.intersection(*(set(names) for names in kinds.values())), key=str.lower) if kinds else []
    return {"common": common, "kinds": kinds, "categories": categories, "need_key": sorted(need_key),
            "key_saved": bool(cloud_key())}


def list_scenes(target="all") -> str:
    lists = scene_lists(target)
    if not lists["kinds"]:
        return govee_lan.NO_LIGHTS
    lines = [f"Scenes all of them have ({len(lists['common'])}): " + ", ".join(lists["common"])]
    if len(lists["kinds"]) > 1:
        for group, names in lists["kinds"].items():
            extra = [n for n in names if n not in lists["common"]]
            lines.append(f"Only the {group} ({len(extra)} more): " + ", ".join(extra))
    if lists["need_key"] and not lists["key_saved"]:
        lines.append(f"Note: the {' and '.join(lists['need_key'])} need the Govee API key for scenes, and it isn't "
                     "saved yet.")
    return "\n".join(lines)


def _canonical(name: str, lights: list) -> str:
    """The scene's name as Govee spells it, or "" if none of these lights' models has it."""
    for sku in sorted({light.sku for light in lights}):
        try:
            scenes = library(sku)
        except GoveeError:
            continue
        wanted = " ".join(str(name).lower().split())
        match = next((scene for scene in scenes if scene.lower() == wanted), "")
        if match:
            return match
    return ""


def start_scene(target="all", name="") -> dict:
    """Start one of Govee's scenes. Returns {"text", "failed"} like set_lights_result."""
    if not str(name).strip():
        raise GoveeError("say which scene, like Rainbow")
    lights = govee_lan.resolve(target, govee_lan.known_lights())
    if not lights:
        return {"text": govee_lan.NO_LIGHTS, "failed": []}
    title = _canonical(name, lights)
    if not title:
        raise GoveeError(f'no scene called "{name}". The scene list shows the names')
    # Lights whose model doesn't have this scene are left alone, not counted as failures.
    plans, skipped, problems = [], set(), {}
    for light in lights:
        try:
            scene = _find(library(light.sku), title)
        except GoveeError as e:
            problems.setdefault(str(e), []).append(light.label)
            continue
        if scene is None:
            skipped.add(light.kind + "s")
        else:
            plans.append((light, scene))
    if plans:
        govee_lan.send([light for light, _ in plans], {"power": True})  # a scene doesn't show on a light that's off

    def start(plan):
        light, scene = plan
        try:
            if light.bluetooth:  # the same packets ptReal carries, written straight to the light
                govee_ble.send(light.device, lan_packets(scene["code"], scene["param"]))
            elif light.sku in LAN_SCENE_MODELS:
                lan_start(light, scene)
            else:
                cloud_start(light, title)
            return light, None
        except (GoveeError, govee_ble.BluetoothError) as e:
            return light, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(start, plans))
    for light, problem in results:
        if problem:
            problems.setdefault(problem, []).append(light.label)
    started = sum(1 for _, problem in results if not problem)
    text = f"{title} started on {started} of {len(lights)} lights."
    if skipped:
        text += f" The {' and '.join(sorted(skipped))} don't have it."
    for problem, labels in problems.items():
        text += f" Not on {', '.join(labels)}: {problem}."
    return {"text": text, "failed": [label for labels in problems.values() for label in labels]}
