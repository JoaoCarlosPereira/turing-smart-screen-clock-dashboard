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
import re
import shutil
import socket
import subprocess
import time
import uuid
from functools import lru_cache
from pathlib import Path

PROTOCOL_VERSION = 2
DEFAULT_PORT = 8787
MULTICAST_GROUP = "239.255.87.87"
SERVICE_NAME = "turing-host-game"


def game_executable_hit(exe: str) -> bool:
    """Recognize known executables and games installed in Steam libraries."""
    path = (exe or "").replace("\\", "/")
    match = re.search(r"/steamapps/common/([^/]+)/(.+\.exe)$", path, re.IGNORECASE)
    if not match:
        return known_game_hit(path.rsplit("/", 1)[-1])
    folder, relative = match.groups()
    folder = folder.casefold()
    stem = relative.rsplit("/", 1)[-1][:-4].casefold()
    if any(part in folder for part in ("steamworks", "redist", "proton", "steamlinuxruntime")):
        return False
    return not any(part in stem for part in ("launcher", "crash", "unins", "setup", "redist", "overlay", "anticheat", "easyanticheat"))


@lru_cache(maxsize=128)
def steam_appid_for_executable(exe: str) -> str:
    """Find the matching Steam app manifest in the executable's library."""
    path = Path(exe)
    for parent in path.parents:
        if parent.name.casefold() != "common" or parent.parent.name.casefold() != "steamapps":
            continue
        try:
            folder = path.relative_to(parent).parts[0]
            for manifest in parent.parent.glob("appmanifest_*.acf"):
                try:
                    content = manifest.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                install = re.search(r'"installdir"\s*"([^"]+)"', content, re.IGNORECASE)
                appid = re.fullmatch(r"appmanifest_(\d+)\.acf", manifest.name, re.IGNORECASE)
                if install and appid and install.group(1).casefold() == folder.casefold():
                    return appid.group(1)
        except (OSError, ValueError, IndexError):
            pass
        break
    return ""

# Reporter-side "is this worth sending as a game" filter. The Mini-PC re-validates
# everything against its own modes.py::KNOWN_GAMES before trusting it either way,
# so this list only needs to be a reasonable superset — keep both lists roughly in
# sync by hand (see modes.py::KNOWN_GAMES for the receiver-side authority).
KNOWN_GAME_STEMS = (
    "palworld",
    "palworld-win64-shipping",
    "icarus",
    "icarus-win64-shipping",
    "lotf2",
    "lotf2-win64-shipping",
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
    "helldivers2",
    "valheim",
    "satisfactory",
    "subnautica",
    "rust",
    "terraria",
    "stardewvalley",
    "lethalcompany",
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
    desktop: dict | None = None,
    metrics: dict | None = None,
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
    if desktop is not None:
        payload["desktop"] = desktop
    if metrics is not None:
        payload["metrics"] = metrics
    return payload


def sample_gpu_metrics() -> dict | None:
    """Read NVIDIA metrics, or AMD VRAM/utilization from Linux sysfs."""
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                fields = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
                if len(fields) >= 4:
                    gpu_usage, gpu_temp, memory_used, memory_total = (float(value) for value in fields[:4])
                    metrics = {"gpu_usage": gpu_usage, "gpu_temp": gpu_temp}
                    if memory_total > 0:
                        metrics["display_memory"] = f"{memory_used:.0f} MiB / {memory_total:.0f} MiB"
                    return metrics
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    # Linux AMD drivers expose these counters through DRM sysfs.
    if os.name != "nt":
        for device in Path("/sys/class/drm").glob("card[0-9]/device"):
            try:
                used = int((device / "mem_info_vram_used").read_text().strip())
                total = int((device / "mem_info_vram_total").read_text().strip())
            except (OSError, ValueError):
                continue
            metrics = {"display_memory": f"{used / 1048576:.0f} MiB / {total / 1048576:.0f} MiB"}
            try:
                metrics["gpu_usage"] = float((device / "gpu_busy_percent").read_text().strip())
            except (OSError, ValueError):
                pass
            return metrics
    return None


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
    if "steamapps" in name.lower() and "common" in name.lower():
        return True
    if stem.endswith(("-win64-shipping", "-win32-shipping")):
        base = stem.split("-win")[0]
        if base in KNOWN_GAME_STEMS:
            return True
    return any(key in stem for key in KNOWN_GAME_STEMS if len(key) >= 4)
