"""Shared wire protocol + transport for the Turing host agents.

Used by both `foreground_reporter.py` (Windows) and `linux_reporter.py`
(Ubuntu/Linux). Keeps the UDP broadcast+multicast channel, JSON schema, and
per-install host_id logic in one place so both platforms stay compatible with
the Mini-PC's receiver in modes.py.

Protocol v2 payload kinds (all sent to the same channel):
  {"v":2,"service":"turing-host-game","kind":"state","host_id":...,"hostname":...,
   "updated_at":...,"game":{...}|null,"media":{...}|null,"lock":{...}|null}
  {"v":2,"service":"turing-host-game","kind":"notification","host_id":...,
   "hostname":...,"updated_at":...,"app":...,"title":...,"body":...}

v1 (legacy, still accepted by the Mini-PC): a bare {"title","exe","pid","updated_at"}
with no "kind"/"host_id" — this module always sends v2, this note is just so the
receiver-side compatibility rule is documented in one place too.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path

PROTOCOL_VERSION = 2
DEFAULT_PORT = 8787
MULTICAST_GROUP = "239.255.87.87"
SERVICE_NAME = "turing-host-game"

# Reporter-side "is this worth sending as a game" filter. The Mini-PC re-validates
# everything against its own modes.py::KNOWN_GAMES before trusting it either way,
# so this list only needs to be a reasonable superset — keep both lists roughly in
# sync by hand (see modes.py::KNOWN_GAMES for the receiver-side authority).
KNOWN_GAME_STEMS = (
    "palworld",
    "palworld-win64-shipping",
    "cs2",
    "csgo",
    "r5apex",
    "fortniteclient-win64-shipping",
    "valorant",
    "valorant-win64-shipping",
    "rocketleague",
    "gta5",
    "gtav",
    "rdr2",
    "eldenring",
    "cyberpunk2077",
    "dota2",
    "hl2",
    "tf2",
    "bg3",
    "bg3_dx11",
    "starfield",
    "overwatch",
    "modernwarfare",
    "cod",
)


def _host_id_file() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "TuringHostAgent" / "host_id"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "turing-host-agent" / "host_id"


def get_or_create_host_id() -> str:
    """Stable per-install id, persisted locally; falls back to hostname if unwritable."""
    path = _host_id_file()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
        return new_id
    except OSError:
        return f"hostname:{socket.gethostname()}"


def build_state_payload(
    host_id: str,
    hostname: str,
    game: dict | None = None,
    media: dict | None = None,
    lock: dict | None = None,
) -> dict:
    payload = {
        "v": PROTOCOL_VERSION,
        "service": SERVICE_NAME,
        "kind": "state",
        "host_id": host_id,
        "hostname": hostname,
        "updated_at": time.time(),
    }
    if game is not None:
        payload["game"] = game
    if media is not None:
        payload["media"] = media
    if lock is not None:
        payload["lock"] = lock
    return payload


def build_notification_payload(host_id: str, hostname: str, app: str, title: str, body: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "service": SERVICE_NAME,
        "kind": "notification",
        "host_id": host_id,
        "hostname": hostname,
        "updated_at": time.time(),
        "app": app,
        "title": title,
        "body": body,
    }


def make_udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    return sock


def send_payload(sock: socket.socket, payload: dict, port: int = DEFAULT_PORT) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        sock.sendto(raw, ("255.255.255.255", port))
    except OSError:
        pass
    try:
        sock.sendto(raw, (MULTICAST_GROUP, port))
    except OSError:
        pass


def known_game_hit(name: str) -> bool:
    stem = Path(name).stem.lower()
    if stem in KNOWN_GAME_STEMS:
        return True
    return any(key in stem for key in KNOWN_GAME_STEMS if len(key) >= 4)
