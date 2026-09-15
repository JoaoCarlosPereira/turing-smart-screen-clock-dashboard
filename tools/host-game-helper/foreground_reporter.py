#!/usr/bin/env python3
"""Announce game/media/lock state on the Windows host to the Turing clock on the LAN.

Zero-config for the Mini-PC: broadcasts UDP; the clock listens automatically.
When the focused window is a shell/terminal, falls back to a running known game
process so opening Palworld still works if the helper console has focus.

Game detection stays stdlib + ctypes only, always available. Media (now-playing)
and notification forwarding use the optional WinRT packages installed by
install-host-helper.ps1 (winrt-Windows.Media.Control /
winrt-Windows.UI.Notifications.Management) — if those are missing or the user
declines the one-time "Notification access" prompt, this script still runs and
still reports the foreground game, it just skips media/notifications.

Windows only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if sys.platform != "win32":
    sys.stderr.write("foreground_reporter.py must run on Windows (Sunshine host).\n")
    sys.exit(1)

import ctypes
from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  (needs sys.path tweak above when double-clicked from elsewhere)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.EnumWindows.argtypes = (ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.Process32NextW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DEFAULT_PORT = common.DEFAULT_PORT
MULTICAST_GROUP = common.MULTICAST_GROUP
SERVICE_NAME = common.SERVICE_NAME
KNOWN_GAME_STEMS = common.KNOWN_GAME_STEMS

BORING_EXE_STEMS = {
    "cmd",
    "powershell",
    "pwsh",
    "windowsterminal",
    "windowsterminal.exe",
    "conhost",
    "openconsole",
    "python",
    "pythonw",
    "py",
    "explorer",
    "searchhost",
    "shellexperiencehost",
    "textinputhost",
    "applicationframehost",
    "foreground_reporter",
    "sunshine",
    "sunshinesvc",
}

BORING_TITLE_FRAGMENTS = (
    "windows powershell",
    "windows terminal",
    "command prompt",
    "administrador: ",
    "turing-host",
    "foreground_reporter",
)

MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


_STATE_LOCK = threading.Lock()
_STATE = {
    "title": "",
    "exe": "",
    "pid": 0,
    "updated_at": 0.0,
}
_LAST_LOG = ""

HOST_ID = common.get_or_create_host_id()
HOSTNAME = os.environ.get("COMPUTERNAME", "") or ""


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _process_image(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _stem(path: str) -> str:
    return Path((path or "").replace("/", "\\")).stem.lower()


def _is_boring(title: str, exe: str) -> bool:
    stem = _stem(exe)
    if stem in BORING_EXE_STEMS:
        return True
    lowered = (title or "").lower()
    return any(frag in lowered for frag in BORING_TITLE_FRAGMENTS)


def _known_game_hit(name: str) -> bool:
    return common.known_game_hit(name)


def _iter_processes():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, INVALID_HANDLE_VALUE, 0, -1):
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return
        while True:
            yield int(entry.th32ProcessID), entry.szExeFile
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)


def _friendly_game_title(title: str, exe: str) -> str:
    """Prefer a stable game label over a truncated window title (Palworld → 'Pal')."""
    stem = _stem(exe)
    # Exact / contained known stems → canonical short name
    aliases = {
        "palworld": "Palworld",
        "palworld-win64-shipping": "Palworld",
        "cs2": "Counter-Strike 2",
        "csgo": "Counter-Strike 2",
        "valorant": "Valorant",
        "valorant-win64-shipping": "Valorant",
        "r5apex": "Apex Legends",
        "fortniteclient-win64-shipping": "Fortnite",
        "rocketleague": "Rocket League",
        "gta5": "GTA V",
        "gtav": "GTA V",
        "rdr2": "Red Dead Redemption 2",
        "eldenring": "Elden Ring",
        "cyberpunk2077": "Cyberpunk 2077",
        "bg3": "Baldur's Gate 3",
        "bg3_dx11": "Baldur's Gate 3",
    }
    if stem in aliases:
        return aliases[stem]
    for key, name in aliases.items():
        if key in stem:
            return name
    raw = (title or "").strip()
    if len(raw) >= 4:
        return raw
    if stem:
        return stem
    return raw


def _find_running_known_game() -> dict | None:
    """Pick a running known game process (largest chance: Palworld while helper has focus)."""
    best = None
    for pid, exe_name in _iter_processes():
        if not _known_game_hit(exe_name):
            continue
        image = _process_image(pid) or exe_name
        # Prefer shipping/client binaries over launchers when both exist
        score = 2 if "shipping" in Path(image).stem.lower() else 1
        candidate = {
            "title": _friendly_game_title("", image),
            "exe": image,
            "pid": pid,
            "score": score,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    if not best:
        return None
    best.pop("score", None)
    return best


def _find_visible_game_window() -> dict | None:
    """Scan top-level windows for a visible known-game process."""
    found: dict | None = None

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _callback(hwnd, _lparam):
        nonlocal found
        if found is not None:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_image(int(pid.value))
        if _is_boring(title, exe):
            return True
        if _known_game_hit(exe) or _known_game_hit(title):
            found = {
                "title": _friendly_game_title(title, exe),
                "exe": exe,
                "pid": int(pid.value),
            }
            return False
        return True

    user32.EnumWindows(_callback, 0)
    return found


def sample_foreground() -> dict:
    hwnd = user32.GetForegroundWindow()
    title = _window_title(hwnd) if hwnd else ""
    pid = wintypes.DWORD(0)
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _process_image(int(pid.value))
    payload = {
        "v": 1,
        "service": SERVICE_NAME,
        "title": title,
        "exe": exe,
        "pid": int(pid.value),
        "updated_at": time.time(),
    }

    # If focus is on the helper/terminal, prefer a running/visible game.
    if _is_boring(title, exe) or (len(title.strip()) < 4 and not _known_game_hit(exe)):
        game = _find_visible_game_window() or _find_running_known_game()
        if game:
            payload.update(game)
            payload["updated_at"] = time.time()
            payload["v"] = 1
            payload["service"] = SERVICE_NAME
    elif _known_game_hit(exe):
        payload["title"] = _friendly_game_title(title, exe)

    return payload


# ---------------------------------------------------------------------------
# Lock state — stdlib + ctypes only, always available (no WinRT dependency).
# ---------------------------------------------------------------------------

DESKTOP_SWITCHDESKTOP = 0x0100
user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
user32.OpenInputDesktop.restype = wintypes.HDESK
user32.CloseDesktop.argtypes = (wintypes.HDESK,)
user32.CloseDesktop.restype = wintypes.BOOL


def sample_lock() -> bool | None:
    """Best-effort: the input desktop can't be opened while the session is locked."""
    try:
        hdesk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    except Exception:
        return None
    if not hdesk:
        return True
    user32.CloseDesktop(hdesk)
    return False


# ---------------------------------------------------------------------------
# Media (now-playing) + notifications — optional WinRT packages.
# `pip install winrt-Windows.Media.Control winrt-Windows.UI.Notifications.Management`
# (installed best-effort by install-host-helper.ps1). Missing/denied → skipped,
# game detection above is unaffected either way.
# ---------------------------------------------------------------------------

_WINRT_MEDIA_AVAILABLE = False
_WINRT_NOTIFICATIONS_AVAILABLE = False
_winrt_media_state: dict = {}
_winrt_media_lock = threading.Lock()
_pending_notifications: list[dict] = []
_pending_notifications_lock = threading.Lock()

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
    )

    _WINRT_MEDIA_AVAILABLE = True
except Exception as exc:  # ImportError or any WinRT init failure
    print(f"(media forwarding disabled: {exc})", flush=True)

try:
    from winrt.windows.ui.notifications.management import UserNotificationListener as _NotifListener
    from winrt.windows.ui.notifications import NotificationKinds as _NotificationKinds

    _WINRT_NOTIFICATIONS_AVAILABLE = True
except Exception as exc:
    print(f"(notification forwarding disabled: {exc})", flush=True)


async def _read_media_session() -> dict | None:
    manager = await _MediaManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None
    try:
        props = await session.try_get_media_properties_async()
    except Exception:
        return None
    timeline = session.get_timeline_properties()
    playback = session.get_playback_info()
    is_playing = bool(playback and playback.playback_status == _PlaybackStatus.PLAYING)
    return {
        "title": str(props.title or ""),
        "artist": str(props.artist or ""),
        "album": str(props.album_title or ""),
        "is_playing": is_playing,
        "position": timeline.position.total_seconds() if timeline else 0.0,
        "length": timeline.end_time.total_seconds() if timeline else 0.0,
        "player_id": "winrt",
    }


def sample_media() -> dict | None:
    if not _WINRT_MEDIA_AVAILABLE:
        return None
    with _winrt_media_lock:
        return dict(_winrt_media_state) if _winrt_media_state else None


def _media_poll_loop(interval: float) -> None:
    """Dedicated asyncio loop for the WinRT media API (own thread — avoids mixing
    asyncio with the plain synchronous polling loop below)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            info = loop.run_until_complete(_read_media_session())
        except Exception as exc:
            print(f"(media poll error: {exc})", flush=True)
            info = None
        with _winrt_media_lock:
            _winrt_media_state.clear()
            if info:
                _winrt_media_state.update(info)
        time.sleep(max(0.5, interval))


def _notification_to_text(notification) -> tuple[str, str, str]:
    app_name = ""
    try:
        app_name = str(notification.app_info.display_info.display_name or "")
    except Exception:
        pass
    title, body = "", ""
    try:
        toast_binding = notification.notification.visual.get_binding(
            "ToastGeneric"
        ) if notification.notification and notification.notification.visual else None
        if toast_binding is not None:
            texts = list(toast_binding.get_text_elements())
            if texts:
                title = str(texts[0].text or "")
            if len(texts) > 1:
                body = " ".join(str(t.text or "") for t in texts[1:]).strip()
    except Exception:
        pass
    return app_name, title, body


async def _notification_listener_loop() -> None:
    listener = _NotifListener.get_current()
    access = await listener.request_access_async()
    if str(access) != "Allowed" and int(access) != 1:
        print("(notification access not granted — run again and click Allow)", flush=True)
        return
    seen_ids: set[int] = set()
    while True:
        try:
            notifications = await listener.get_notifications_async(_NotificationKinds.TOAST)
            for note in notifications:
                note_id = int(note.id)
                if note_id in seen_ids:
                    continue
                seen_ids.add(note_id)
                if len(seen_ids) > 200:
                    seen_ids.clear()
                app, title, body = _notification_to_text(note)
                if not (title or body):
                    continue
                with _pending_notifications_lock:
                    _pending_notifications.append({"app": app, "title": title, "body": body})
        except Exception as exc:
            print(f"(notification poll error: {exc})", flush=True)
        await asyncio.sleep(1.0)


def _notification_listener_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_notification_listener_loop())
    except Exception as exc:
        print(f"(notification listener stopped: {exc})", flush=True)


def _pop_pending_notifications() -> list[dict]:
    with _pending_notifications_lock:
        items, _pending_notifications[:] = list(_pending_notifications), []
    return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        import json

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health"):
            self._send_json({"ok": True, "service": SERVICE_NAME, "mode": "broadcast"})
            return
        if path == "/foreground":
            with _STATE_LOCK:
                payload = dict(_STATE)
            self._send_json(payload)
            return
        self._send_json({"error": "not found"}, status=404)


def poll_and_announce(interval: float, port: int) -> None:
    global _LAST_LOG
    sock = common.make_udp_socket()
    while True:
        try:
            game_sample = sample_foreground()
            with _STATE_LOCK:
                _STATE.update(game_sample)

            game = None
            if game_sample.get("title") or game_sample.get("exe"):
                game = {
                    "title": game_sample.get("title", ""),
                    "exe": game_sample.get("exe", ""),
                    "pid": game_sample.get("pid", 0),
                }
            media = sample_media()
            lock_state = sample_lock()
            lock = {"is_locked": lock_state} if lock_state is not None else None

            payload = common.build_state_payload(HOST_ID, HOSTNAME, game=game, media=media, lock=lock)
            common.send_payload(sock, payload, port)

            for note in _pop_pending_notifications():
                note_payload = common.build_notification_payload(
                    HOST_ID, HOSTNAME, note["app"] or "Windows", note["title"], note["body"]
                )
                common.send_payload(sock, note_payload, port)

            label = (game or {}).get("title") or Path(str((game or {}).get("exe") or "")).stem or "?"
            if label != _LAST_LOG:
                _LAST_LOG = label
                print(f"announcing: {label}", flush=True)
        except Exception as exc:
            print(f"sample error: {exc}", flush=True)
        time.sleep(max(0.2, interval))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turing host game helper — auto-announces game/media/lock/notifications on the LAN"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP/HTTP port (default {DEFAULT_PORT})")
    parser.add_argument("--interval", type=float, default=1.0, help="Sample/announce interval seconds")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Also serve HTTP GET /foreground (optional)",
    )
    parser.add_argument("--http-host", default="0.0.0.0", help="HTTP bind address when --http is set")
    args = parser.parse_args()

    # Avoid reporting this helper console as the game when possible.
    os.environ.setdefault("TURING_HOST_HELPER", "1")

    sample = sample_foreground()
    with _STATE_LOCK:
        _STATE.update(sample)

    threading.Thread(
        target=poll_and_announce,
        args=(args.interval, args.port),
        name="turing-host-announce",
        daemon=True,
    ).start()

    if _WINRT_MEDIA_AVAILABLE:
        threading.Thread(
            target=_media_poll_loop, args=(args.interval,), name="turing-host-media", daemon=True
        ).start()

    if _WINRT_NOTIFICATIONS_AVAILABLE:
        threading.Thread(
            target=_notification_listener_thread, name="turing-host-notifications", daemon=True
        ).start()

    print(
        f"turing-host-game-helper announcing on UDP {MULTICAST_GROUP}:{args.port} "
        f"+ broadcast :{args.port} (host_id={HOST_ID[:8]}...)",
        flush=True,
    )
    print("Keep this running. Open your game (e.g. Palworld) — no Mini-PC config needed.", flush=True)
    print(f"now: {sample.get('title') or Path(str(sample.get('exe') or '')).stem}", flush=True)
    print(
        f"media forwarding: {'on' if _WINRT_MEDIA_AVAILABLE else 'off (winrt package missing)'}; "
        f"notification forwarding: {'on' if _WINRT_NOTIFICATIONS_AVAILABLE else 'off (winrt package missing)'}",
        flush=True,
    )

    if args.http:
        server = ThreadingHTTPServer((args.http_host, args.port), Handler)
        print(f"Optional HTTP debug: http://{args.http_host}:{args.port}/foreground", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.", flush=True)
        finally:
            server.server_close()
        return 0

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
