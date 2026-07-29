#!/usr/bin/env python3
"""Announce the active game on the Windows host to the Turing clock on the LAN.

Zero-config for the Mini-PC: broadcasts UDP; the clock listens automatically.
When the focused window is a shell/terminal, falls back to a running known game
process so opening Palworld still works if the helper console has focus.

Stdlib + ctypes only. Windows only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
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

DEFAULT_PORT = 8787
MULTICAST_GROUP = "239.255.87.87"
SERVICE_NAME = "turing-host-game"

# Prefer these when the focused window is a shell / helper UI.
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
    stem = Path(name).stem.lower()
    if stem in KNOWN_GAME_STEMS:
        return True
    return any(key in stem for key in KNOWN_GAME_STEMS if len(key) >= 4)


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


def _make_udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    return sock


def announce(sock: socket.socket, payload: dict, port: int) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        sock.sendto(raw, ("255.255.255.255", port))
    except OSError:
        pass
    try:
        sock.sendto(raw, (MULTICAST_GROUP, port))
    except OSError:
        pass


def poll_and_announce(interval: float, port: int) -> None:
    global _LAST_LOG
    sock = _make_udp_socket()
    while True:
        try:
            sample = sample_foreground()
            with _STATE_LOCK:
                _STATE.update(sample)
            announce(sock, sample, port)
            label = sample.get("title") or Path(str(sample.get("exe") or "")).stem or "?"
            if label != _LAST_LOG:
                _LAST_LOG = label
                print(f"announcing: {label}", flush=True)
        except Exception as exc:
            print(f"sample error: {exc}", flush=True)
        time.sleep(max(0.2, interval))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turing host game helper — auto-announces active game on the LAN"
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

    thread = threading.Thread(
        target=poll_and_announce,
        args=(args.interval, args.port),
        name="turing-host-announce",
        daemon=True,
    )
    thread.start()

    print(
        f"turing-host-game-helper announcing on UDP {MULTICAST_GROUP}:{args.port} "
        f"+ broadcast :{args.port}",
        flush=True,
    )
    print("Keep this running. Open your game (e.g. Palworld) — no Mini-PC config needed.", flush=True)
    print(f"now: {sample.get('title') or Path(str(sample.get('exe') or '')).stem}", flush=True)

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
