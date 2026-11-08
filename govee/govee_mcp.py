#!/usr/bin/env python3
"""MCP server (stdio) for the Govee lights, so voice and chat agents can run them.

Register it with Codex or Claude Code by pointing them at this file; README.md has the commands.
It speaks newline-delimited JSON-RPC on stdin and stdout. Anything else goes to stderr.

Tool calls go to the lights hub (lights_page.py) when it's running, so every app shares one path to the lights and
only the hub needs macOS's Local Network permission. If the hub is down, the tools talk to the lights directly.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import govee_lan  # noqa: E402
import govee_scenes  # noqa: E402

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
HUB = os.environ.get("GOVEE_HUB", "http://127.0.0.1:8737")  # empty: always talk to the lights directly

TOOLS = [
    {
        "name": "list_lights",
        "title": "List lights",
        "description": "Scan the home network for Govee lights and show each one's name, room and state "
                       "(on or off, brightness, color). Use the names it shows as targets.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "set_lights",
        "title": "Set lights",
        "description": "Change Govee lights: power, brightness, color or white temperature. Setting brightness, "
                       "color or temperature also turns the lights on. Example: 'make the lights warm' is "
                       "target 'all' with temperature 'warm'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": "Which lights: 'all' (the default), a light's name, a room, 'bulbs', "
                                          "'floor lamps', or several separated by commas."},
                "power": {"type": "string", "enum": ["on", "off"]},
                "brightness": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Percent."},
                "color": {"type": "string",
                          "description": "A color name (red, orange, yellow, green, teal, cyan, blue, purple, "
                                         "magenta, pink, white, warm white, soft white, cool white) or a hex code "
                                         "like '#ff8800'."},
                "temperature": {"type": "string",
                                "description": "White light: warm, soft, neutral, cool, daylight, or kelvin from "
                                               "2000 to 9000."},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "identify_light",
        "title": "Blink a light",
        "description": "Blink one light twice so the person can see which one it is, then leave it as it was. "
                       "Use it to help the person name their lights.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "One light, like 'bulb 3'."}},
            "required": ["target"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "name_light",
        "title": "Name a light",
        "description": "Give one light a name, and optionally a room, saved for next time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "The light's current label, like 'bulb 3'."},
                "name": {"type": "string"},
                "room": {"type": "string"},
            },
            "required": ["target", "name"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "list_scenes",
        "title": "List scenes",
        "description": "List Govee's built-in scenes (Rainbow, Aurora, Fire and so on) for the chosen lights. The bulbs "
                       "and the floor lamps have different lists; the ones they share come first.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Which lights; 'all' by default."}},
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "start_scene",
        "title": "Start a scene",
        "description": "Start one of Govee's built-in scenes, like Rainbow, on the chosen lights. The floor lamps take "
                       "it directly; the bulbs need the Govee API key. Lights don't report scenes, so a started "
                       "scene can't be confirmed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The scene, as list_scenes shows it."},
                "target": {"type": "string", "description": "Which lights; 'all' by default."},
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]

HANDLERS = {
    "list_lights": lambda a: govee_lan.list_lights(),
    "set_lights": lambda a: govee_lan.set_lights(a.get("target") or "all", a.get("power"), a.get("brightness"),
                                                 a.get("color"), a.get("temperature")),
    "identify_light": lambda a: govee_lan.identify_light(a["target"]),
    "name_light": lambda a: govee_lan.name_light(a["target"], a["name"], a.get("room") or ""),
    "list_scenes": lambda a: govee_scenes.list_scenes(a.get("target") or "all"),
    "start_scene": lambda a: govee_scenes.start_scene(a.get("target") or "all", a["name"])["text"],
}


def run_local(name: str, args: dict) -> str:
    """Run a tool against the lights directly. The hub uses this too."""
    return HANDLERS[name](args)


def _via_hub(name: str, args: dict):
    """(ok, text) from the lights hub, or None if the hub isn't running."""
    if not HUB:
        return None
    request = urllib.request.Request(f"{HUB}/api/tool/{name}", data=json.dumps(args).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            reply = json.load(e)
        except ValueError:
            return False, f"The lights hub answered {e.code}."
        finally:
            e.close()
    except TimeoutError:
        return False, "The lights hub took too long to answer."  # don't retry directly: it may have acted
    except (urllib.error.URLError, ConnectionError):
        return None
    except ValueError:
        return False, "The lights hub sent back something unreadable."
    return bool(reply.get("ok")), str(reply.get("text") or "")


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _call(name: str, args: dict) -> dict:
    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
    missing = [key for key in schema.get("required", []) if not args.get(key)]
    hub = None if missing else _via_hub(name, args)
    if missing:
        text, is_error = "Missing: " + ", ".join(missing), True
    elif hub is not None:
        text, is_error = hub[1], not hub[0]
    else:
        try:
            text, is_error = run_local(name, args), False
        except govee_lan.GoveeError as e:
            text, is_error = str(e), True
        except Exception as e:  # report it rather than take the server down
            traceback.print_exc(file=sys.stderr)
            text, is_error = f"Unexpected error: {e}", True
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(message) -> dict | None:
    """Answer one JSON-RPC message. Returns None for notifications, which get no reply."""
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid request")
    if "id" not in message:
        return None
    mid, method, params = message["id"], message.get("method"), message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(mid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "govee-lights", "version": "0.1.0"},
            "instructions": "Controls the Govee lights on the home network. Call list_lights to see the lights' "
                            "names before picking out single lights, and list_scenes before start_scene.",
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in HANDLERS:
            return _error(mid, -32602, f"Unknown tool: {name}")
        return _result(mid, _call(name, params.get("arguments") or {}))
    return _error(mid, -32601, f"Method not found: {method}")


def main() -> None:
    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "Parse error")
        else:
            reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
