#!/usr/bin/env python3
"""
Mode system for Turing Smart Screen.

Manages multiple display modes: MAIN (clock/dashboard), MULTIMEDIA (media info),
and GAMER (gaming overlay with FPS and hardware stats).
"""

import json
import os
import re
import subprocess
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
    last_switch_time: float = 0.0
    switch_cooldown: float = 3.0  # seconds before allowing another switch


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

    def detect(self) -> MultimediaInfo:
        """Query all MPRIS2 players and return the playing one."""
        try:
            player_names = self._get_mpris_players()
        except Exception as exc:
            logger.debug("Failed to query MPRIS2 players: %s", exc)
            vol, muted = get_system_volume()
            self._last_info.volume = vol
            self._last_info.muted = muted
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
        for bus_name, player_id in sorted_players:
            info = self._get_player_info(bus_name, player_id)
            if info and (info.is_playing or info.title):
                info.player_id = player_id
                vol, muted = get_system_volume()
                info.volume = vol
                info.muted = muted
                self._last_info = info
                return info

        # No playing media, but keep last info as paused
        self._last_info.is_playing = False
        vol, muted = get_system_volume()
        self._last_info.volume = vol
        self._last_info.muted = muted
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
            if session_key != self._session_key or self._session_started_at is None:
                self._session_key = session_key
                self._session_started_at = proc_started or datetime.now().astimezone()
            elif proc_started and proc_started < self._session_started_at:
                # Prefer earlier process start (e.g. reaper launched before client)
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

    def refresh_metrics(self, info: GamerInfo) -> GamerInfo:
        """Update CPU/GPU usage and temperatures on an existing GamerInfo."""
        self._gather_gamer_metrics(info)
        return info

    def _detect_game(self) -> Optional[dict]:
        """Detect a running local game or Steam Remote Play stream."""
        try:
            procs = list(psutil.process_iter(["name", "pid", "ppid", "cmdline"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        stream_hit = None
        known_hit = None

        for proc in procs:
            proc_name = proc.info.get("name") or ""
            proc_lower = proc_name.lower()
            pid = proc.info.get("pid")
            ppid = proc.info.get("ppid")
            cmdline = proc.info.get("cmdline") or []

            if not proc_lower or pid < 200 or ppid in (0, 2):
                continue
            if proc_lower in NON_GAME_PROCESSES or Path(proc_lower).stem in NON_GAME_PROCESSES:
                continue

            # Steam Remote Play / in-home streaming — game runs on another PC
            if proc_lower in {"streaming_client", "reaper"} or "streaming_client" in " ".join(cmdline):
                appid = _extract_steam_appid(cmdline)
                if appid:
                    stream_hit = {
                        "process": proc_name,
                        "display_name": resolve_steam_app_name(appid),
                        "pid": pid,
                        "appid": appid,
                    }
                    # Prefer streaming_client over reaper wrapper
                    if proc_lower == "streaming_client":
                        return stream_hit
                continue

            stem = Path(proc_lower).stem
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
        return known_hit or stream_hit

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
        self._forced = False

        if config_mode:
            mode_map = {"main": Mode.MAIN, "multimedia": Mode.MULTIMEDIA, "gamer": Mode.GAMER}
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
        return None

    def detect_and_switch(self):
        """Auto-detect the appropriate mode and switch if needed."""
        if self._forced:
            return

        now = time.monotonic()
        if now - self.state.last_switch_time < self.state.switch_cooldown:
            return

        try:
            game_info = self.gamer_detector.detect()
            if game_info.game_name:
                if self.state.current_mode != Mode.GAMER:
                    self._switch_mode(Mode.GAMER)
                self.state.gamer = game_info
                return
        except Exception as exc:
            logger.debug("Game detection failed: %s", exc)

        try:
            media_info = self.multimedia_detector.detect()
            # Only switch when media is actually playing
            if media_info.is_playing and media_info.title:
                if self.state.current_mode != Mode.MULTIMEDIA:
                    self._switch_mode(Mode.MULTIMEDIA)
                self.state.multimedia = media_info
                return
        except Exception as exc:
            logger.debug("Multimedia detection failed: %s", exc)

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
