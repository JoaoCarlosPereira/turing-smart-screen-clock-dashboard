#!/usr/bin/env python3
"""Announce game/media/lock/notification state on an Ubuntu host to the Turing
clock on the LAN — the Linux counterpart to foreground_reporter.py (Windows).

Same wire protocol/port as the Windows agent (see common.py) so the Mini-PC's
receiver (modes.py) treats both the same way. Stdlib + standard CLI tools only
(busctl, loginctl, dbus-monitor — present on any systemd desktop), no pip
dependencies, no foreground-window title on Wayland (process-presence only;
see _sample_game).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

if sys.platform == "win32":
    sys.stderr.write("linux_reporter.py is for Linux/Ubuntu hosts — use foreground_reporter.py on Windows.\n")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

HOST_ID = common.get_or_create_host_id()
HOSTNAME = socket.gethostname()

_NOTIFICATION_QUEUE: list[dict] = []
_NOTIFICATION_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Game: local /proc scan (mirrors modes.py::_iter_processes), matched against
# the shared known-game table. No foreground-window title on Linux without an
# X11/Wayland-specific API, so this is process-presence detection only — same
# fallback tier the Windows agent uses when focus is on a shell.
# ---------------------------------------------------------------------------

def _iter_processes() -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return result
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{entry}/comm", "r", encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if comm:
            result.append((pid, comm))
    return result


def sample_game() -> dict | None:
    best: dict | None = None
    for pid, comm in _iter_processes():
        if not common.known_game_hit(comm):
            continue
        best = {"title": comm, "exe": comm, "pid": pid}
        break
    return best


# ---------------------------------------------------------------------------
# Media: MPRIS2 via busctl --user — same technique as modes.py's
# MultimediaDetector (_get_mpris_players / _get_player_info), trimmed to the
# fields the wire protocol needs (no cover art, no YouTube duration lookup).
# ---------------------------------------------------------------------------

_MPRIS_BASE = "org.mpris.MediaPlayer2"


def _busctl(*args: str, timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(["busctl", *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _mpris_players() -> list[str]:
    listing = _busctl("--user", "list")
    if not listing:
        return []
    names = []
    prefix = f"{_MPRIS_BASE}."
    for line in listing.splitlines():
        parts = line.split()
        if parts and parts[0].startswith(prefix):
            names.append(parts[0])
    return names


def _decode_dbus_text(text: str) -> str:
    """Decode busctl/dbus-monitor C-style octal escapes (e.g. \\303\\241 -> á).

    Both tools print non-ASCII as octal byte escapes, so every string that
    reaches the wire (track metadata, notification text) must pass through
    here — otherwise the screen shows "SEQU\\303\\212NCIA" instead of
    "SEQUÊNCIA". Mirrors modes.py::MultimediaDetector._decode_dbus_text.
    """
    if not text or "\\" not in text:
        return text

    def repl(match):
        chunks = re.findall(r"\\([0-7]{3})", match.group(0))
        try:
            return bytes(int(c, 8) for c in chunks).decode("utf-8")
        except Exception:
            return match.group(0)

    return re.sub(r"(?:\\[0-7]{3})+", repl, text)


def _extract_metadata_field(metadata: str, key: str) -> str:
    if key.endswith(":artist"):
        match = re.search(rf'{key}"\s+as\s+\d+\s+"([^"]*)"', metadata)
        if match:
            return _decode_dbus_text(match.group(1))
    match = re.search(rf'{key}"\s+s\s+"([^"]*)"', metadata)
    return _decode_dbus_text(match.group(1)) if match else ""


def _extract_metadata_int(metadata: str, key: str) -> int:
    match = re.search(rf'{key}"\s+[txui]\s+(\d+)', metadata)
    return int(match.group(1)) if match else 0


def sample_media() -> dict | None:
    for bus_name in _mpris_players():
        if "playerctld" in bus_name:
            continue
        object_path = "/org/mpris/MediaPlayer2"
        iface = "org.mpris.MediaPlayer2.Player"
        status = _busctl("--user", "get-property", bus_name, object_path, iface, "PlaybackStatus")
        metadata = _busctl("--user", "get-property", bus_name, object_path, iface, "Metadata")
        position_raw = _busctl("--user", "get-property", bus_name, object_path, iface, "Position")

        is_playing = bool(status and "Playing" in status)
        if not is_playing and not metadata:
            continue

        title = _extract_metadata_field(metadata or "", "xesam:title")
        artist = _extract_metadata_field(metadata or "", "xesam:artist")
        album = _extract_metadata_field(metadata or "", "xesam:album")
        length_us = _extract_metadata_int(metadata or "", "mpris:length")
        position_us = 0
        if position_raw:
            match = re.match(r"^[txui]\s+(-?\d+)$", position_raw.strip())
            if match:
                position_us = int(match.group(1))

        if not title and not artist:
            continue

        # Cover art: only http(s) URLs travel — the receiver is a different
        # machine, so a file:// path would either 404 there or, worse, resolve
        # to an unrelated local file with the same path. Players that only
        # expose local art (Rhythmbox, VLC) therefore arrive without a cover.
        cover_url = _extract_metadata_field(metadata or "", "mpris:artUrl")
        if not cover_url.startswith(("http://", "https://")):
            cover_url = ""
        # xesam:url lets the receiver recover a YouTube thumbnail/duration the
        # same way it does for local players (YouTube exposes no mpris:artUrl).
        track_url = _extract_metadata_field(metadata or "", "xesam:url")

        return {
            "title": title,
            "artist": artist,
            "album": album,
            "is_playing": is_playing,
            "position": position_us / 1_000_000,
            "length": length_us / 1_000_000,
            "player_id": bus_name.rsplit(".", 1)[-1],
            "cover_url": cover_url,
            "url": track_url,
        }
    return None


# ---------------------------------------------------------------------------
# Desktop theme: the wallpaper this host is using, so the screen (which now
# lives on a headless machine with no wallpaper of its own) can wear the same
# background and derive its accent colors from it. Only a small id travels by
# UDP; the image itself is served over HTTP and fetched when the id changes.
# ---------------------------------------------------------------------------

_WALLPAPER_STATE: dict = {"path": "", "id": ""}
_WALLPAPER_LOCK = threading.Lock()


def _gsettings(schema: str, key: str) -> str:
    try:
        result = subprocess.run(
            ["gsettings", "get", schema, key], capture_output=True, text=True, timeout=2
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().strip("'\"")


def _wallpaper_path() -> str:
    """Active wallpaper file, GNOME first then COSMIC (mirrors clock-display)."""
    dark = "prefer-dark" in _gsettings("org.gnome.desktop.interface", "color-scheme")
    keys = ("picture-uri-dark", "picture-uri") if dark else ("picture-uri", "picture-uri-dark")
    for key in keys:
        uri = _gsettings("org.gnome.desktop.background", key)
        if uri.startswith("file://"):
            path = Path(unquote(urlparse(uri).path))
            if path.is_file():
                return str(path)

    root = Path.home() / ".config/cosmic/com.system76.CosmicBackground/v1/backgrounds"
    for candidate in (root / "all", root):
        try:
            text = candidate.read_text(encoding="utf-8") if candidate.is_file() else ""
        except OSError:
            continue
        match = re.search(r'Path\("([^"]+)"\)', text)
        if match and Path(match.group(1)).is_file():
            return match.group(1)
    return ""


def sample_desktop() -> dict | None:
    path = _wallpaper_path()
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    # Identity = path + mtime + size, so editing or swapping the wallpaper
    # invalidates the receiver's cached copy without re-sending 4 MB per tick.
    wallpaper_id = hashlib.sha1(
        f"{path}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")
    ).hexdigest()[:16]
    with _WALLPAPER_LOCK:
        _WALLPAPER_STATE["path"] = path
        _WALLPAPER_STATE["id"] = wallpaper_id
    return {
        "wallpaper_id": wallpaper_id,
        "wallpaper_name": Path(path).name,
        "is_dark": "prefer-dark" in _gsettings("org.gnome.desktop.interface", "color-scheme"),
    }


class _WallpaperHandler(BaseHTTPRequestHandler):
    """Serves the current wallpaper so the remote screen can fetch it once."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] != "/wallpaper":
            self.send_error(404, "only /wallpaper is served")
            return
        with _WALLPAPER_LOCK:
            path, wallpaper_id = _WALLPAPER_STATE["path"], _WALLPAPER_STATE["id"]
        if not path:
            self.send_error(404, "no wallpaper on this host")
            return
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            self.send_error(500, f"cannot read wallpaper: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Wallpaper-Id", wallpaper_id)
        self.send_header("X-Wallpaper-Name", Path(path).name)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        """Silence per-request stderr logging (this runs under systemd)."""


def _serve_wallpaper(port: int) -> None:
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _WallpaperHandler)
    except OSError as exc:
        print(f"(wallpaper HTTP disabled: {exc})", flush=True)
        return
    server.serve_forever()


# ---------------------------------------------------------------------------
# Lock: systemd-logind LockedHint — same technique as modes.py's
# LockDetector._locked_via_logind.
# ---------------------------------------------------------------------------

def sample_lock() -> bool | None:
    result = _busctl(
        "get-property", "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
        "org.freedesktop.login1.Session", "LockedHint", timeout=1.0,
    )
    if result:
        text = result.strip().lower()
        if "true" in text or text.endswith("1"):
            return True
        if "false" in text or text.endswith("0"):
            return False

    try:
        sessions = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True, timeout=1
        )
        if sessions.returncode != 0:
            return None
        for line in sessions.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            session_id = parts[0]
            props = subprocess.run(
                ["loginctl", "show-session", session_id, "-p", "LockedHint", "-p", "Type", "-p", "State"],
                capture_output=True, text=True, timeout=1,
            )
            if props.returncode != 0:
                continue
            values = dict(
                line.split("=", 1) for line in props.stdout.splitlines() if "=" in line
            )
            if (values.get("Type") or "").lower() not in ("wayland", "x11", "mir", "tty"):
                continue
            if (values.get("State") or "").lower() not in ("active", "online"):
                continue
            hint = (values.get("LockedHint") or "").lower()
            if hint in ("yes", "true", "1"):
                return True
            if hint in ("no", "false", "0"):
                return False
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Notifications: dbus-monitor on the session bus — trimmed version of
# clock-display.py's notification_monitor() (app/title/body only, no portal/
# gtk fan-out, no icon persistence — icons never cross the network here).
# ---------------------------------------------------------------------------

def _notification_monitor_loop() -> None:
    command = [
        "stdbuf", "-oL", "dbus-monitor", "--session",
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    ]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except (OSError, FileNotFoundError) as exc:
        print(f"(notification forwarding disabled: {exc})", flush=True)
        return

    capturing = False
    strings: list[str] = []
    for line in proc.stdout:
        if line.startswith("method call") and "member=Notify" in line:
            capturing = True
            strings = []
            continue
        if not capturing:
            continue
        match = re.search(r'string "(.*)"', line)
        if match:
            strings.append(match.group(1))
            if len(strings) >= 4:
                app, _icon, title, body = (
                    _decode_dbus_text(strings[0]),
                    strings[1],
                    _decode_dbus_text(strings[2]),
                    _decode_dbus_text(strings[3]),
                )
                if title or body:
                    with _NOTIFICATION_LOCK:
                        _NOTIFICATION_QUEUE.append({"app": app, "title": title, "body": body})
                capturing = False


def _pop_notifications() -> list[dict]:
    with _NOTIFICATION_LOCK:
        items, _NOTIFICATION_QUEUE[:] = list(_NOTIFICATION_QUEUE), []
    return items


_DESKTOP_SAMPLE_SECONDS = 15.0


def poll_and_announce(interval: float, port: int) -> None:
    sock = common.make_udp_socket()
    last_label = ""
    desktop_cache: list = [None, -_DESKTOP_SAMPLE_SECONDS]
    while True:
        try:
            game = sample_game()
            media = sample_media()
            lock_state = sample_lock()
            lock = {"is_locked": lock_state} if lock_state is not None else None

            # The wallpaper changes rarely and sampling it costs two gsettings
            # subprocesses, so re-read it only every _DESKTOP_SAMPLE_SECONDS;
            # the cached value still rides along on every tick.
            now = time.monotonic()
            nonlocal_desktop = desktop_cache[0]
            if now - desktop_cache[1] >= _DESKTOP_SAMPLE_SECONDS:
                nonlocal_desktop = sample_desktop()
                desktop_cache[0], desktop_cache[1] = nonlocal_desktop, now

            payload = common.build_state_payload(
                HOST_ID, HOSTNAME, game=game, media=media, lock=lock, desktop=nonlocal_desktop
            )
            common.send_payload(sock, payload, port)

            for note in _pop_notifications():
                note_payload = common.build_notification_payload(
                    HOST_ID, HOSTNAME, note["app"] or "Linux", note["title"], note["body"]
                )
                common.send_payload(sock, note_payload, port)

            label = (game or {}).get("title") or ""
            if label != last_label:
                last_label = label
                print(f"announcing: {label or '(no game)'}", flush=True)
        except Exception as exc:
            print(f"sample error: {exc}", flush=True)
        time.sleep(max(0.5, interval))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turing host agent (Linux) — announces game/media/lock/notifications on the LAN"
    )
    parser.add_argument("--port", type=int, default=common.DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    threading.Thread(target=_notification_monitor_loop, name="turing-host-notify", daemon=True).start()
    threading.Thread(
        target=_serve_wallpaper, args=(args.port,), name="turing-host-wallpaper", daemon=True
    ).start()

    print(
        f"turing-host-agent (linux) announcing on UDP {common.MULTICAST_GROUP}:{args.port} "
        f"+ broadcast :{args.port} (host_id={HOST_ID[:8]}...)",
        flush=True,
    )
    try:
        poll_and_announce(args.interval, args.port)
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
