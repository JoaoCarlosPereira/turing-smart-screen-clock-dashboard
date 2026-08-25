#!/usr/bin/env python3
"""
Mode system for Turing Smart Screen.

Manages multiple display modes: MAIN (clock/dashboard), MULTIMEDIA (media info),
GAMER (gaming overlay), and LOCKED (session lock — time + lock icon only).
"""

import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import psutil

from library.lcd.lcd_comm_rev_a import Orientation
from library.log import logger


# ---------------------------------------------------------------------------
# Enums & data types
# ---------------------------------------------------------------------------

class Mode(Enum):
    """Available display modes."""
    MAIN = auto()
    MULTIMEDIA = auto()
    GAMER = auto()
    LOCKED = auto()


@dataclass
class LockInfo:
    """Session lock state for LOCKED mode."""
    is_locked: bool = False
    source: str = ""  # logind / gnome / freedesktop


@dataclass
class MultimediaInfo:
    """Information about currently playing media."""
    title: str = ""
    artist: str = ""
    album: str = ""
    app_name: str = ""
    cover_url: str = ""
    cover_image: Optional[object] = None  # PIL Image
    is_playing: bool = False
    position: float = 0.0
    length: float = 0.0
    player_id: str = ""  # MPRIS identity
    volume: float = 0.0  # 0.0–1.0 system sink volume
    muted: bool = False


@dataclass
class GamerInfo:
    """Information about the currently running game."""
    game_name: str = ""
    game_art_path: str = ""
    game_art_image: Optional[object] = None  # PIL Image (hero / backdrop)
    game_logo_image: Optional[object] = None  # PIL Image
    game_capsule_image: Optional[object] = None  # PIL Image (portrait cover)
    steam_appid: str = ""
    theme_accent: tuple = (255, 160, 64)
    theme_accent2: tuple = (78, 132, 255)
    theme_panel: tuple = (11, 20, 34)
    process_name: str = ""
    process_pid: Optional[int] = None
    fps: float = 0.0
    cpu_usage: float = 0.0
    cpu_temp: float = 0.0
    gpu_usage: float = 0.0
    gpu_temp: float = 0.0
    memory_usage: float = 0.0  # system RAM percent 0-100
    display_memory: str = ""
    uptime: str = ""
    elapsed: str = ""
    detected_at: Optional[datetime] = None


@dataclass
class ModeState:
    """Current state of the mode system."""
    current_mode: Mode = Mode.MAIN
    previous_mode: Mode = Mode.MAIN
    multimedia: MultimediaInfo = field(default_factory=MultimediaInfo)
    gamer: GamerInfo = field(default_factory=GamerInfo)
    lock: LockInfo = field(default_factory=LockInfo)
    last_switch_time: float = 0.0
    switch_cooldown: float = 6.0  # seconds before allowing another switch
    # Require consecutive hits/misses so flaky detection does not flap modes
    game_hits: int = 0
    game_misses: int = 0
    media_hits: int = 0
    media_misses: int = 0
    lock_hits: int = 0
    lock_misses: int = 0
    enter_confirm: int = 2
    leave_confirm: int = 2
    lock_enter_confirm: int = 1  # lock/unlock should feel immediate
    lock_leave_confirm: int = 1


# ---------------------------------------------------------------------------
# Known games / processes database
# ---------------------------------------------------------------------------

# Map of known game process names -> display names and art paths
KNOWN_GAMES = {
    # Popular games (process name -> display name)
    "csgo": "Counter-Strike 2",
    "cs2": "Counter-Strike 2",
    "valorant": "Valorant",
    "valorant-win64-shipping": "Valorant",
    "rocketleague": "Rocket League",
    "r5apex": "Apex Legends",
    "fortniteclient-win64-shipping": "Fortnite",
    "minecraft": "Minecraft",
    "gta5": "GTA V",
    "gtav": "GTA V",
    "rdr2": "Red Dead Redemption 2",
    "cod": "Call of Duty",
    "codmw": "Call of Duty",
    "cod.exe": "Call of Duty",
    "modernwarfare": "Call of Duty",
    "overwatch": "Overwatch 2",
    "overwatch2": "Overwatch 2",
    "ow2": "Overwatch 2",
    "eldenring": "Elden Ring",
    "starfield": "Starfield",
    "cyberpunk2077": "Cyberpunk 2077",
    "godofwar": "God of War Ragnarök",
    "spiderman": "Marvel's Spider-Man",
    "hogwartslegacy": "Hogwarts Legacy",
    "palworld": "Palworld",
    "palworld-win64-shipping": "Palworld",
    "left4dead2": "Left 4 Dead 2",
    "tf2": "Team Fortress 2",
    "bg3": "Baldur's Gate 3",
    "baldur": "Baldur's Gate 3",
    "stellaris": "Stellaris",
    "civilizationvi": "Civilization VI",
    "dota2": "Dota 2",
    "hl2": "Half-Life 2",
}

# Never treat these as games (Steam UI / helpers always running)
NON_GAME_PROCESSES = {
    "steam",
    "steamwebhelper",
    "steam-runtime-launcher-service",
    "srt-logger",
    "srt-bwrap",
    "pv-adverb",
    "steamwebhelper_sniper_wrap.sh",
}

# Moonlight / Sunshine GameStream client process names (Linux + Windows)
MOONLIGHT_PROCESS_NAMES = {
    "moonlight",
    "moonlight-qt",
    "moonlight.exe",
    "moonlight-qt.exe",
}

# NVIDIA GameStream / Sunshine ports used while a session is active
GAMESTREAM_PORTS = {
    47984, 47989, 47990, 47991, 47992, 47995, 47996, 47998, 47999, 48000, 48010,
}

_MOONLIGHT_SERVERINFO_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_MOONLIGHT_SERVERINFO_TTL = 8.0
_MOONLIGHT_SERVERINFO_FAIL_TTL = 20.0
_MOONLIGHT_HOSTS_CACHE: tuple[float, list[dict]] | None = None
_MOONLIGHT_HOSTS_TTL = 30.0
_MOONLIGHT_JOURNAL_APPID_CACHE: tuple[float, Optional[str]] | None = None
_MOONLIGHT_JOURNAL_TTL = 10.0
_MOONLIGHT_UNIQUEID = "0123456789ABCDEF"
_HOST_GAME_HELPER_CACHE: tuple[float, Optional[dict]] | None = None
_HOST_GAME_HELPER_TTL = 4.0
_HOST_GAME_HELPER_FAIL_TTL = 12.0
_HOST_GAME_HELPER_DEFAULT_PORT = 8787
_HOST_GAME_HELPER_MULTICAST = "239.255.87.87"
_HOST_GAME_HELPER_SERVICE = "turing-host-game"
_HOST_GAME_UDP_CACHE: Optional[dict] = None
_HOST_GAME_UDP_CACHE_MONO: float = 0.0
_HOST_GAME_UDP_LOCK = threading.Lock()
_HOST_GAME_UDP_THREAD: Optional[threading.Thread] = None

_FOREGROUND_TITLE_IGNORE = {
    "",
    "program manager",
    "windows input experience",
    "windows shell experience host",
    "search",
    "searchhost",
    "start",
    "task switching",
    "nvidia geforce overlay",
    "nvidia share",
    "game bar",
    "xbox game bar",
    "moonlight",
    "steam",
    "steam big picture mode",
    "sunshine",
    "windows powershell",
    "windows terminal",
    "command prompt",
    "cmd.exe",
    "c:\\windows\\system32\\cmd.exe",
}

# Well-known Steam AppIDs (useful for Remote Play when the game isn't installed locally)
STEAM_APP_NAMES = {
    "730": "Counter-Strike 2",
    "570": "Dota 2",
    "440": "Team Fortress 2",
    "550": "Left 4 Dead 2",
    "1172470": "Apex Legends",
    "252950": "Rocket League",
    "271590": "GTA V",
    "1174180": "Red Dead Redemption 2",
    "1245620": "Elden Ring",
    "1091500": "Cyberpunk 2077",
    "1623730": "Palworld",
    "1086940": "Baldur's Gate 3",
    "1938090": "Call of Duty",
    "2357570": "Overwatch 2",
    "420": "Half-Life 2",
}

# Process stem → Steam AppID (for local launches without streaming_client)
KNOWN_GAME_APPIDS = {
    "csgo": "730",
    "cs2": "730",
    "dota2": "570",
    "tf2": "440",
    "left4dead2": "550",
    "r5apex": "1172470",
    "rocketleague": "252950",
    "gta5": "271590",
    "gtav": "271590",
    "rdr2": "1174180",
    "eldenring": "1245620",
    "cyberpunk2077": "1091500",
    "palworld": "1623730",
    "palworld-win64-shipping": "1623730",
    "bg3": "1086940",
    "baldur": "1086940",
    "overwatch": "2357570",
    "overwatch2": "2357570",
    "ow2": "2357570",
    "hl2": "420",
}

STEAM_NAME_CACHE_PATH = Path.home() / ".cache" / "turing-clock" / "steam-app-names.json"
GAME_ART_CACHE_DIR = Path.home() / ".cache" / "turing-clock" / "game-art"
_STEAM_NAME_CACHE: dict[str, str] = {}
_STEAM_NAME_CACHE_LOADED = False
_GAME_THEME_CACHE: dict[str, dict] = {}

# Art directory paths to search for game artwork
GAME_ART_ROOTS = (
    Path.home() / "Pictures",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path("/usr/share/icons"),
)


def _steam_roots() -> list[Path]:
    roots = [
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "debian-installation",
        Path.home() / ".local" / "share" / "Steam",
    ]
    found: list[Path] = []
    for root in roots:
        if root.is_dir() and root not in found:
            found.append(root)
    return found


def _load_steam_name_cache() -> dict[str, str]:
    global _STEAM_NAME_CACHE, _STEAM_NAME_CACHE_LOADED
    if _STEAM_NAME_CACHE_LOADED:
        return _STEAM_NAME_CACHE
    _STEAM_NAME_CACHE_LOADED = True
    _STEAM_NAME_CACHE = dict(STEAM_APP_NAMES)
    try:
        if STEAM_NAME_CACHE_PATH.is_file():
            data = json.loads(STEAM_NAME_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _STEAM_NAME_CACHE.update({str(k): str(v) for k, v in data.items()})
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Steam name cache load failed: %s", exc)
    return _STEAM_NAME_CACHE


def _save_steam_name_cache():
    try:
        STEAM_NAME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STEAM_NAME_CACHE_PATH.write_text(
            json.dumps(_STEAM_NAME_CACHE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("Steam name cache save failed: %s", exc)


def _name_from_appmanifest(appid: str) -> Optional[str]:
    for root in _steam_roots():
        candidates = [
            root / "steamapps" / f"appmanifest_{appid}.acf",
            root / "steamapps" / "common" / f"appmanifest_{appid}.acf",
        ]
        # Also scan extra libraries from libraryfolders.vdf
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                    lib = Path(match.group(1)) / "steamapps" / f"appmanifest_{appid}.acf"
                    candidates.append(lib)
            except OSError:
                pass
        for path in candidates:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(r'"name"\s+"([^"]+)"', text)
            if match:
                return match.group(1)
    return None


def _name_from_steam_store(appid: str) -> Optional[str]:
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=portuguese"
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        entry = payload.get(str(appid)) or {}
        if entry.get("success") and isinstance(entry.get("data"), dict):
            name = entry["data"].get("name")
            if name:
                return str(name)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("Steam store lookup failed for %s: %s", appid, exc)
    return None


def resolve_steam_app_name(appid: str) -> str:
    """Resolve a Steam AppID to a display name (cache → manifest → store API)."""
    appid = str(appid).strip()
    if not appid.isdigit():
        return f"Steam App {appid}"
    cache = _load_steam_name_cache()
    if appid in cache:
        return cache[appid]
    name = _name_from_appmanifest(appid) or _name_from_steam_store(appid)
    if name:
        cache[appid] = name
        _STEAM_NAME_CACHE[appid] = name
        _save_steam_name_cache()
        return name
    return f"Steam App {appid}"


def _extract_steam_appid(cmdline: list[str] | str) -> Optional[str]:
    if isinstance(cmdline, list):
        text = " ".join(str(part) for part in cmdline)
    else:
        text = cmdline or ""
    match = re.search(r"(?:--appid|--gameid|AppId|appid)[=:\s]+(\d{3,})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _moonlight_config_paths() -> list[Path]:
    """Locate Moonlight.conf across native, Snap and Flatpak installs."""
    home = Path.home()
    candidates = [
        home / "snap/moonlight/current/.config/Moonlight Game Streaming Project/Moonlight.conf",
        home / ".config/Moonlight Game Streaming Project/Moonlight.conf",
        home
        / ".var/app/com.moonlight_stream.Moonlight/config/Moonlight Game Streaming Project/Moonlight.conf",
    ]
    snap_root = home / "snap" / "moonlight"
    if snap_root.is_dir():
        for path in sorted(
            snap_root.glob("*/.config/Moonlight Game Streaming Project/Moonlight.conf"),
            reverse=True,
        ):
            if path not in candidates:
                candidates.append(path)
    return [path for path in candidates if path.is_file()]


def _parse_moonlight_hosts(conf_text: str) -> list[dict]:
    """Parse Qt QSettings host/app entries from Moonlight.conf."""
    hosts: dict[str, dict] = {}
    for match in re.finditer(r"^(\d+)\\([^=]+)=(.*)$", conf_text, re.MULTILINE):
        idx, key, value = match.group(1), match.group(2), match.group(3)
        host = hosts.setdefault(idx, {"apps": {}})
        if key.startswith("apps\\"):
            parts = key.split("\\")
            if len(parts) >= 3:
                app = host["apps"].setdefault(parts[1], {})
                app[parts[2]] = value
            continue
        host[key] = value

    parsed: list[dict] = []
    for host in hosts.values():
        apps = []
        for app in host.get("apps", {}).values():
            name = (app.get("name") or "").strip()
            if not name:
                continue
            apps.append({"id": str(app.get("id") or "").strip(), "name": name})
        ips = []
        for key in ("localaddress", "manualaddress", "remoteaddress"):
            ip = (host.get(key) or "").strip()
            if ip and ip not in ips:
                ips.append(ip)
        parsed.append(
            {
                "hostname": (host.get("hostname") or host.get("manualaddress") or "Moonlight").strip(),
                "ips": ips,
                "apps": apps,
                "uuid": (host.get("uuid") or "").strip(),
            }
        )
    return parsed


def _moonlight_hosts() -> list[dict]:
    """Cached list of paired Moonlight hosts and their apps."""
    global _MOONLIGHT_HOSTS_CACHE
    now = time.monotonic()
    if _MOONLIGHT_HOSTS_CACHE and (now - _MOONLIGHT_HOSTS_CACHE[0]) < _MOONLIGHT_HOSTS_TTL:
        return _MOONLIGHT_HOSTS_CACHE[1]

    hosts: list[dict] = []
    seen_uuids: set[str] = set()
    for path in _moonlight_config_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for host in _parse_moonlight_hosts(text):
            key = host.get("uuid") or f"{host.get('hostname')}|{','.join(host.get('ips') or [])}"
            if key in seen_uuids:
                continue
            seen_uuids.add(key)
            hosts.append(host)

    _MOONLIGHT_HOSTS_CACHE = (now, hosts)
    return hosts


def _query_sunshine_current_game(ip: str) -> Optional[str]:
    """Best-effort Sunshine currentgame id via the same HTTP probe Moonlight uses."""
    ip = (ip or "").strip()
    if not ip:
        return None

    now = time.monotonic()
    cached = _MOONLIGHT_SERVERINFO_CACHE.get(ip)
    if cached and (now - cached[0]) < (
        _MOONLIGHT_SERVERINFO_TTL if cached[1] is not None else _MOONLIGHT_SERVERINFO_FAIL_TTL
    ):
        return cached[1]

    current: Optional[str] = None
    url = (
        f"http://{ip}:47989/serverinfo"
        f"?uniqueid={_MOONLIGHT_UNIQUEID}"
    )
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=0.25) as resp:
            text = resp.read().decode("utf-8", "ignore")
        match = re.search(r"<currentgame>(\d+)</currentgame>", text, re.IGNORECASE)
        if match:
            current = match.group(1)
    except Exception:
        current = None

    _MOONLIGHT_SERVERINFO_CACHE[ip] = (now, current)
    return current


def _moonlight_last_launch_appid() -> Optional[str]:
    """Read the latest Moonlight launch?appid=… from the user journal (cached)."""
    global _MOONLIGHT_JOURNAL_APPID_CACHE
    now = time.monotonic()
    if _MOONLIGHT_JOURNAL_APPID_CACHE and (now - _MOONLIGHT_JOURNAL_APPID_CACHE[0]) < _MOONLIGHT_JOURNAL_TTL:
        return _MOONLIGHT_JOURNAL_APPID_CACHE[1]

    appid: Optional[str] = None
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "_COMM=moonlight",
                "-n",
                "80",
                "--no-pager",
                "-o",
                "cat",
                "--since",
                "2 hours ago",
            ],
            capture_output=True,
            text=True,
            timeout=0.4,
            check=False,
        )
        for line in reversed((result.stdout or "").splitlines()):
            match = re.search(r"launch\?[^\"\s]*appid=(\d+)", line, re.IGNORECASE)
            if match:
                appid = match.group(1)
                break
    except (subprocess.SubprocessError, OSError, ValueError):
        appid = None

    _MOONLIGHT_JOURNAL_APPID_CACHE = (now, appid)
    return appid


def _preferred_moonlight_ips(hosts: list[dict]) -> list[str]:
    """LAN addresses first; skip duplicate / empty values."""
    local: list[str] = []
    remote: list[str] = []
    seen: set[str] = set()

    def _is_lan(ip: str) -> bool:
        if ip.startswith(("10.", "192.168.")):
            return True
        if ip.startswith("172."):
            try:
                second = int(ip.split(".", 2)[1])
            except (IndexError, ValueError):
                return False
            return 16 <= second <= 31
        return False

    for host in hosts:
        for ip in host.get("ips") or []:
            if not ip or ip in seen:
                continue
            seen.add(ip)
            (local if _is_lan(ip) else remote).append(ip)
    return local + remote


def _moonlight_app_name_from_hosts(hosts: list[dict], app_id: str) -> Optional[str]:
    app_id = str(app_id or "").strip()
    if not app_id or app_id == "0":
        return None
    for host in hosts:
        for app in host.get("apps") or []:
            if str(app.get("id") or "") == app_id:
                return str(app.get("name") or "").strip() or None
    return None


def _moonlight_name_from_cmdline(cmdline: list[str]) -> Optional[str]:
    """Parse `moonlight stream <host> <app>` style launches."""
    if not cmdline:
        return None
    lowered = [str(part) for part in cmdline]
    for idx, part in enumerate(lowered):
        if part.lower() == "stream" and idx + 2 < len(lowered):
            app = " ".join(lowered[idx + 2:]).strip().strip('"')
            if app:
                return app
    joined = " ".join(lowered)
    match = re.search(r"\bstream\s+\S+\s+(.+)$", joined, re.IGNORECASE)
    if match:
        app = match.group(1).strip().strip('"')
        return app or None
    return None


def _is_moonlight_process(proc_name: str, cmdline: list[str], exe: str = "") -> bool:
    stem = Path((proc_name or "").lower()).stem
    if stem in MOONLIGHT_PROCESS_NAMES:
        return True
    exe_l = (exe or "").lower()
    if "moonlight" in Path(exe_l).name:
        return True
    # Flatpak / snap wrappers sometimes expose a generic name with moonlight in argv0
    if cmdline:
        joined = " ".join(str(p) for p in cmdline[:3]).lower()
        if re.search(r"(^|[/\s])moonlight(-qt)?(\s|$)", joined):
            return True
    return False


def _proc_has_gamestream_traffic(proc, host_ips: set[str]) -> bool:
    """True when Moonlight holds an active stream — games or Desktop remoto.

    On Linux/Snap, stream UDP often appears as UNCONN without raddr. Treat
    multiple non-mDNS UDP sockets bound to a unicast address as an active session.
    """
    try:
        connections = proc.net_connections(kind="inet")
    except (psutil.Error, PermissionError, OSError):
        return False

    stream_udp_ports = {47998, 47999, 48000, 48010}
    control_ports = {47984, 47989, 47990, 47991}
    unbound_stream_udp = 0

    for conn in connections:
        is_udp = int(getattr(conn, "type", 0)) == 2  # socket.SOCK_DGRAM
        local = conn.laddr
        lip = getattr(local, "ip", None) if local else None
        lport = getattr(local, "port", None) if local else None
        if local is not None and isinstance(local, tuple):
            lip = local[0] if local else lip
            lport = local[1] if len(local) > 1 else lport
        try:
            lport_i = int(lport) if lport is not None else -1
        except (TypeError, ValueError):
            lport_i = -1

        remote = conn.raddr
        rip = None
        rport_i = -1
        if remote:
            rip = getattr(remote, "ip", None)
            rport = getattr(remote, "port", None)
            if rip is None and isinstance(remote, tuple):
                rip = remote[0] if remote else None
                rport = remote[1] if len(remote) > 1 else None
            try:
                rport_i = int(rport) if rport is not None else -1
            except (TypeError, ValueError):
                rport_i = -1

        on_host = bool(host_ips) and bool(rip) and rip in host_ips

        if is_udp and rip and (on_host or rport_i in stream_udp_ports):
            return True

        if (
            conn.status == "ESTABLISHED"
            and on_host
            and (rport_i in GAMESTREAM_PORTS or rport_i in control_ports or 47980 <= rport_i <= 48020)
        ):
            return True

        # Snap Moonlight: video/audio sockets bound to LAN IP, no raddr visible
        if is_udp and lport_i not in (-1, 5353) and lip and lip not in ("0.0.0.0", "::", ""):
            unbound_stream_udp += 1

    return unbound_stream_udp >= 2


def _format_moonlight_session_name(app_name: str, hostname: str = "") -> str:
    """Human label for Moonlight sessions, including remote Desktop."""
    raw = (app_name or "").strip() or "Moonlight"
    host = (hostname or "").strip()
    lowered = raw.lower()

    desktop_aliases = {
        "desktop",
        "área de trabalho",
        "area de trabalho",
        "remote desktop",
        "desktop remoto",
    }
    if lowered in desktop_aliases:
        label = "Desktop remoto"
    elif "big picture" in lowered:
        label = "Steam Big Picture"
    else:
        label = raw

    if host and host.lower() not in label.lower():
        return f"{label} · {host}"
    return label


def _host_game_helper_base_urls(hosts: list[dict]) -> list[str]:
    """Resolve optional HTTP helper URLs (fallback; primary path is UDP broadcast)."""
    configured = ""
    try:
        from library.config import CONFIG_DATA

        configured = str((CONFIG_DATA.get("config") or {}).get("HOST_GAME_HELPER_URL") or "").strip()
    except Exception:
        configured = ""

    if configured and configured.upper() not in {"AUTO", "UDP", ""}:
        return [configured.rstrip("/")]

    urls: list[str] = []
    for ip in _preferred_moonlight_ips(hosts):
        urls.append(f"http://{ip}:{_HOST_GAME_HELPER_DEFAULT_PORT}")
    return urls


def _host_game_udp_payload_fresh(max_age: float = 5.0) -> Optional[dict]:
    with _HOST_GAME_UDP_LOCK:
        if not _HOST_GAME_UDP_CACHE:
            return None
        age = time.monotonic() - _HOST_GAME_UDP_CACHE_MONO
        if age > max_age:
            return None
        return dict(_HOST_GAME_UDP_CACHE)


def _accept_host_game_udp_payload(data: dict, source_ip: str = "") -> None:
    global _HOST_GAME_UDP_CACHE, _HOST_GAME_UDP_CACHE_MONO, _HOST_GAME_HELPER_CACHE
    if not isinstance(data, dict):
        return
    service = str(data.get("service") or "")
    if service and service != _HOST_GAME_HELPER_SERVICE:
        return
    # Require at least a title or exe so empty keepalives do not clear state
    if not (data.get("title") or data.get("exe")):
        return
    payload = {
        "title": str(data.get("title") or ""),
        "exe": str(data.get("exe") or ""),
        "pid": int(data.get("pid") or 0),
        "updated_at": float(data.get("updated_at") or time.time()),
        "source_ip": source_ip,
    }
    with _HOST_GAME_UDP_LOCK:
        _HOST_GAME_UDP_CACHE = payload
        _HOST_GAME_UDP_CACHE_MONO = time.monotonic()
    # Keep HTTP-style cache warm so resolve path stays sync/fast
    _HOST_GAME_HELPER_CACHE = (time.monotonic(), payload)


def _host_game_udp_loop(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", port))
    except OSError as exc:
        logger.debug("Host game UDP listener bind failed on %s: %s", port, exc)
        sock.close()
        return

    try:
        mreq = struct.pack("=4s4s", socket.inet_aton(_HOST_GAME_HELPER_MULTICAST), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        logger.debug("Host game multicast join failed: %s", exc)

    sock.settimeout(1.0)
    logger.info(
        "Listening for host game helper on UDP :%s (multicast %s)",
        port,
        _HOST_GAME_HELPER_MULTICAST,
    )
    while True:
        try:
            raw, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            data = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            continue
        _accept_host_game_udp_payload(data, source_ip=addr[0] if addr else "")


def _ensure_host_game_udp_listener() -> None:
    global _HOST_GAME_UDP_THREAD
    if _HOST_GAME_UDP_THREAD and _HOST_GAME_UDP_THREAD.is_alive():
        return
    thread = threading.Thread(
        target=_host_game_udp_loop,
        args=(_HOST_GAME_HELPER_DEFAULT_PORT,),
        name="host-game-udp",
        daemon=True,
    )
    _HOST_GAME_UDP_THREAD = thread
    thread.start()


def _query_host_game_helper(hosts: list[dict]) -> Optional[dict]:
    """Foreground window from Windows helper — UDP broadcast first, HTTP fallback."""
    global _HOST_GAME_HELPER_CACHE
    _ensure_host_game_udp_listener()

    udp_payload = _host_game_udp_payload_fresh(max_age=5.0)
    if udp_payload:
        return udp_payload

    now = time.monotonic()
    if _HOST_GAME_HELPER_CACHE is not None:
        age = now - _HOST_GAME_HELPER_CACHE[0]
        payload = _HOST_GAME_HELPER_CACHE[1]
        ttl = _HOST_GAME_HELPER_TTL if payload is not None else _HOST_GAME_HELPER_FAIL_TTL
        if age < ttl:
            return payload

    payload: Optional[dict] = None
    for base in _host_game_helper_base_urls(hosts)[:2]:
        url = f"{base.rstrip('/')}/foreground"
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=0.15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            if isinstance(data, dict) and (data.get("title") or data.get("exe")):
                payload = {
                    "title": str(data.get("title") or ""),
                    "exe": str(data.get("exe") or ""),
                    "pid": int(data.get("pid") or 0),
                    "updated_at": float(data.get("updated_at") or time.time()),
                }
                break
        except Exception:
            continue

    _HOST_GAME_HELPER_CACHE = (now, payload)
    return payload


def _clean_foreground_title(title: str, exe: str = "") -> str:
    """Normalize a Windows foreground title into a usable game label."""
    raw = " ".join((title or "").replace("\xa0", " ").split()).strip()
    if not raw:
        stem = Path((exe or "").replace("\\", "/")).stem
        raw = stem.replace("_", " ").replace("-", " ").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered in _FOREGROUND_TITLE_IGNORE:
        return ""
    if lowered.startswith("desktop remoto") or lowered == "desktop":
        return ""

    for sep in (" — ", " – ", " - ", " | "):
        if sep in raw:
            left = raw.split(sep, 1)[0].strip()
            if len(left) >= 3 and left.lower() not in _FOREGROUND_TITLE_IGNORE:
                raw = left
                break

    raw = re.sub(r"\s+\(\d+\s*[\-–—]?\s*bit\)$", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"\s+\[.*?\]$", "", raw).strip()
    if raw.lower() in _FOREGROUND_TITLE_IGNORE:
        return ""
    return raw


_BORING_FOREGROUND_STEMS = {
    "cmd",
    "powershell",
    "pwsh",
    "windowsterminal",
    "conhost",
    "openconsole",
    "python",
    "pythonw",
    "explorer",
    "sunshine",
    "sunshinesvc",
}


def _match_game_name_from_text(text: str) -> tuple[str, str]:
    """Match a free-text label against known games / Steam names.

    Returns (display_name, steam_appid); ("", "") when no real game matches.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return "", ""
    for key, name in KNOWN_GAMES.items():
        if len(key) < 4:
            continue
        if key in needle or name.lower() == needle or name.lower() in needle:
            return name, KNOWN_GAME_APPIDS.get(key, "")
    for appid, name in STEAM_APP_NAMES.items():
        if name.lower() == needle or name.lower() in needle:
            return name, appid
    for appid, name in _load_steam_name_cache().items():
        if str(name).lower() == needle or str(name).lower() in needle:
            return str(name), str(appid)
    return "", ""


# Exe stems the Windows helper reports when a *non-game* window has focus.
# The old helper (no is_game flag) falls back to a running known game when a
# shell has focus — in that case the payload carries the *game* exe. When it
# reports one of these exes, no game is focused/running, so the announcement
# is filtered out locally regardless of the title text.
_NON_GAME_HELPER_EXES = {
    # shells / terminals
    "cmd",
    "powershell",
    "pwsh",
    "conhost",
    "openconsole",
    "windowsterminal",
    "wt",
    # Windows shell / UI
    "explorer",
    "searchhost",
    "searchui",
    "shellexperiencehost",
    "textinputhost",
    "applicationframehost",
    "startmenuexperiencehost",
    "taskhostw",
    "sihost",
    "dwm",
    "winlogon",
    "ctfmon",
    "fontdrvhost",
    "runtimebroker",
    "svchost",
    # browsers
    "chrome",
    "msedge",
    "firefox",
    "brave",
    "opera",
    # office / editors
    "excel",
    "winword",
    "powerpnt",
    "outlook",
    "wordpad",
    "notepad",
    "mspaint",
    "code",
    "idea64",
    "pycharm64",
    "rider64",
    # media / chat
    "vlc",
    "spotify",
    "discord",
    "teams",
    # streaming hosts / this helper
    "sunshine",
    "sunshinesvc",
    "foreground_reporter",
    # python (helper / tools)
    "python",
    "pythonw",
    "py",
}


def _classify_helper_payload(payload: dict) -> tuple[str, str]:
    """Filter one Windows-helper announcement against the local game list.

    The Mini-PC is the authority for "is this a game": the helper may be an
    old version (no is_game flag, announcing any focused window) or a new
    one — either way only recognized games pass through.
    """
    title = str(payload.get("title") or "")
    exe = str(payload.get("exe") or "")
    stem = Path(exe.replace("\\", "/")).stem.lower()
    if stem in _NON_GAME_HELPER_EXES:
        return "", ""
    return _match_game_from_foreground(title, exe)


def _match_game_from_foreground(title: str, exe: str = "") -> tuple[str, str]:
    """Map helper title/exe to (display_name, steam_appid).

    Strict: only a *recognized* game yields a hit. Unmatched foreground
    windows (browser, office, file manager, ...) return ("", "") so a
    Desktop stream alone never looks like a game.
    """
    stem = Path((exe or "").replace("\\", "/")).stem.lower()
    cleaned = _clean_foreground_title(title, exe)
    if not cleaned:
        return "", ""
    if stem in _BORING_FOREGROUND_STEMS and cleaned.lower() in _FOREGROUND_TITLE_IGNORE:
        return "", ""
    if stem in _BORING_FOREGROUND_STEMS and not any(
        key in cleaned.lower() for key in KNOWN_GAMES if len(key) >= 4
    ):
        # Ignore shell titles unless they somehow include a game name.
        if cleaned.lower() in _FOREGROUND_TITLE_IGNORE or stem in cleaned.lower():
            return "", ""

    if stem in KNOWN_GAMES:
        return KNOWN_GAMES[stem], KNOWN_GAME_APPIDS.get(stem, "")
    if stem in KNOWN_GAME_APPIDS:
        appid = KNOWN_GAME_APPIDS[stem]
        return resolve_steam_app_name(appid), appid

    return _match_game_name_from_text(cleaned)


def _resolve_moonlight_display_name(proc, hosts: list[dict], cmdline: list[str]) -> str:
    """Best-effort streamed app title for the GAMER overlay (game or Desktop)."""
    return _resolve_moonlight_game_info(proc, hosts, cmdline)["display_name"]


def _resolve_moonlight_game_info(proc, hosts: list[dict], cmdline: list[str]) -> dict:
    """Resolve the streamed app (host helper first) and whether it is a real game.

    The GAMER mode may only be entered when ``is_game`` is True — a Desktop
    remoto stream without a detected game must not flip the screen.
    """
    hostname = (hosts[0].get("hostname") if hosts else "") or ""

    helper = _query_host_game_helper(hosts)
    if helper:
        # Local list is the authority so both helper versions behave the
        # same: the old helper announces ANY focused window (no is_game),
        # the new one may send is_game — either way only a recognized game
        # passes. Non-game exes (browser, office, shell) are filtered here.
        display, appid = _classify_helper_payload(helper)
        if display:
            return {"display_name": display, "appid": appid, "is_game": True}
        # No recognized game focused — the stream is Desktop-only right
        # now; do not surface a stale game name from a previous poll.
        return {"display_name": "", "appid": "", "is_game": False}

    cmdline_name = _moonlight_name_from_cmdline(cmdline)
    if cmdline_name:
        name = _format_moonlight_session_name(cmdline_name, hostname)
        matched_name, _ = _match_game_name_from_text(cmdline_name)
        is_game = bool(matched_name)
        return {
            "display_name": name if is_game else "",
            "appid": _appid_for_process("moonlight", cmdline_name) if is_game else "",
            "is_game": is_game,
        }

    app_id = None
    for ip in _preferred_moonlight_ips(hosts)[:1]:
        app_id = _query_sunshine_current_game(ip)
        if app_id and app_id != "0":
            break
        app_id = None
    if not app_id:
        app_id = _moonlight_last_launch_appid()

    if app_id and app_id != "0":
        mapped = _moonlight_app_name_from_hosts(hosts, app_id)
        if mapped:
            name = _format_moonlight_session_name(mapped, hostname)
            return {
                "display_name": name,
                "appid": _appid_for_process("moonlight", mapped),
                "is_game": True,
            }
        # "App <id>" is an unlabelled Sunshine entry (often the Desktop app).
        return {"display_name": "", "appid": "", "is_game": False}

    # Generic "Moonlight" / "Desktop remoto" — no game detected.
    return {"display_name": "", "appid": "", "is_game": False}


def _appid_for_process(proc_name: str, display_name: str = "") -> str:
    stem = Path((proc_name or "").lower()).stem
    if stem in KNOWN_GAME_APPIDS:
        return KNOWN_GAME_APPIDS[stem]
    needle = (display_name or "").strip().lower()
    if needle:
        for appid, name in STEAM_APP_NAMES.items():
            if name.lower() == needle:
                return appid
        cache = _load_steam_name_cache()
        for appid, name in cache.items():
            if str(name).lower() == needle:
                return str(appid)
    return ""


def _find_local_steam_art(appid: str, names: tuple[str, ...]) -> Optional[Path]:
    """Search Steam librarycache for artwork files."""
    for root in _steam_roots():
        base = root / "appcache" / "librarycache" / str(appid)
        if not base.is_dir():
            continue
        # Prefer direct files, then hashed subdirs (newest first)
        for name in names:
            direct = base / name
            if direct.is_file():
                return direct
        subdirs = sorted(
            (p for p in base.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for sub in subdirs:
            for name in names:
                path = sub / name
                if path.is_file():
                    return path
        # Loose hashed jpgs sometimes used as capsules — only if large enough
        if "library_capsule.jpg" in names:
            jpgs = sorted(
                (p for p in base.glob("*.jpg") if p.stat().st_size > 20_000),
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            if jpgs:
                return jpgs[0]
    return None


def _download_steam_art(appid: str, filename: str, url: str) -> Optional[Path]:
    dest_dir = GAME_ART_CACHE_DIR / str(appid)
    dest = dest_dir / filename
    if dest.is_file() and dest.stat().st_size > 1024:
        return dest
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "turing-clock/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = resp.read()
        if len(data) < 1024:
            return None
        dest.write_bytes(data)
        return dest
    except (OSError, ValueError) as exc:
        logger.debug("Steam art download failed (%s %s): %s", appid, filename, exc)
        return None


def _load_image(path: Optional[Path]):
    if path is None or not path.is_file():
        return None
    try:
        from PIL import Image
        return Image.open(path).convert("RGBA")
    except OSError as exc:
        logger.debug("Failed to open game art %s: %s", path, exc)
        return None


def _cover_fit(image, size: tuple[int, int]):
    from PIL import Image
    tw, th = size
    iw, ih = image.size
    scale = max(tw / iw, th / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = image.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - tw) // 2)
    top = max(0, (nh - th) // 2)
    return resized.crop((left, top, left + tw, top + th))


def _extract_theme_colors(hero_image) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    """Pick vivid accents + a dark panel tone from hero art."""
    from PIL import Image

    sample = hero_image.convert("RGB").resize((64, 36), Image.Resampling.BOX)
    quantized = sample.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    colors = [tuple(palette[i : i + 3]) for i in range(0, min(24, len(palette)), 3)]

    def sat_lum(c):
        r, g, b = c
        mx, mn = max(c), min(c)
        sat = mx - mn
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return sat, lum

    accents = []
    darks = []
    for c in colors:
        sat, lum = sat_lum(c)
        if lum < 55:
            darks.append(c)
        if sat > 28 and 35 < lum < 220:
            accents.append((sat * 1.4 + (40 if 60 < lum < 180 else 0), c))

    accents.sort(reverse=True, key=lambda item: item[0])
    accent = accents[0][1] if accents else (255, 160, 64)
    accent2 = accents[1][1] if len(accents) > 1 else (78, 132, 255)
    if darks:
        r, g, b = darks[0]
        panel = (max(6, r // 3), max(10, g // 3), max(14, b // 3))
    else:
        panel = (11, 20, 34)
    return accent, accent2, panel


_MEDIA_THEME_CACHE: dict[str, tuple] = {}


def _media_theme_from_cover(cover, cache_key: str = ""):
    """Cache accent colors extracted from album art."""
    if cache_key and cache_key in _MEDIA_THEME_CACHE:
        return _MEDIA_THEME_CACHE[cache_key]
    accent, accent2, panel = _extract_theme_colors(cover)
    if cache_key:
        _MEDIA_THEME_CACHE[cache_key] = (accent, accent2, panel)
    return accent, accent2, panel


def load_game_theme(appid: str) -> dict:
    """
    Load Steam artwork + accent colors for a game.
    Prefer local Steam librarycache, then CDN download into ~/.cache/turing-clock/game-art.
    """
    appid = str(appid or "").strip()
    if not appid:
        return {}
    if appid in _GAME_THEME_CACHE:
        return _GAME_THEME_CACHE[appid]

    hero_names = ("library_hero.jpg", "library_hero_blur.jpg", "library_header.jpg")
    logo_names = ("logo.png", "logo.jpg")
    capsule_names = ("library_capsule.jpg", "library_600x900.jpg", "portrait.png")

    hero_path = _find_local_steam_art(appid, hero_names)
    logo_path = _find_local_steam_art(appid, logo_names)
    capsule_path = _find_local_steam_art(appid, capsule_names)

    cdn = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
    if hero_path is None:
        hero_path = (
            _download_steam_art(appid, "library_hero.jpg", f"{cdn}/library_hero.jpg")
            or _download_steam_art(appid, "header.jpg", f"{cdn}/header.jpg")
        )
    if logo_path is None:
        logo_path = _download_steam_art(appid, "logo.png", f"{cdn}/logo.png")
    if capsule_path is None:
        capsule_path = (
            _download_steam_art(appid, "library_600x900.jpg", f"{cdn}/library_600x900.jpg")
            or _download_steam_art(appid, "library_capsule.jpg", f"{cdn}/library_capsule.jpg")
            or _download_steam_art(appid, "capsule_616x353.jpg", f"{cdn}/capsule_616x353.jpg")
        )

    hero = _load_image(hero_path)
    logo = _load_image(logo_path)
    capsule = _load_image(capsule_path)
    if hero is None and capsule is not None:
        hero = capsule

    accent, accent2, panel = ((255, 160, 64), (78, 132, 255), (11, 20, 34))
    if hero is not None:
        accent, accent2, panel = _extract_theme_colors(hero)

    theme = {
        "appid": appid,
        "hero": hero,
        "logo": logo,
        "capsule": capsule,
        "hero_path": str(hero_path) if hero_path else "",
        "accent": accent,
        "accent2": accent2,
        "panel": panel,
    }
    if hero is not None or logo is not None or capsule is not None:
        _GAME_THEME_CACHE[appid] = theme
        logger.info(
            "Game theme loaded for appid=%s (hero=%s logo=%s capsule=%s accent=%s)",
            appid,
            bool(hero),
            bool(logo),
            bool(capsule),
            accent,
        )
    return theme


def apply_game_theme(info: GamerInfo, appid: str = ""):
    """Attach Steam artwork / accents onto a GamerInfo instance."""
    appid = str(appid or info.steam_appid or "").strip()
    if not appid:
        appid = _appid_for_process(info.process_name, info.game_name)
    if not appid:
        return info
    info.steam_appid = appid
    theme = load_game_theme(appid)
    if not theme:
        return info
    info.game_art_image = theme.get("hero")
    info.game_logo_image = theme.get("logo")
    info.game_capsule_image = theme.get("capsule")
    info.game_art_path = theme.get("hero_path") or ""
    info.theme_accent = theme.get("accent") or info.theme_accent
    info.theme_accent2 = theme.get("accent2") or info.theme_accent2
    info.theme_panel = theme.get("panel") or info.theme_panel
    return info


# ---------------------------------------------------------------------------
# System volume (PipeWire / PulseAudio)
# ---------------------------------------------------------------------------

def get_system_volume() -> tuple[float, bool]:
    """Return (volume 0.0–1.0, muted) for the default audio sink."""
    # Prefer wpctl (PipeWire on Pop!_OS / COSMIC)
    try:
        result = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip()
            muted = "[MUTED]" in line.upper()
            match = re.search(r"Volume:\s*([0-9.]+)", line)
            if match:
                return max(0.0, min(1.0, float(match.group(1)))), muted
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    # Fallback: pactl
    try:
        vol = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        mute = subprocess.run(
            ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        muted = "yes" in (mute.stdout or "").lower() or "sim" in (mute.stdout or "").lower()
        match = re.search(r"(\d+)%", vol.stdout or "")
        if match:
            return max(0.0, min(1.0, int(match.group(1)) / 100.0)), muted
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return 0.0, False


# ---------------------------------------------------------------------------
# Multimedia detection (MPRIS2)
# ---------------------------------------------------------------------------

class MultimediaDetector:
    """Detects and retrieves media information via MPRIS2 D-Bus interface."""

    MPRIS_BASE = "org.mpris.MediaPlayer2"
    DBUS_PROPS = "org.freedesktop.DBus.Properties"

    def __init__(self):
        self._last_info = MultimediaInfo()
        self._player_names: dict[str, str] = {}  # bus_name -> player_id
        self._last_time = time.time()
        self._local_position = 0.0

    def detect(self) -> MultimediaInfo:
        """Query all MPRIS2 players and return the playing one."""
        now = time.time()
        dt = now - self._last_time
        self._last_time = now

        try:
            player_names = self._get_mpris_players()
        except Exception as exc:
            logger.debug("Failed to query MPRIS2 players: %s", exc)
            vol, muted = get_system_volume()
            self._last_info.volume = vol
            self._last_info.muted = muted
            if self._last_info.is_playing:
                self._local_position = min(
                    self._last_info.length,
                    self._local_position + dt
                )
                self._last_info.position = self._local_position
            return self._last_info

        # Prioritize common real players over utility players (playerctld, etc.)
        priority = ["spotify", "firefox", "chromium", "google-chrome",
                    "mpv", "vlc", "rhythmbox", "celtix", "deadbeef",
                    "clementine", "bombad", "audacious", "quodlibet",
                    "strawberry", "tomahawk", "podcast"]

        # Sort players: priority players first, then others
        sorted_players = sorted(player_names.items(),
                                key=lambda x: (x[1] not in priority,
                                               x[1] in priority and x[1] not in priority))

        # Check each player
        found_info = None
        for bus_name, player_id in sorted_players:
            info = self._get_player_info(bus_name, player_id)
            if info and (info.is_playing or info.title):
                info.player_id = player_id
                vol, muted = get_system_volume()
                info.volume = vol
                info.muted = muted
                found_info = info
                break

        if found_info:
            # If same track, estimate position locally if D-Bus reports 0
            if (
                found_info.title == self._last_info.title
                and found_info.artist == self._last_info.artist
            ):
                if found_info.position > 0:
                    self._local_position = found_info.position
                elif found_info.is_playing:
                    self._local_position = min(
                        found_info.length or 3600.0,
                        self._local_position + dt
                    )
                    found_info.position = self._local_position
                logger.debug("Multimedia progress: title=%s pos=%.1fs len=%.1fs dt=%.3fs", 
                             found_info.title, found_info.position, found_info.length, dt)
            else:
                self._local_position = found_info.position

            self._last_info = found_info
            return found_info

        # No playing media, but keep last info as paused
        self._last_info.is_playing = False
        vol, muted = get_system_volume()
        self._last_info.volume = vol
        self._last_info.muted = muted
        self._last_info.position = self._local_position
        return self._last_info

    def _get_mpris_players(self) -> dict[str, str]:
        """Get list of active MPRIS2 player bus names."""
        try:
            result = subprocess.run(
                [
                    "busctl", "--user", "list",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return {}

            names = {}
            prefix = f"{self.MPRIS_BASE}."
            for line in result.stdout.splitlines():
                # busctl output format: NAME PID PROCESS USER CONNECTION...
                # MPRIS name is the FIRST column
                parts = line.split()
                if len(parts) >= 1 and parts[0].startswith(prefix):
                    bus_name = parts[0]
                    # Extract player name (e.g., "spotify" from "org.mpris.MediaPlayer2.spotify")
                    # or "firefox" from "org.mpris.MediaPlayer2.firefox.instance_1_183"
                    match = re.search(rf'{prefix}(\w+)', bus_name)
                    if match:
                        player_id = match.group(1)
                    else:
                        player_id = bus_name.split(".")[-1]
                    names[bus_name] = player_id
            return names
        except (subprocess.SubprocessError, OSError):
            return {}

    def _get_player_info(self, bus_name: str, player_id: str) -> Optional[MultimediaInfo]:
        """Get current track info from a MPRIS2 player."""
        if player_id in ("playerctld",):
            return None

        info = MultimediaInfo()
        info.app_name = player_id
        info.player_id = player_id
        object_path = "/org/mpris/MediaPlayer2"
        iface = "org.mpris.MediaPlayer2.Player"

        try:
            metadata = self._dbus_get_property(bus_name, object_path, iface, "Metadata")
            playback_status = self._dbus_get_property(bus_name, object_path, iface, "PlaybackStatus")
            position = self._dbus_get_property(bus_name, object_path, iface, "Position")

            status = self._as_string(playback_status)
            if status == "Playing":
                info.is_playing = True
            elif status in ("Paused", "Stopped"):
                info.is_playing = False
            elif not metadata:
                return None

            if metadata:
                info.title = self._extract_metadata(metadata, "xesam:title") or ""
                info.artist = self._extract_metadata(metadata, "xesam:artist") or ""
                info.album = self._extract_metadata(metadata, "xesam:album") or ""
                info.cover_url = self._extract_metadata(metadata, "mpris:artUrl") or ""
                length_us = self._extract_metadata_int(metadata, "mpris:length")
                if length_us:
                    info.length = length_us / 1_000_000
                if isinstance(info.artist, list):
                    info.artist = " / ".join(str(a) for a in info.artist)

            # Fallback: if no mpris:artUrl, try to extract YouTube thumbnail
            # from xesam:url (YouTube does not provide mpris:artUrl)
            if not info.cover_url:
                yt_url = self._extract_metadata(metadata, "xesam:url") or ""
                if yt_url:
                    yt_url = yt_url.strip()
                    info.cover_url = self._resolve_youtube_thumbnail(yt_url)

            # Fallback: if no mpris:length, try to extract YouTube duration
            # from xesam:url via YouTube HTML scraping (YouTube does not provide mpris:length)
            if not info.length:
                yt_url = self._extract_metadata(metadata, "xesam:url") or ""
                if yt_url:
                    yt_url = yt_url.strip()
                    info.length = self._resolve_youtube_duration(yt_url) or 0.0
                    logger.debug("YouTube duration resolved: url=%s length=%.1fs", yt_url, info.length)

            if position is not None:
                try:
                    info.position = float(self._as_int(position)) / 1_000_000
                except (TypeError, ValueError):
                    pass

            if not info.title and not info.artist:
                return None
        except Exception as exc:
            logger.debug("Error reading MPRIS2 properties: %s", exc)
            return None

        if info.cover_url:
            info.cover_image = self._load_cover_art(info.cover_url)
        return info

    def _dbus_get_property(self, bus_name: str, object_path: str,
                           interface: str, property_name: str):
        """Get a raw D-Bus property string from busctl."""
        try:
            result = subprocess.run(
                [
                    "busctl", "--user", "get-property",
                    bus_name, object_path, interface, property_name,
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _as_string(value) -> str:
        if value is None:
            return ""
        value = str(value).strip()
        match = re.match(r'^s\s+"(.*)"$', value)
        if match:
            return match.group(1)
        match = re.match(r"^s\s+'(.*)'$", value)
        if match:
            return match.group(1)
        return value.strip('"\'')

    @staticmethod
    def _as_int(value) -> int:
        if value is None:
            return 0
        value = str(value).strip()
        match = re.match(r'^[txui]\s+(-?\d+)$', value)
        if match:
            return int(match.group(1))
        return int(value)

    @staticmethod
    def _decode_dbus_text(text: str) -> str:
        """Decode busctl C-style octal escapes (e.g. \\303\\241 -> á)."""
        if not text or "\\" not in text:
            return text

        def repl(match):
            chunks = re.findall(r"\\([0-7]{3})", match.group(0))
            try:
                return bytes(int(c, 8) for c in chunks).decode("utf-8")
            except Exception:
                return match.group(0)

        return re.sub(r"(?:\\[0-7]{3})+", repl, text)

    def _extract_metadata(self, metadata, key: str):
        """Extract a metadata field from D-Bus metadata structure."""
        if key.endswith(":artist"):
            match = re.search(rf'{key}"\s+as\s+\d+\s+"([^"]*)"', metadata)
            if match:
                return self._decode_dbus_text(match.group(1))
            match = re.search(rf'{key}"\s+as\s+\[(.*?)\]', metadata, re.DOTALL)
            if match:
                items = [self._decode_dbus_text(i) for i in re.findall(r'"([^"]*)"', match.group(1))]
                return " / ".join(items) if items else None

        match = re.search(rf'{key}"\s+s\s+"([^"]*)"', metadata)
        if match:
            return self._decode_dbus_text(match.group(1))
        match = re.search(rf'{key}"\s+t\s+"([^"]*)"', metadata)
        if match:
            return self._decode_dbus_text(match.group(1))
        return None

    def _extract_metadata_int(self, metadata, key: str) -> Optional[int]:
        match = re.search(rf'{key}"\s+[txui]\s+(\d+)', metadata)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _resolve_youtube_thumbnail(url: str) -> str:
        """Extract a YouTube video ID from URL and return thumbnail URL."""
        if not url:
            return ""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            if parsed.hostname and "youtube" in parsed.hostname:
                video_id = parse_qs(parsed.query).get("v")
                if video_id:
                    return f"https://img.youtube.com/vi/{video_id[0]}/maxresdefault.jpg"
            # Also handle youtube.com/shorts/VIDEO_ID
            path = parsed.path.strip("/")
            if parsed.hostname and "youtube" in parsed.hostname and path.startswith("shorts/"):
                video_id = path.split("/")[0]
                return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        except Exception:
            pass
        return ""

    @staticmethod
    def _resolve_youtube_duration(url: str) -> Optional[float]:
        """Resolve YouTube video duration in seconds by scraping video page."""
        if not url:
            return None
        try:
            from urllib.request import Request, urlopen
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            video_id = None
            if parsed.hostname and "youtube" in parsed.hostname:
                video_id = parse_qs(parsed.query).get("v")
            if not video_id and parsed.hostname and "youtube" in parsed.hostname:
                path = parsed.path.strip("/")
                if path.startswith("shorts/"):
                    video_id = path.split("/")[0]
            if not video_id:
                return None
            video_id = video_id[0]
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            req = Request(video_url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
            })
            with urlopen(req, timeout=5) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            # Extract lengthSeconds from embedded JSON metadata
            match = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', html)
            if match:
                return float(match.group(1))
        except Exception:
            pass
        return None

    def _load_cover_art(self, url: str) -> Optional[object]:
        """Load cover art from file:// or https:// URL."""
        try:
            from io import BytesIO
            from urllib.request import Request, urlopen
            from PIL import Image

            if url in getattr(self, "_cover_cache", {}):
                cached = self._cover_cache[url]
                return cached.copy() if cached is not None else None

            if not hasattr(self, "_cover_cache"):
                self._cover_cache = {}

            image = None
            if url.startswith("file://"):
                path = Path(url.replace("file://", ""))
                if path.is_file():
                    image = Image.open(path).convert("RGBA")
            elif url.startswith("http://") or url.startswith("https://"):
                req = Request(url, headers={"User-Agent": "turing-clock/1.0"})
                with urlopen(req, timeout=3) as resp:
                    image = Image.open(BytesIO(resp.read())).convert("RGBA")

            if image is not None:
                image = image.resize((180, 180), Image.Resampling.LANCZOS)
            self._cover_cache[url] = image.copy() if image is not None else None
            return image.copy() if image is not None else None
        except Exception as exc:
            logger.debug("Failed to load cover art from %s: %s", url, exc)
            if hasattr(self, "_cover_cache"):
                self._cover_cache[url] = None
            return None


# ---------------------------------------------------------------------------
# Gamer detection
# ---------------------------------------------------------------------------

class GamerDetector:
    """Detects running games and gathers gaming metrics."""

    def __init__(self):
        self._last_info = GamerInfo()
        self._last_game_process: Optional[str] = None
        self._session_key: str = ""
        self._session_started_at: Optional[datetime] = None
        self._last_seen_mono: float = 0.0
        _ensure_host_game_udp_listener()

    def detect(self) -> GamerInfo:
        """Check if a game is running and gather metrics."""
        game_proc = self._detect_game()

        if game_proc:
            self._last_game_process = game_proc["process"]
            self._last_seen_mono = time.monotonic()
            info = GamerInfo()
            info.game_name = game_proc["display_name"]
            info.process_name = game_proc["process"]
            info.process_pid = game_proc["pid"]
            info.steam_appid = str(game_proc.get("appid") or "")

            session_key = f"{info.steam_appid}:{info.game_name}"
            proc_started = self._process_start_time(info.process_pid)
            # Moonlight / Steam Remote Play stay open for hours in the tray.
            # Playtime must count the *detected session*, not process uptime.
            stream_client = self._is_stream_client_process(info.process_name)
            if session_key != self._session_key or self._session_started_at is None:
                self._session_key = session_key
                if stream_client:
                    self._session_started_at = datetime.now().astimezone()
                else:
                    self._session_started_at = proc_started or datetime.now().astimezone()
            elif (
                not stream_client
                and proc_started
                and proc_started < self._session_started_at
            ):
                # Local games: prefer earlier process start (reaper before client).
                self._session_started_at = proc_started

            info.detected_at = self._session_started_at
            elapsed_sec = max(0.0, (datetime.now().astimezone() - self._session_started_at).total_seconds())
            info.elapsed = format_playtime(elapsed_sec)

            # Reuse theme images if same game (avoid reloading every poll)
            if (
                self._last_info.steam_appid
                and self._last_info.steam_appid == info.steam_appid
                and self._last_info.game_art_image is not None
            ):
                info.game_art_image = self._last_info.game_art_image
                info.game_logo_image = self._last_info.game_logo_image
                info.game_capsule_image = self._last_info.game_capsule_image
                info.game_art_path = self._last_info.game_art_path
                info.theme_accent = self._last_info.theme_accent
                info.theme_accent2 = self._last_info.theme_accent2
                info.theme_panel = self._last_info.theme_panel
            else:
                apply_game_theme(info, info.steam_appid)
            self._gather_gamer_metrics(info)
            self._last_info = info
            return info

        # No game detected — keep session briefly, then clear
        if not self._last_info.game_name or (time.monotonic() - self._last_seen_mono) > 30:
            self._last_info = GamerInfo()
            self._last_game_process = None
            self._session_key = ""
            self._session_started_at = None
            return self._last_info

        if self._session_started_at:
            elapsed_sec = max(0.0, (datetime.now().astimezone() - self._session_started_at).total_seconds())
            self._last_info.elapsed = format_playtime(elapsed_sec)
            self._gather_gamer_metrics(self._last_info)
        return self._last_info

    @staticmethod
    def _process_start_time(pid: Optional[int]) -> Optional[datetime]:
        if not pid:
            return None
        try:
            return datetime.fromtimestamp(psutil.Process(pid).create_time()).astimezone()
        except (psutil.Error, ValueError, OSError):
            return None

    @staticmethod
    def _is_stream_client_process(process_name: str) -> bool:
        stem = Path((process_name or "").lower()).stem
        if stem in MOONLIGHT_PROCESS_NAMES or "moonlight" in stem:
            return True
        return stem in {"streaming_client", "reaper"}

    def refresh_metrics(self, info: GamerInfo) -> GamerInfo:
        """Update CPU/GPU usage and temperatures on an existing GamerInfo."""
        self._gather_gamer_metrics(info)
        return info

    def _detect_game(self) -> Optional[dict]:
        """Detect a running local game, Steam Remote Play, or Moonlight stream."""
        # Cheap attrs first — cmdline/exe are expensive across all processes.
        try:
            procs = list(psutil.process_iter(["name", "pid", "ppid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        stream_hit = None
        known_hit = None
        moonlight_hit = None
        moonlight_hosts = None
        moonlight_ips = None

        for proc in procs:
            proc_name = proc.info.get("name") or ""
            proc_lower = proc_name.lower()
            pid = proc.info.get("pid")
            ppid = proc.info.get("ppid")

            if not proc_lower or pid < 200 or ppid in (0, 2):
                continue
            if proc_lower in NON_GAME_PROCESSES or Path(proc_lower).stem in NON_GAME_PROCESSES:
                continue

            stem = Path(proc_lower).stem

            # Moonlight — resolve cmdline/exe/sockets only for matching processes
            if stem in MOONLIGHT_PROCESS_NAMES or "moonlight" in stem:
                try:
                    cmdline = proc.cmdline() or []
                    exe = proc.exe() or ""
                except (psutil.Error, PermissionError, OSError):
                    cmdline, exe = [], ""
                if not _is_moonlight_process(proc_name, cmdline, exe):
                    continue
                if moonlight_hosts is None:
                    moonlight_hosts = _moonlight_hosts()
                    moonlight_ips = {
                        ip
                        for host in moonlight_hosts
                        for ip in (host.get("ips") or [])
                        if ip
                    }
                if _proc_has_gamestream_traffic(proc, moonlight_ips or set()):
                    game_info = _resolve_moonlight_game_info(
                        proc, moonlight_hosts or [], cmdline
                    )
                    # Only a real game on the host flips the screen — a
                    # Desktop remoto stream without a game must not enter GAMER.
                    if game_info.get("is_game") and game_info.get("display_name"):
                        moonlight_hit = {
                            "process": proc_name,
                            "display_name": game_info["display_name"],
                            "pid": pid,
                            "appid": game_info.get("appid")
                            or _appid_for_process(proc_name, game_info["display_name"]),
                        }
                continue

            # Steam Remote Play — need cmdline only for these names
            if stem in {"streaming_client", "reaper"}:
                try:
                    cmdline = proc.cmdline() or []
                except (psutil.Error, PermissionError, OSError):
                    cmdline = []
                if stem == "reaper" and "streaming_client" not in " ".join(cmdline):
                    pass  # fall through to normal game checks
                else:
                    appid = _extract_steam_appid(cmdline)
                    if appid:
                        stream_hit = {
                            "process": proc_name,
                            "display_name": resolve_steam_app_name(appid),
                            "pid": pid,
                            "appid": appid,
                        }
                        if stem == "streaming_client":
                            return stream_hit
                        continue

            if stem in KNOWN_GAMES:
                known_hit = {
                    "process": proc_name,
                    "display_name": KNOWN_GAMES[stem],
                    "pid": pid,
                    "appid": KNOWN_GAME_APPIDS.get(stem, ""),
                }
                break
            if proc_lower in KNOWN_GAMES:
                known_hit = {
                    "process": proc_name,
                    "display_name": KNOWN_GAMES[proc_lower],
                    "pid": pid,
                    "appid": KNOWN_GAME_APPIDS.get(stem, KNOWN_GAME_APPIDS.get(proc_lower, "")),
                }
                break

            for game_key, display_name in KNOWN_GAMES.items():
                if len(game_key) < 5:
                    continue
                if re.search(rf"(^|[_\-.]){re.escape(game_key)}([_\-.]|$)", proc_lower):
                    known_hit = {
                        "process": proc_name,
                        "display_name": display_name,
                        "pid": pid,
                        "appid": KNOWN_GAME_APPIDS.get(game_key, ""),
                    }
                    break
            if known_hit:
                break

        if known_hit and not known_hit.get("appid"):
            known_hit["appid"] = _appid_for_process(known_hit["process"], known_hit["display_name"])
        if stream_hit and not stream_hit.get("appid"):
            stream_hit["appid"] = _appid_for_process(stream_hit["process"], stream_hit["display_name"])
        if moonlight_hit and not moonlight_hit.get("appid"):
            moonlight_hit["appid"] = _appid_for_process(
                moonlight_hit["process"], moonlight_hit["display_name"]
            )
        return known_hit or stream_hit or moonlight_hit

    def _gather_gamer_metrics(self, info: GamerInfo, pid: Optional[int] = None):
        """Gather system-wide CPU/GPU usage and temperatures."""
        # System CPU usage (0-100), non-blocking after first sample
        info.cpu_usage = float(psutil.cpu_percent(interval=None))
        if info.cpu_usage == 0.0:
            # First call often returns 0 — take a short sample once
            info.cpu_usage = float(psutil.cpu_percent(interval=0.15))
        info.cpu_temp = self._get_cpu_temp()

        # System RAM percent
        info.memory_usage = float(psutil.virtual_memory().percent)

        # GPU (NVIDIA via nvidia-smi / GPUtil; AMD via hwmon fallback)
        info.gpu_usage = self._get_gpu_usage()
        info.gpu_temp = self._get_gpu_temp()

    def _get_cpu_temp(self) -> float:
        """CPU package temperature in °C."""
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            return 0.0
        if not temps:
            return 0.0

        # Prefer package / Tctl / CPU labels
        preferred = ("Package id 0", "Tctl", "CPU", "cpu_thermal", "temp1")
        for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            entries = temps.get(chip) or []
            for label in preferred:
                for entry in entries:
                    if (entry.label or "") == label and entry.current:
                        return float(entry.current)
            if entries and entries[0].current:
                return float(entries[0].current)

        # Any sensor as last resort
        for entries in temps.values():
            for entry in entries:
                if entry.current:
                    return float(entry.current)
        return 0.0

    def _get_gpu_usage(self) -> float:
        """GPU utilization percentage."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip().split("\n")[0].strip())
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

        try:
            import GPUtil
            gpus = GPUtil.getGPUs()
            if gpus:
                return float(gpus[0].load * 100)
        except Exception:
            pass

        return 0.0

    def _get_gpu_temp(self) -> float:
        """GPU temperature in °C."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip().split("\n")[0].strip())
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

        # AMD / Intel via hwmon
        try:
            for path in Path("/sys/class/hwmon").iterdir():
                name = ""
                try:
                    name = (path / "name").read_text().strip().lower()
                except OSError:
                    continue
                if name not in ("amdgpu", "radeon", "i915", "xe"):
                    continue
                temp_file = path / "temp1_input"
                if temp_file.exists():
                    return float(temp_file.read_text().strip()) / 1000.0
        except (OSError, ValueError):
            pass

        return 0.0


# ---------------------------------------------------------------------------
# Session lock detection (logind / GNOME / freedesktop ScreenSaver)
# ---------------------------------------------------------------------------

class LockDetector:
    """Detect whether the graphical session is locked."""

    def __init__(self):
        self._last_info = LockInfo()

    def detect(self) -> LockInfo:
        """Return current lock state (best-effort across desktop environments)."""
        for checker, source in (
            (self._locked_via_logind, "logind"),
            (self._locked_via_gnome_screensaver, "gnome"),
            (self._locked_via_freedesktop_screensaver, "freedesktop"),
        ):
            try:
                result = checker()
            except Exception as exc:
                logger.debug("Lock check via %s failed: %s", source, exc)
                continue
            if result is None:
                continue
            info = LockInfo(is_locked=bool(result), source=source)
            self._last_info = info
            return info
        return self._last_info

    def is_locked(self) -> bool:
        return bool(self.detect().is_locked)

    @staticmethod
    def _parse_busctl_bool(stdout: str) -> Optional[bool]:
        text = (stdout or "").strip().lower()
        if not text:
            return None
        # busctl: "b true" / "b false"  |  method call: "b true"
        if "true" in text or text.endswith("1"):
            return True
        if "false" in text or text.endswith("0"):
            return False
        return None

    def _locked_via_logind(self) -> Optional[bool]:
        """Prefer systemd-logind LockedHint on the caller's / graphical session."""
        # Fast path: session/auto resolves to the calling process session
        try:
            result = subprocess.run(
                [
                    "busctl",
                    "get-property",
                    "org.freedesktop.login1",
                    "/org/freedesktop/login1/session/auto",
                    "org.freedesktop.login1.Session",
                    "LockedHint",
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode == 0:
                parsed = self._parse_busctl_bool(result.stdout)
                if parsed is not None:
                    return parsed
        except (subprocess.SubprocessError, OSError):
            pass

        # Fallback: any active local graphical session
        try:
            listed = subprocess.run(
                ["loginctl", "list-sessions", "--no-legend"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if listed.returncode != 0:
                return None
            for line in listed.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                session_id = parts[0]
                props = subprocess.run(
                    [
                        "loginctl",
                        "show-session",
                        session_id,
                        "-p",
                        "LockedHint",
                        "-p",
                        "Type",
                        "-p",
                        "State",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if props.returncode != 0:
                    continue
                values = {}
                for prop_line in props.stdout.splitlines():
                    if "=" in prop_line:
                        key, val = prop_line.split("=", 1)
                        values[key.strip()] = val.strip()
                session_type = (values.get("Type") or "").lower()
                state = (values.get("State") or "").lower()
                if session_type not in ("wayland", "x11", "mir", "tty"):
                    continue
                if state not in ("active", "online"):
                    continue
                hint = (values.get("LockedHint") or "").lower()
                if hint in ("yes", "true", "1"):
                    return True
                if hint in ("no", "false", "0"):
                    return False
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        return None

    def _locked_via_gnome_screensaver(self) -> Optional[bool]:
        try:
            result = subprocess.run(
                [
                    "busctl",
                    "--user",
                    "call",
                    "org.gnome.ScreenSaver",
                    "/org/gnome/ScreenSaver",
                    "org.gnome.ScreenSaver",
                    "GetActive",
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode != 0:
                return None
            return self._parse_busctl_bool(result.stdout)
        except (subprocess.SubprocessError, OSError):
            return None

    def _locked_via_freedesktop_screensaver(self) -> Optional[bool]:
        try:
            result = subprocess.run(
                [
                    "busctl",
                    "--user",
                    "call",
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver",
                    "GetActive",
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode != 0:
                return None
            return self._parse_busctl_bool(result.stdout)
        except (subprocess.SubprocessError, OSError):
            return None


# ---------------------------------------------------------------------------
# Shared visual language (matches clock-display MAIN dashboard)
# ---------------------------------------------------------------------------

_WIDTH, _HEIGHT = 480, 320
_BG = (4, 8, 16)
_PANEL = (11, 20, 34)
_CYAN = (56, 214, 255)
_BLUE = (78, 132, 255)
_MAGENTA = (214, 84, 255)
_WHITE = (236, 244, 255)
_MUTED = (126, 150, 176)
_GREEN = (78, 232, 168)
_GRID = (10, 20, 34)
_ACCENT = (24, 48, 72)
_ORANGE = (255, 160, 64)
_FONT_REG = "res/fonts/roboto/Roboto-Regular.ttf"
_FONT_MED = "res/fonts/roboto/Roboto-Medium.ttf"
_FONT_BOLD = "res/fonts/roboto/Roboto-Bold.ttf"
_FONT_MONO = "res/fonts/roboto-mono/RobotoMono-Bold.ttf"


def _font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def _tech_bg():
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, _WIDTH, 32):
        draw.line((x, 0, x, _HEIGHT), fill=_GRID, width=1)
    for y in range(0, _HEIGHT, 32):
        draw.line((0, y, _WIDTH, y), fill=_GRID, width=1)
    draw.rectangle((0, 0, _WIDTH, 3), fill=_CYAN)
    return image, draw


def _corner_marks(draw, box, color):
    x1, y1, x2, y2 = box
    length = 10
    for points in (
        (x1, y1 + length, x1, y1, x1 + length, y1),
        (x2 - length, y1, x2, y1, x2, y1 + length),
        (x1, y2 - length, x1, y2, x1 + length, y2),
        (x2 - length, y2, x2, y2, x2, y2 - length),
    ):
        draw.line(points, fill=color, width=2)


def _fit_text(draw, text, font, max_width):
    text = (text or "").strip()
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _paste_cover(base, cover, box, radius=12):
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    fitted = cover.resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    base.paste(fitted, (x1, y1), mask)


def render_multimedia_mode(info: MultimediaInfo, now: datetime) -> object:
    """Immersive now-playing screen: cover-themed backdrop, large art, bold track HUD."""
    from PIL import Image, ImageDraw, ImageEnhance

    cover = info.cover_image
    cache_key = info.cover_url or f"{info.title}|{info.artist}"
    if cover is not None:
        accent, accent2, panel = _media_theme_from_cover(cover, cache_key)
    else:
        accent, accent2, panel = _GREEN, _CYAN, _PANEL
    ar, ag, ab = accent

    # --- Full-bleed cover backdrop ---
    if cover is not None:
        backdrop = _cover_fit(cover.convert("RGB"), (_WIDTH, _HEIGHT))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.78)
        backdrop = ImageEnhance.Contrast(backdrop).enhance(1.1)
        backdrop = ImageEnhance.Color(backdrop).enhance(1.2)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (_WIDTH, _HEIGHT), (*_BG, 255))

    wash = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    wash_draw.rectangle((0, 0, _WIDTH, 44), fill=(ar, ag, ab, 70))
    for y in range(200, _HEIGHT):
        t = (y - 200) / max(1, _HEIGHT - 200)
        wash_draw.line((0, y, _WIDTH, y), fill=(4, 6, 10, int(30 + 200 * t)))
    image = Image.alpha_composite(image, wash)

    # Large square album cover
    cover_box = (18, 42, 198, 222)  # 180×180
    frame = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    frame_draw = ImageDraw.Draw(frame)
    frame_draw.rounded_rectangle(
        (cover_box[0] - 4, cover_box[1] - 4, cover_box[2] + 4, cover_box[3] + 4),
        radius=16,
        fill=(ar, ag, ab, 220),
    )
    frame_draw.rounded_rectangle(cover_box, radius=12, fill=(8, 10, 14, 255))
    image = Image.alpha_composite(image, frame)
    if cover is not None:
        _paste_cover(image, cover, cover_box, radius=12)

    # Soft scrim behind text so labels stay readable without a heavy card
    scrim = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rounded_rectangle(
        (208, 44, 462, 228),
        radius=14,
        fill=(0, 0, 0, 110),
    )
    image = Image.alpha_composite(image, scrim)

    image_rgb = image.convert("RGB")
    draw = ImageDraw.Draw(image_rgb)
    if cover is None:
        cx = (cover_box[0] + cover_box[2]) // 2
        cy = (cover_box[1] + cover_box[3]) // 2
        draw.text((cx, cy), "♪", font=_font(_FONT_BOLD, 64), fill=accent, anchor="mm")

    draw.rectangle((0, 0, _WIDTH - 1, _HEIGHT - 1), outline=accent, width=3)

    # Clock
    clock = now.strftime("%H:%M:%S")
    draw.text((447, 16), clock, font=_font(_FONT_MONO, 26), fill=(0, 0, 0), anchor="ra")
    draw.text((446, 15), clock, font=_font(_FONT_MONO, 26), fill=_WHITE, anchor="ra")

    text_left = 216
    app = (info.app_name or "MEDIA").upper()
    draw.text((text_left, 48), app, font=_font(_FONT_MONO, 13), fill=accent)

    title = _fit_text(draw, info.title or "Sem música", _font(_FONT_BOLD, 26), 240)
    draw.text((text_left + 2, 72), title, font=_font(_FONT_BOLD, 26), fill=(0, 0, 0))
    draw.text((text_left, 70), title, font=_font(_FONT_BOLD, 26), fill=_WHITE)

    artist = _fit_text(draw, info.artist or "Artista desconhecido", _font(_FONT_MED, 18), 240)
    draw.text((text_left, 104), artist, font=_font(_FONT_MED, 18), fill=accent2)

    y_cursor = 132
    if info.album:
        album = _fit_text(draw, info.album, _font(_FONT_REG, 15), 240)
        draw.text((text_left, y_cursor), album, font=_font(_FONT_REG, 15), fill=_WHITE)
        y_cursor += 24

    # Volume bar (no central time badge)
    vol, muted = info.volume, info.muted
    if vol <= 0 and not muted:
        vol, muted = get_system_volume()
    vol_color = _HW_HOT if muted else (_HW_WARN if vol >= 0.9 else accent)
    vol_y = min(200, y_cursor + 14)
    draw.text((text_left, vol_y), "MUTE" if muted else "VOL", font=_font(_FONT_MONO, 14), fill=vol_color)
    vol_x1, vol_x2 = text_left + 52, 454
    draw.rounded_rectangle((vol_x1, vol_y + 4, vol_x2, vol_y + 16), radius=5, fill=(20, 32, 48))
    if not muted and vol > 0:
        fill = int(vol_x1 + (vol_x2 - vol_x1) * max(0.0, min(1.0, vol)))
        if fill > vol_x1:
            draw.rounded_rectangle((vol_x1, vol_y + 4, fill, vol_y + 16), radius=5, fill=vol_color)
    draw.text(
        (vol_x2, vol_y + 28),
        "—" if muted else f"{int(vol * 100)}%",
        font=_font(_FONT_MONO, 16),
        fill=vol_color,
        anchor="ra",
    )

    # Bottom HUD — track progress only
    hud_y = 250
    draw.rectangle((0, hud_y - 6, _WIDTH, _HEIGHT), fill=(6, 8, 12))
    draw.rectangle((0, hud_y - 6, _WIDTH, hud_y - 3), fill=accent)

    bar_x1, bar_x2 = 18, 462
    bar_y = hud_y + 10
    draw.rounded_rectangle((bar_x1, bar_y, bar_x2, bar_y + 12), radius=5, fill=(20, 32, 48))
    if info.length > 0:
        ratio = max(0.0, min(1.0, info.position / info.length))
        fill = int(bar_x1 + (bar_x2 - bar_x1) * ratio)
        if fill > bar_x1:
            draw.rounded_rectangle((bar_x1, bar_y, fill, bar_y + 12), radius=5, fill=accent)

    draw.text((bar_x1, bar_y + 18), format_time(info.position), font=_font(_FONT_MONO, 14), fill=accent2)
    draw.text((bar_x2, bar_y + 18), format_time(info.length), font=_font(_FONT_MONO, 14), fill=accent2, anchor="ra")

    return image_rgb


def render_gamer_mode(info: GamerInfo, now: datetime) -> object:
    """Immersive game-branded HUD: hero art, capsule cover, logo, compact stats."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    accent = tuple(info.theme_accent or _ORANGE)
    accent2 = tuple(info.theme_accent2 or _BLUE)
    ar, ag, ab = accent

    # --- Full-bleed game art (kept vivid so the theme is obvious) ---
    source = info.game_art_image or info.game_capsule_image
    if source is not None:
        backdrop = _cover_fit(source.convert("RGB"), (_WIDTH, _HEIGHT))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.78)
        backdrop = ImageEnhance.Contrast(backdrop).enhance(1.1)
        backdrop = ImageEnhance.Color(backdrop).enhance(1.2)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (_WIDTH, _HEIGHT), (*_BG, 255))

    # Top accent wash + bottom band for HUD readability
    wash = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    wash_draw.rectangle((0, 0, _WIDTH, 44), fill=(ar, ag, ab, 70))
    for y in range(210, _HEIGHT):
        t = (y - 210) / max(1, _HEIGHT - 210)
        wash_draw.line((0, y, _WIDTH, y), fill=(4, 6, 10, int(30 + 200 * t)))
    image = Image.alpha_composite(image, wash)

    # Portrait capsule — main visual identity
    cover = info.game_capsule_image or info.game_art_image
    cover_box = (18, 42, 168, 238)  # leave room for larger hardware HUD
    frame = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    frame_draw = ImageDraw.Draw(frame)
    frame_draw.rounded_rectangle(
        (cover_box[0] - 4, cover_box[1] - 4, cover_box[2] + 4, cover_box[3] + 4),
        radius=16,
        fill=(ar, ag, ab, 220),
    )
    frame_draw.rounded_rectangle(cover_box, radius=12, fill=(8, 10, 14, 255))
    image = Image.alpha_composite(image, frame)
    if cover is not None:
        _paste_cover(image, cover, cover_box, radius=12)

    # Logo (large) to the right of the capsule
    text_left = 196
    logo_bottom = 56
    if info.game_logo_image is not None:
        try:
            logo = info.game_logo_image.convert("RGBA")
            max_w, max_h = 260, 120
            lw, lh = logo.size
            scale = min(max_w / max(1, lw), max_h / max(1, lh))
            nw, nh = max(1, int(lw * scale)), max(1, int(lh * scale))
            logo = logo.resize((nw, nh), Image.Resampling.LANCZOS)
            shadow = Image.new("RGBA", (nw + 10, nh + 10), (0, 0, 0, 0))
            shadow.paste((0, 0, 0, 160), (5, 5), logo.split()[-1])
            shadow = shadow.filter(ImageFilter.GaussianBlur(4))
            lx, ly = text_left, 52
            image.paste(shadow, (lx - 2, ly + 3), shadow)
            image.paste(logo, (lx, ly), logo)
            logo_bottom = ly + nh + 8
        except Exception as exc:
            logger.debug("Game logo paste failed: %s", exc)

    image_rgb = image.convert("RGB")
    draw = ImageDraw.Draw(image_rgb)

    # Outer accent frame
    draw.rectangle((0, 0, _WIDTH - 1, _HEIGHT - 1), outline=accent, width=3)

    # Clock top-right
    clock = now.strftime("%H:%M:%S")
    draw.text((447, 16), clock, font=_font(_FONT_MONO, 26), fill=(0, 0, 0), anchor="ra")
    draw.text((446, 15), clock, font=_font(_FONT_MONO, 26), fill=_WHITE, anchor="ra")

    if info.game_logo_image is None:
        name = _fit_text(draw, info.game_name or "Jogo", _font(_FONT_BOLD, 32), 260)
        draw.text((text_left + 2, 58), name, font=_font(_FONT_BOLD, 32), fill=(0, 0, 0))
        draw.text((text_left, 56), name, font=_font(_FONT_BOLD, 32), fill=_WHITE)
        logo_bottom = 96
    elif info.game_name:
        sub = _fit_text(draw, info.game_name.upper(), _font(_FONT_MONO, 13), 250)
        draw.text((text_left, logo_bottom), sub, font=_font(_FONT_MONO, 13), fill=_WHITE)

    # Session playtime (replaces the old "JOGANDO" badge)
    badge_y = min(200, max(logo_bottom + 8, 160))
    playtime = info.elapsed or "00:00:00"
    badge_w = max(124, int(draw.textlength(playtime, font=_font(_FONT_MONO, 18)) + 28))
    draw.rounded_rectangle((text_left, badge_y, text_left + badge_w, badge_y + 34), radius=8, fill=accent)
    draw.text(
        (text_left + badge_w // 2, badge_y + 17),
        playtime,
        font=_font(_FONT_MONO, 18),
        fill=(12, 12, 14),
        anchor="mm",
    )

    # Bottom HUD — larger hardware readouts (colors shift when load/temp is high)
    hud_y = 250
    draw.rectangle((0, hud_y - 6, _WIDTH, _HEIGHT), fill=(6, 8, 12))
    draw.rectangle((0, hud_y - 6, _WIDTH, hud_y - 3), fill=accent)

    stats = [
        ("CPU", f"{info.cpu_usage:.0f}%", _hw_level_color(info.cpu_usage, kind="usage", fallback=accent2)),
        ("CPU°", f"{info.cpu_temp:.0f}°" if info.cpu_temp else "--", _hw_level_color(info.cpu_temp, kind="temp", fallback=accent)),
        ("GPU", f"{info.gpu_usage:.0f}%", _hw_level_color(info.gpu_usage, kind="usage", fallback=accent2)),
        ("GPU°", f"{info.gpu_temp:.0f}°" if info.gpu_temp else "--", _hw_level_color(info.gpu_temp, kind="temp", fallback=accent)),
    ]
    slot_w = _WIDTH // 4
    for i, (label, value, color) in enumerate(stats):
        cx = slot_w * i + slot_w // 2
        draw.text((cx, hud_y + 8), label, font=_font(_FONT_MONO, 15), fill=color, anchor="ma")
        draw.text((cx, hud_y + 32), value, font=_font(_FONT_BOLD, 28), fill=color, anchor="ma")

    return image_rgb


def _draw_lock_icon(draw, cx: int, cy: int, color: tuple, scale: float = 1.0):
    """Draw a simple padlock centered at (cx, cy)."""
    s = scale
    # Shackle
    left = int(cx - 22 * s)
    right = int(cx + 22 * s)
    top = int(cy - 48 * s)
    mid = int(cy - 18 * s)
    draw.arc((left, top, right, mid + int(22 * s)), start=180, end=0, fill=color, width=max(3, int(5 * s)))
    # Body
    body = (
        int(cx - 28 * s),
        int(cy - 14 * s),
        int(cx + 28 * s),
        int(cy + 36 * s),
    )
    draw.rounded_rectangle(body, radius=max(4, int(8 * s)), outline=color, width=max(2, int(4 * s)))
    draw.rounded_rectangle(
        (
            body[0] + int(6 * s),
            body[1] + int(6 * s),
            body[2] - int(6 * s),
            body[3] - int(6 * s),
        ),
        radius=max(3, int(6 * s)),
        fill=color,
    )
    # Keyhole
    kh_color = (12, 14, 18)
    draw.ellipse(
        (int(cx - 6 * s), int(cy - 2 * s), int(cx + 6 * s), int(cy + 10 * s)),
        fill=kh_color,
    )
    draw.polygon(
        [
            (int(cx - 3 * s), int(cy + 8 * s)),
            (int(cx + 3 * s), int(cy + 8 * s)),
            (int(cx + 2 * s), int(cy + 22 * s)),
            (int(cx - 2 * s), int(cy + 22 * s)),
        ],
        fill=kh_color,
    )


def render_locked_mode(now: datetime, theme: Optional[dict] = None) -> object:
    """Minimal lock screen: OS-themed backdrop, lock icon, time only."""
    from PIL import Image, ImageDraw, ImageEnhance

    theme = theme or {}
    accent = tuple(theme.get("accent") or _CYAN)
    accent2 = tuple(theme.get("accent2") or _BLUE)
    wallpaper = theme.get("wallpaper")
    ar, ag, ab = accent

    if wallpaper is not None:
        backdrop = _cover_fit(wallpaper.convert("RGB"), (_WIDTH, _HEIGHT))
        # Darker than MAIN — panel brightness also drops to 10%
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.42)
        backdrop = ImageEnhance.Contrast(backdrop).enhance(1.05)
        backdrop = ImageEnhance.Color(backdrop).enhance(0.85)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (_WIDTH, _HEIGHT), (*_BG, 255))

    wash = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    wash_draw.rectangle((0, 0, _WIDTH, 6), fill=(ar, ag, ab, 100))
    # Soft vignette + center scrim so the clock stays readable
    for y in range(_HEIGHT):
        edge = min(y, _HEIGHT - 1 - y) / max(1, _HEIGHT // 2)
        alpha = int(40 + 90 * (1.0 - edge))
        wash_draw.line((0, y, _WIDTH, y), fill=(4, 6, 10, alpha))
    wash_draw.rounded_rectangle(
        (40, 36, _WIDTH - 40, _HEIGHT - 36),
        radius=20,
        fill=(0, 0, 0, 120),
    )
    image = Image.alpha_composite(image, wash)

    image_rgb = image.convert("RGB")
    draw = ImageDraw.Draw(image_rgb)
    draw.rectangle((0, 0, _WIDTH - 1, _HEIGHT - 1), outline=accent, width=3)

    # Lock icon
    _draw_lock_icon(draw, _WIDTH // 2, 88, accent, scale=1.15)

    # Time only (large, centered)
    clock = now.strftime("%H:%M:%S")
    draw.text(
        (_WIDTH // 2 + 2, 188),
        clock,
        font=_font(_FONT_MONO, 72),
        fill=(0, 0, 0),
        anchor="mm",
    )
    draw.text(
        (_WIDTH // 2, 186),
        clock,
        font=_font(_FONT_MONO, 72),
        fill=_WHITE,
        anchor="mm",
    )

    draw.text(
        (_WIDTH // 2, 248),
        "BLOQUEADO",
        font=_font(_FONT_MONO, 18),
        fill=accent2,
        anchor="mm",
    )

    return image_rgb


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def format_time(seconds: float) -> str:
    """Format seconds to MM:SS or HH:MM:SS."""
    if seconds <= 0:
        return "0:00"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_playtime(seconds: float) -> str:
    """Format session playtime as HH:MM:SS."""
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


_HW_OK = (78, 232, 168)
_HW_WARN = (255, 176, 64)
_HW_HOT = (255, 72, 72)


def _hw_level_color(value: float, kind: str = "usage", fallback=(78, 232, 168)) -> tuple:
    """
    Color hardware indicators by load/temperature.
    usage:  %  — warn ≥70, hot ≥90
    temp:   °C — warn ≥75, hot ≥90
    """
    try:
        level = float(value or 0)
    except (TypeError, ValueError):
        return fallback
    if level <= 0:
        return fallback
    if kind == "temp":
        if level >= 90:
            return _HW_HOT
        if level >= 75:
            return _HW_WARN
        return _HW_OK
    # usage %
    if level >= 90:
        return _HW_HOT
    if level >= 70:
        return _HW_WARN
    return _HW_OK


# ---------------------------------------------------------------------------
# ModeManager: orchestrates detection, switching, and rendering
# ---------------------------------------------------------------------------

class ModeManager:
    """Manages mode detection, switching, and rendering."""

    def __init__(self, config_mode: Optional[str] = None):
        self.state = ModeState()
        self.multimedia_detector = MultimediaDetector()
        self.gamer_detector = GamerDetector()
        self.lock_detector = LockDetector()
        self._forced = False

        if config_mode:
            mode_map = {
                "main": Mode.MAIN,
                "multimedia": Mode.MULTIMEDIA,
                "gamer": Mode.GAMER,
                "locked": Mode.LOCKED,
            }
            self.state.current_mode = mode_map.get(config_mode.lower(), Mode.MAIN)
            self._forced = True
            logger.info("Mode set to: %s (from config)", self.state.current_mode.name)

    def get_render_function(self) -> Optional[callable]:
        if self.state.current_mode == Mode.MAIN:
            return None
        if self.state.current_mode == Mode.MULTIMEDIA:
            return lambda: render_multimedia_mode(self.state.multimedia, datetime.now())
        if self.state.current_mode == Mode.GAMER:
            return lambda: render_gamer_mode(self.state.gamer, datetime.now())
        if self.state.current_mode == Mode.LOCKED:
            return lambda: render_locked_mode(datetime.now())
        return None

    def poll_lock(self) -> bool:
        """Lightweight lock poll (call more often than full detect_and_switch).

        Returns True when the active mode changed because of lock/unlock.
        """
        if self._forced:
            return False

        previous = self.state.current_mode
        lock_info = None
        try:
            lock_info = self.lock_detector.detect()
        except Exception as exc:
            logger.debug("Lock detection failed: %s", exc)
            return False

        if lock_info and lock_info.is_locked:
            self.state.lock_hits += 1
            self.state.lock_misses = 0
            self.state.lock = lock_info
            if self.state.current_mode != Mode.LOCKED:
                if self.state.lock_hits >= self.state.lock_enter_confirm:
                    self._switch_mode(Mode.LOCKED)
            return self.state.current_mode != previous

        self.state.lock_hits = 0
        if self.state.current_mode == Mode.LOCKED:
            self.state.lock_misses += 1
            if self.state.lock_misses < self.state.lock_leave_confirm:
                return False
            self.state.lock_misses = 0
            self.state.lock = LockInfo(
                is_locked=False,
                source=lock_info.source if lock_info else "",
            )
            # Leave LOCKED immediately; full detect_and_switch picks game/media next
            self._switch_mode(Mode.MAIN)
            self.state.last_switch_time = 0.0
            return True
        return False

    def detect_and_switch(self):
        """Auto-detect the appropriate mode and switch if needed.

        Priority: LOCKED > GAMER > MULTIMEDIA > MAIN
        """
        if self._forced:
            return

        # Lock handled by poll_lock() on a faster cadence — skip if already locked
        if self.state.current_mode == Mode.LOCKED:
            return

        now = time.monotonic()
        if now - self.state.last_switch_time < self.state.switch_cooldown:
            return

        game_info = None
        try:
            game_info = self.gamer_detector.detect()
        except Exception as exc:
            logger.debug("Game detection failed: %s", exc)

        if game_info and game_info.game_name:
            self.state.game_hits += 1
            self.state.game_misses = 0
            self.state.media_hits = 0
            self.state.gamer = game_info
            if self.state.current_mode != Mode.GAMER:
                if self.state.game_hits >= self.state.enter_confirm:
                    self._switch_mode(Mode.GAMER)
            return

        self.state.game_hits = 0
        if self.state.current_mode == Mode.GAMER:
            self.state.game_misses += 1
            if self.state.game_misses < self.state.leave_confirm:
                return
            self.state.game_misses = 0

        media_info = None
        try:
            media_info = self.multimedia_detector.detect()
        except Exception as exc:
            logger.debug("Multimedia detection failed: %s", exc)

        if media_info and media_info.is_playing and media_info.title:
            self.state.media_hits += 1
            self.state.media_misses = 0
            self.state.multimedia = media_info
            if self.state.current_mode != Mode.MULTIMEDIA:
                if self.state.media_hits >= self.state.enter_confirm:
                    self._switch_mode(Mode.MULTIMEDIA)
            return

        self.state.media_hits = 0
        if self.state.current_mode == Mode.MULTIMEDIA:
            self.state.media_misses += 1
            if self.state.media_misses < self.state.leave_confirm:
                return
            self.state.media_misses = 0

        if self.state.current_mode != Mode.MAIN:
            self._switch_mode(Mode.MAIN)

    def _switch_mode(self, new_mode: Mode):
        self.state.previous_mode = self.state.current_mode
        self.state.current_mode = new_mode
        self.state.last_switch_time = time.monotonic()
        logger.info("Mode switched: %s -> %s", self.state.previous_mode.name, new_mode.name)

    def force_mode(self, new_mode: Mode):
        if self.state.current_mode == new_mode:
            return
        self._switch_mode(new_mode)
