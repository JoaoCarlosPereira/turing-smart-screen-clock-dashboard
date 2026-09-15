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
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

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


def _extract_metadata_field(metadata: str, key: str) -> str:
    if key.endswith(":artist"):
        match = re.search(rf'{key}"\s+as\s+\d+\s+"([^"]*)"', metadata)
        if match:
            return match.group(1)
    match = re.search(rf'{key}"\s+s\s+"([^"]*)"', metadata)
    return match.group(1) if match else ""


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
        return {
            "title": title,
            "artist": artist,
            "album": album,
            "is_playing": is_playing,
            "position": position_us / 1_000_000,
            "length": length_us / 1_000_000,
            "player_id": bus_name.rsplit(".", 1)[-1],
        }
    return None


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
                app, _icon, title, body = strings[0], strings[1], strings[2], strings[3]
                if title or body:
                    with _NOTIFICATION_LOCK:
                        _NOTIFICATION_QUEUE.append({"app": app, "title": title, "body": body})
                capturing = False


def _pop_notifications() -> list[dict]:
    with _NOTIFICATION_LOCK:
        items, _NOTIFICATION_QUEUE[:] = list(_NOTIFICATION_QUEUE), []
    return items


def poll_and_announce(interval: float, port: int) -> None:
    sock = common.make_udp_socket()
    last_label = ""
    while True:
        try:
            game = sample_game()
            media = sample_media()
            lock_state = sample_lock()
            lock = {"is_locked": lock_state} if lock_state is not None else None

            payload = common.build_state_payload(HOST_ID, HOSTNAME, game=game, media=media, lock=lock)
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
