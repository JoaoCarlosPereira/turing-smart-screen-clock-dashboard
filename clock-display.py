#!/usr/bin/env python3
"""Tech dashboard with Pop!_OS/COSMIC notification mirroring and multi-mode support.

Modes:
  - MAIN (default): Clock, date, weather (SMO/SC) and notification dashboard
  - MULTIMEDIA:     Spotify-like media info display (auto-detected via MPRIS2)
  - GAMER:          Gaming overlay with game info, FPS, hardware stats (auto-detected)
  - LOCKED:         Session lock — time + lock icon at 10% brightness (OS theme)

Usage:
  python clock-display.py                    # Auto-detect mode
  python clock-display.py --mode main        # Force MAIN mode
  python clock-display.py --mode multimedia  # Force MULTIMEDIA mode
  python clock-display.py --mode gamer       # Force GAMER mode
  python clock-display.py --mode locked      # Force LOCKED mode
"""

import argparse
import hashlib
import html
import io
import json
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import serial
from PIL import Image, ImageDraw, ImageFont

from library.lcd.lcd_comm import LcdComm
from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation
from library.log import logger

# Import mode system
from modes import Mode, ModeManager, MultimediaInfo, GamerInfo, LockInfo  # noqa: F401
from modes import render_multimedia_mode, render_gamer_mode, render_locked_mode  # noqa: F401
from modes import _cover_fit, _extract_theme_colors  # noqa: F401


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Turing Smart Screen — Clock Dashboard with Multi-Modes")
    parser.add_argument(
        "--mode",
        choices=["main", "multimedia", "gamer", "locked"],
        default=None,
        help="Force a display mode. If omitted, auto-detection is used.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Constants (unchanged from original)
# ---------------------------------------------------------------------------

WIDTH, HEIGHT = 480, 320
BG = (4, 8, 16)
PANEL = (11, 20, 34)
PANEL_LIGHT = (15, 28, 46)
CYAN = (56, 214, 255)
BLUE = (78, 132, 255)
MAGENTA = (214, 84, 255)
WHITE = (236, 244, 255)
MUTED = (126, 150, 176)
GREEN = (78, 232, 168)
ERROR_RED = (255, 72, 96)
ERROR_AMBER = (255, 176, 64)
GRID = (10, 20, 34)
ACCENT_LINE = (24, 48, 72)
FONT_REGULAR = "res/fonts/roboto/Roboto-Regular.ttf"
FONT_MEDIUM = "res/fonts/roboto/Roboto-Medium.ttf"
FONT_BOLD = "res/fonts/roboto/Roboto-Bold.ttf"
FONT_MONO = "res/fonts/roboto-mono/RobotoMono-Bold.ttf"
NOTIFICATION_SECONDS = 5
NOTIFICATION_RETENTION_SECONDS = 15 * 60  # clear dashboard note after 15 min idle
BRIGHTNESS = 100
LOCKED_BRIGHTNESS = 10
ACTIVE_BRIGHTNESS = BRIGHTNESS
ORIENTATION = Orientation.REVERSE_LANDSCAPE
SERIAL_WRITE_TIMEOUT = 2
RECOVER_SLEEP_SECONDS = 3
WATCHDOG_SECONDS = 12  # no successful frame → force recovery
RECOVERY_COOLDOWN_SECONDS = 5
SOFT_NUDGE_SECONDS = 120  # soft wake only; avoid hammering Rev A
HARD_NUDGE_EVERY = 0  # disabled: periodic hard Reset was corrupting a healthy panel
# After cold boot the first Reset often "succeeds" while the panel stays blank.
# Only run a second Reset if we still have no successful frames.
BOOT_CONFIRM_HARD_SECONDS = 20
# Region refreshed every second in LOCKED mode (must match clock placement)
LOCKED_CLOCK_CROP = (40, 140, 440, 230)

running = True
notification_queue = queue.Queue()
monitor_process = None
last_successful_frame_at = 0.0
last_recovery_at = 0.0
last_soft_nudge_at = 0.0
recovery_hard_next = False
# Collapse FDO Notify doubles + portal→impl→gtk fan-out of the same toast.
_NOTIFICATION_DEDUP_SECONDS = 2.5
_recent_notifications: deque[tuple[float, str, str]] = deque(maxlen=64)


class ResilientLcd(LcdCommRevA):
    """LcdCommRevA with write timeouts and RTS/CTS unstick recovery."""

    def __init__(self, com_port: str = "AUTO", display_width: int = 320, display_height: int = 480):
        logger.debug("HW revision: A")
        LcdComm.__init__(self, com_port, display_width, display_height, update_queue=None)

    def closeSerial(self):
        if self.lcd_serial is None:
            return
        try:
            self.lcd_serial.close()
        except Exception as exc:
            logger.debug("Serial close ignored: %s", exc)
        finally:
            self.lcd_serial = None

    def unstick(self, port: str | None = None):
        """Toggle DTR/RTS without hardware flow control to clear a wedged CDC ACM port."""
        target = port
        if not target or target == "AUTO":
            target = self.auto_detect_com_port()
        if not target:
            return
        try:
            probe = serial.Serial(
                target,
                115200,
                timeout=1,
                write_timeout=SERIAL_WRITE_TIMEOUT,
                rtscts=False,
                dsrdtr=False,
            )
            probe.dtr = False
            probe.rts = False
            time.sleep(0.2)
            probe.dtr = True
            probe.rts = True
            time.sleep(0.3)
            probe.close()
            logger.debug("Serial unstick applied on %s", target)
        except Exception as exc:
            logger.debug("Serial unstick skipped on %s: %s", target, exc)

    def openSerial(self):
        if self.com_port == "AUTO":
            detected = self.auto_detect_com_port()
            if not detected:
                raise serial.SerialException("Display COM port not found")
            self.com_port = detected
            logger.debug("Auto detected COM port: %s", self.com_port)
        else:
            logger.debug("Static COM port: %s", self.com_port)

        self.unstick(self.com_port)
        try:
            self.lcd_serial = serial.Serial(
                self.com_port,
                115200,
                timeout=1,
                write_timeout=SERIAL_WRITE_TIMEOUT,
                rtscts=True,
            )
        except Exception as exc:
            raise serial.SerialException(f"Cannot open COM port {self.com_port}: {exc}") from exc

    def WriteLine(self, line: bytes):
        try:
            self.serial_write(line)
        except serial.SerialTimeoutException:
            logger.warning("Serial write timed out — display may be frozen")
            raise
        except serial.SerialException:
            logger.error("SerialException while writing to display")
            raise

    def _wake_panel(self, *, set_orientation: bool = True):
        """Rev A often accepts serial writes while the panel stays dark — force on.

        Avoid repeating SetOrientation on periodic nudges: it can scramble the
        Rev A framebuffer into a frozen/garbled image while serial still "works".
        """
        try:
            self.ScreenOn()
        except Exception as exc:
            logger.debug("ScreenOn ignored: %s", exc)
        self.SetBrightness(level=ACTIVE_BRIGHTNESS)
        if set_orientation:
            self.SetOrientation(orientation=ORIENTATION)

    def soft_bring_up(self):
        """Reconnect without hardware Reset (gentler; preferred for recovery)."""
        global last_successful_frame_at
        logger.info("Soft reconnecting Turing display...")
        self.closeSerial()
        self.com_port = "AUTO"
        time.sleep(0.3)
        self.openSerial()
        self.InitializeComm()
        self._wake_panel()
        last_successful_frame_at = time.monotonic()
        logger.info("Display soft-ready on %s", self.com_port)

    def hard_bring_up(self):
        """Full reconnect with Reset (ttyACM* often renumbers afterwards)."""
        global last_successful_frame_at
        logger.info("Hard resetting Turing display...")
        self.closeSerial()
        self.com_port = "AUTO"
        time.sleep(0.4)

        self.openSerial()
        try:
            self.Reset()
        except Exception as exc:
            logger.debug("Reset raised (%s); continuing with re-detect", exc)

        self.closeSerial()
        self.com_port = "AUTO"
        deadline = time.monotonic() + 12
        last_err = None
        while time.monotonic() < deadline:
            try:
                self.openSerial()
                self.InitializeComm()
                self._wake_panel()
                last_successful_frame_at = time.monotonic()
                logger.info("Display hard-ready on %s", self.com_port)
                return
            except Exception as exc:
                last_err = exc
                self.closeSerial()
                self.com_port = "AUTO"
                time.sleep(0.6)
        raise serial.SerialException(f"Display hard bring-up failed: {last_err}")

    def bring_up(self):
        """Try soft reconnect first; fall back to hard Reset."""
        global recovery_hard_next
        if recovery_hard_next:
            recovery_hard_next = False
            self.hard_bring_up()
            return
        try:
            self.soft_bring_up()
        except Exception as soft_exc:
            logger.warning("Soft reconnect failed (%s); trying hard reset", soft_exc)
            self.hard_bring_up()


@dataclass
class Notification:
    app: str
    icon: str
    title: str
    body: str
    received_at: datetime


# ---------------------------------------------------------------------------
# Weather (Open-Meteo) — São Miguel do Oeste / SC (location not shown on UI)
# ---------------------------------------------------------------------------

WEATHER_LAT = -26.7253
WEATHER_LON = -53.5184
WEATHER_CACHE_SECONDS = 12 * 60  # refresh ~every 12 minutes
WEATHER_TIMEOUT_SECONDS = 2.5
RAIN_BLUE = (72, 168, 255)

_WEATHER_CACHE: Optional["WeatherInfo"] = None
_WEATHER_FETCHED_AT = 0.0

# WMO weather interpretation codes → short PT labels
_WEATHER_CODE_PT = {
    0: "Céu limpo",
    1: "Principalmente limpo",
    2: "Parcialmente nublado",
    3: "Nublado",
    45: "Neblina",
    48: "Neblina gelada",
    51: "Garoa fraca",
    53: "Garoa",
    55: "Garoa forte",
    56: "Garoa gelada",
    57: "Garoa gelada",
    61: "Chuva fraca",
    63: "Chuva",
    65: "Chuva forte",
    66: "Chuva gelada",
    67: "Chuva gelada",
    71: "Neve fraca",
    73: "Neve",
    75: "Neve forte",
    77: "Grãos de neve",
    80: "Pancadas fracas",
    81: "Pancadas",
    82: "Pancadas fortes",
    85: "Pancadas de neve",
    86: "Pancadas de neve",
    95: "Tempestade",
    96: "Tempestade com granizo",
    99: "Tempestade com granizo",
}


@dataclass
class WeatherInfo:
    temperature: float = 0.0
    weather_code: int = 0
    humidity: float = 0.0
    wind_kmh: float = 0.0
    temp_max: float = 0.0
    temp_min: float = 0.0
    precip_prob: float = 0.0
    condition: str = ""
    hourly_precip_prob: list[float] = field(default_factory=list)  # 24 values, local hours 0–23
    fetched_at: float = 0.0
    ok: bool = False


def weather_condition_pt(code: int) -> str:
    return _WEATHER_CODE_PT.get(int(code), "Condição desconhecida")


def remaining_day_precip_prob(hourly: list[float], now_hour: int, day_max: float) -> float:
    """Daily rain % still ahead: day_max scaled by remaining hourly probability mass.

    Example: day_max=100, 70% of the day's precip-mass already passed → ~30%.
    Current hour counts as remaining.
    """
    day_max = max(0.0, min(100.0, float(day_max or 0.0)))
    now_hour = max(0, min(23, int(now_hour)))
    vals: list[float] = []
    for i in range(24):
        try:
            vals.append(max(0.0, float(hourly[i] or 0.0)))
        except (IndexError, TypeError, ValueError):
            vals.append(0.0)
    past = sum(vals[:now_hour])
    rest = sum(vals[now_hour:])
    total = past + rest
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, day_max * (rest / total)))


def fetch_weather(force: bool = False) -> Optional[WeatherInfo]:
    """Fetch/cached Open-Meteo conditions for São Miguel do Oeste - SC."""
    global _WEATHER_CACHE, _WEATHER_FETCHED_AT
    now_mono = time.monotonic()
    if (
        not force
        and _WEATHER_CACHE is not None
        and _WEATHER_CACHE.ok
        and (now_mono - _WEATHER_FETCHED_AT) < WEATHER_CACHE_SECONDS
    ):
        return _WEATHER_CACHE

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        "&hourly=precipitation_probability"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=America%2FSao_Paulo"
        "&forecast_days=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "turing-clock/1.0"})
        with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        current = payload.get("current") or {}
        daily = payload.get("daily") or {}
        hourly = payload.get("hourly") or {}
        code = int(current.get("weather_code") or 0)
        raw_probs = hourly.get("precipitation_probability") or []
        hourly_probs: list[float] = []
        for i in range(24):
            try:
                hourly_probs.append(float(raw_probs[i] or 0.0))
            except (IndexError, TypeError, ValueError):
                hourly_probs.append(0.0)
        info = WeatherInfo(
            temperature=float(current.get("temperature_2m") or 0.0),
            weather_code=code,
            humidity=float(current.get("relative_humidity_2m") or 0.0),
            wind_kmh=float(current.get("wind_speed_10m") or 0.0),
            temp_max=float((daily.get("temperature_2m_max") or [0.0])[0] or 0.0),
            temp_min=float((daily.get("temperature_2m_min") or [0.0])[0] or 0.0),
            precip_prob=float((daily.get("precipitation_probability_max") or [0.0])[0] or 0.0),
            condition=weather_condition_pt(code),
            hourly_precip_prob=hourly_probs,
            fetched_at=now_mono,
            ok=True,
        )
        _WEATHER_CACHE = info
        _WEATHER_FETCHED_AT = now_mono
        return info
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logger.debug("Open-Meteo weather fetch failed: %s", exc)
        if _WEATHER_CACHE is not None and _WEATHER_CACHE.ok:
            return _WEATHER_CACHE
        return None


def _draw_weather_icon(draw, box, code: int, color):
    """Bold geometric weather glyph — readable at ~44px on IPS."""
    x1, y1, x2, y2 = box
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    code = int(code)
    span = max(28, min(x2 - x1, y2 - y1))
    s = span / 96.0
    w = max(2, int(3 * s))
    sun = (255, 196, 72)
    cloud = color
    drop = RAIN_BLUE

    def _xy(dx, dy):
        return (cx + int(dx * s), cy + int(dy * s))

    def _sun(ox=0, oy=0, r=16):
        draw.ellipse((*_xy(ox - r, oy - r), *_xy(ox + r, oy + r)), fill=sun)
        for dx, dy in ((0, -26), (0, 26), (-26, 0), (26, 0), (-18, -18), (18, -18), (-18, 18), (18, 18)):
            draw.line((*_xy(ox + dx / 2.4, oy + dy / 2.4), *_xy(ox + dx, oy + dy)), fill=sun, width=w)

    def _cloud(ox=0, oy=2):
        draw.ellipse((*_xy(ox - 22, oy - 12), *_xy(ox + 2, oy + 10)), fill=cloud)
        draw.ellipse((*_xy(ox - 6, oy - 18), *_xy(ox + 22, oy + 8)), fill=cloud)
        draw.rounded_rectangle((*_xy(ox - 20, oy - 2), *_xy(ox + 20, oy + 14)), radius=max(4, int(7 * s)), fill=cloud)

    if code == 0:
        _sun()
        return
    if code in (1, 2):
        _sun(ox=-10, oy=-10, r=12)
        _cloud(ox=4, oy=6)
        return
    if code in (3, 45, 48):
        _cloud()
        return
    if code >= 95:
        _cloud(oy=-4)
        bolt = [_xy(-2, -2), _xy(10, -2), _xy(2, 10), _xy(12, 10), _xy(-8, 28), _xy(0, 10), _xy(-6, 10)]
        draw.polygon(bolt, fill=ERROR_AMBER)
        return
    # rain / drizzle / showers / snow
    _cloud(oy=-6)
    for ox in (-10, 2, 14):
        draw.line((*_xy(ox, 12), *_xy(ox - 3, 24)), fill=drop, width=max(2, w))


def _rain_bar_color(prob: float, is_now: bool):
    """Yellow bars for the day; only the current hour is blue."""
    if is_now:
        return RAIN_BLUE
    if prob <= 0:
        return (72, 56, 28)
    return ERROR_AMBER


def _text_with_shadow(draw, xy, text, font, fill, anchor=None, shadow=(0, 0, 0)):
    """Light dark halo so glyphs stay readable over wallpaper / bars."""
    x, y = xy
    kwargs = {}
    if anchor is not None:
        kwargs["anchor"] = anchor
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1)):
        draw.text((x + dx, y + dy), text, font=font, fill=shadow, **kwargs)
    draw.text((x, y), text, font=font, fill=fill, **kwargs)


def _draw_rain_timeline(draw, box, probs: list[float], now_hour: int, accent, label_font, axis_font):
    """Day rain strip: 12×2h bars labeled 00/02/…/22 with a now marker."""
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1:
        return

    hours = 12
    slots: list[float] = []
    for i in range(hours):
        vals = []
        for h in (i * 2, i * 2 + 1):
            try:
                vals.append(float(probs[h] or 0.0))
            except (IndexError, TypeError, ValueError):
                vals.append(0.0)
        slots.append(max(vals) if vals else 0.0)

    header_h = 18
    axis_h = 26
    chart_top = y1 + header_h
    chart_bottom = y2 - axis_h
    chart_h = max(10, chart_bottom - chart_top)

    # Soft plate behind title + axis so wallpaper doesn't wash out the labels
    draw.rounded_rectangle((x1 - 4, y1 - 4, x2 + 4, chart_top - 2), radius=6, fill=(8, 10, 14))
    draw.rounded_rectangle((x1 - 4, chart_bottom + 2, x2 + 4, y2 + 2), radius=6, fill=(8, 10, 14))

    _text_with_shadow(draw, (x1, y1 - 1), "Chuva", label_font, WHITE)

    gap = 3
    usable = max(hours, x2 - x1)
    bar_w = max(8, (usable - gap * (hours - 1)) // hours)
    total_w = hours * bar_w + (hours - 1) * gap
    ox = x1 + max(0, (usable - total_w) // 2)
    now_hour = max(0, min(23, int(now_hour)))
    now_slot = now_hour // 2

    draw.rectangle((ox, chart_bottom - 1, ox + total_w - 1, chart_bottom), fill=(40, 48, 60))

    for i, p in enumerate(slots):
        is_now = i == now_slot
        h = max(3, int(chart_h * (p / 100.0))) if p > 0 else 2
        bx1 = ox + i * (bar_w + gap)
        bx2 = bx1 + bar_w - 1
        by1 = chart_bottom - h
        color = _rain_bar_color(p, is_now)
        draw.rounded_rectangle((bx1, by1, bx2, chart_bottom), radius=2, fill=color)
        if is_now:
            draw.rectangle((bx1, chart_top, bx2, chart_top + 2), fill=RAIN_BLUE)
            draw.rectangle((bx1, chart_bottom + 1, bx2, chart_bottom + 2), fill=RAIN_BLUE)

        cx = bx1 + bar_w // 2
        hour_color = RAIN_BLUE if is_now else WHITE
        _text_with_shadow(draw, (cx, y2), f"{i * 2:02d}", axis_font, hour_color, anchor="mb")


ICON_CACHE: dict[tuple[str, str, int], Image.Image | None] = {}
ICON_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".webp", ".xpm", ".gif")
ICON_SIZES = ("128x128", "96x96", "64x64", "48x48", "32x32", "256x256", "scalable")
ICON_CATEGORIES = ("apps", "status", "devices", "mimetypes", "places", "actions")
ICON_ROOTS = (
    Path.home() / ".local/share/icons",
    Path.home() / ".icons",
    Path.home() / ".local/share/flatpak/exports/share/icons",
    Path("/var/lib/flatpak/exports/share/icons"),
    Path("/var/lib/snapd/desktop/icons"),
    Path("/usr/share/icons/Pop"),
    Path("/usr/share/icons/Cosmic"),
    Path("/usr/share/icons/hicolor"),
    Path("/usr/share/icons"),
    Path("/usr/share/pixmaps"),
)
DESKTOP_DIRS = (
    Path.home() / ".local/share/applications",
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/usr/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
)
# Chrome/Firefox delete scoped temp icons quickly — keep a local copy.
NOTIF_ICON_CACHE_DIR = Path.home() / ".cache" / "turing-clock" / "notif-icons"
BROWSER_APP_NAMES = (
    "google chrome",
    "chrome",
    "chromium",
    "chromium-browser",
    "firefox",
    "firefox web browser",
    "brave",
    "brave-browser",
    "microsoft-edge",
    "microsoft edge",
    "opera",
    "vivaldi",
)


def font(path, size):
    return ImageFont.truetype(path, size)


def clean_markup(value):
    value = re.sub(r"<[^>]*>", "", value or "")
    value = html.unescape(value)
    return " ".join(value.split())


def clean_notification_body(value):
    value = clean_markup(value)
    return re.sub(
        r"^(?:https?://)?(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )


def is_whatsapp_notification(app: str = "", title: str = "", body: str = "", *extra: str) -> bool:
    """Detect native WhatsApp apps and WhatsApp Web (Chrome/Chromium notifications)."""
    blob = " ".join([app or "", title or "", body or "", *[e or "" for e in extra]]).lower()
    return "whatsapp" in blob or "web.whatsapp.com" in blob


def redact_whatsapp_notification(
    app: str,
    title: str,
    body: str,
    *extra: str,
) -> tuple[str, str, str]:
    """Hide WhatsApp / WhatsApp Web message text; keep only who notified.

    Chrome notifications often arrive as app=Google Chrome with
    body starting with web.whatsapp.com — still redact those.
    Returns (display_app, who, generic_body).
    """
    if not is_whatsapp_notification(app, title, body, *extra):
        return app, title, body

    who = clean_markup(title).strip()
    generic = {
        "",
        "whatsapp",
        "whatsapp web",
        "whatsapp desktop",
        "google chrome",
        "chrome",
        "chromium",
        "chromium-browser",
    }
    if who.lower() in generic:
        cleaned = clean_notification_body(body)
        # Group-style body: "Alice: olá…" — keep only the sender prefix
        if ":" in cleaned:
            sender = cleaned.split(":", 1)[0].strip()
            if (
                sender
                and len(sender) <= 80
                and "\n" not in sender
                and "whatsapp" not in sender.lower()
            ):
                who = sender
        if who.lower() in generic:
            who = "WhatsApp"

    display_app = app
    if _is_browser_app(app) or "whatsapp" not in (app or "").lower():
        display_app = "WhatsApp"
    return display_app, who, "Nova notificação"


def dbus_unescape(value):
    return value.replace(r"\n", "\n").replace(r'\"', '"')


def _normalize_icon_ref(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("file://"):
        return unquote(urlparse(value).path)
    return value


def _is_browser_app(app: str) -> bool:
    name = (app or "").strip().lower()
    return name in BROWSER_APP_NAMES or any(name.startswith(b) for b in BROWSER_APP_NAMES)


def _persist_notification_icon(path_ref: str) -> str:
    """Copy temp browser icons into a stable cache before Chrome deletes them."""
    raw = _normalize_icon_ref(path_ref)
    if not raw:
        return ""
    src = Path(raw)
    try:
        if src.resolve().is_relative_to(NOTIF_ICON_CACHE_DIR.resolve()):
            return str(src)
    except (OSError, ValueError):
        pass
    if not src.is_file():
        return raw
    try:
        NOTIF_ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()[:16]
        dest = NOTIF_ICON_CACHE_DIR / f"{digest}{src.suffix.lower() or '.png'}"
        if not dest.is_file():
            shutil.copy2(src, dest)
        return str(dest)
    except OSError as exc:
        logger.warning("Could not persist notification icon %s: %s", src, exc)
        return raw


def _prefer_site_icon(app_icon: str, image_path: str = "") -> str:
    """Chrome puts product logo in app_icon and the site favicon in image-path."""
    for candidate in (image_path, app_icon):
        normalized = _normalize_icon_ref(candidate)
        if not normalized:
            continue
        path = Path(normalized)
        if path.is_file():
            # Prefer sibling icon.png when app_icon is Chrome's logo.png
            if path.name.lower() == "logo.png":
                sibling = path.with_name("icon.png")
                if sibling.is_file():
                    return _persist_notification_icon(str(sibling))
            return _persist_notification_icon(str(path))
        if candidate and not candidate.startswith("file://") and "/" not in candidate:
            # Theme icon name — keep as-is for later resolution
            if image_path and candidate == _normalize_icon_ref(app_icon):
                continue
            return candidate
    if image_path:
        return _persist_notification_icon(image_path)
    return _persist_notification_icon(app_icon) if app_icon else ""


def _desktop_icon_name(app_name: str) -> str | None:
    needle = (app_name or "").strip().lower()
    if not needle:
        return None
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for desktop in directory.glob("*.desktop"):
            try:
                text = desktop.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            icon_name = None
            names = {desktop.stem.lower()}
            for line in text.splitlines():
                if line.startswith("Name="):
                    names.add(line.split("=", 1)[1].strip().lower())
                elif line.startswith("Icon="):
                    icon_name = line.split("=", 1)[1].strip()
                elif line.startswith("StartupWMClass="):
                    names.add(line.split("=", 1)[1].strip().lower())
            if not icon_name:
                continue
            if needle in names or needle == desktop.stem.lower():
                return icon_name
    return None


def _find_icon_in_roots(name: str) -> Path | None:
    base = Path(name).name
    stem = Path(base).stem if Path(base).suffix.lower() in ICON_EXTS else base
    candidates = []
    if Path(base).suffix.lower() in ICON_EXTS:
        candidates.append(base)
    for ext in ICON_EXTS:
        candidates.append(f"{stem}{ext}")

    roots: list[Path] = []
    for root in ICON_ROOTS:
        if not root.exists():
            continue
        roots.append(root)
        if root.name == "icons":
            roots.extend(path for path in root.iterdir() if path.is_dir())

    for root in roots:
        for candidate in candidates:
            direct = root / candidate
            if direct.is_file():
                return direct
        for size in ICON_SIZES:
            for category in ICON_CATEGORIES:
                for candidate in candidates:
                    path = root / size / category / candidate
                    if path.is_file():
                        return path
    return None


def _lookup_icon_with_gtk(name: str, size: int) -> Path | None:
    script = (
        "import sys\n"
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "from gi.repository import Gtk\n"
        "theme = Gtk.IconTheme.get_default()\n"
        "info = theme.lookup_icon(sys.argv[1], int(sys.argv[2]), 0)\n"
        "print(info.get_filename() if info else '')\n"
    )
    try:
        output = subprocess.check_output(
            ["python3", "-c", script, name, str(size)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return Path(output) if output else None


def resolve_icon_path(icon: str, app: str = "") -> Path | None:
    """Resolve icon file. For browsers, avoid falling back to the browser app icon."""
    candidates = [_normalize_icon_ref(icon)]
    if not _is_browser_app(app):
        candidates.append(_normalize_icon_ref(app))
        candidates.append(_desktop_icon_name(app) or "")
    elif not icon:
        # No site icon available — only then use browser branding
        candidates.append(_normalize_icon_ref(app))
        candidates.append(_desktop_icon_name(app) or "")

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
        found = _find_icon_in_roots(candidate) or _lookup_icon_with_gtk(Path(candidate).stem, 64)
        if found:
            return found
    return None


def _load_svg_png_bytes(path: Path, size: int) -> bytes | None:
    script = (
        "import sys\n"
        "import gi\n"
        "gi.require_version('GdkPixbuf', '2.0')\n"
        "from gi.repository import GdkPixbuf\n"
        "pb = GdkPixbuf.Pixbuf.new_from_file_at_size(sys.argv[1], int(sys.argv[2]), int(sys.argv[2]))\n"
        "ok, data = pb.save_to_bufferv('png', [], [])\n"
        "sys.stdout.buffer.write(data)\n"
    )
    try:
        return subprocess.check_output(
            ["python3", "-c", script, str(path), str(size)],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def load_notification_icon(icon: str, app: str, size: int) -> Image.Image | None:
    key = (icon or "", app or "", size)
    if key in ICON_CACHE:
        cached = ICON_CACHE[key]
        return cached.copy() if cached is not None else None

    path = resolve_icon_path(icon, app)
    image = None
    if path is not None:
        try:
            if path.suffix.lower() == ".svg":
                png = _load_svg_png_bytes(path, size)
                if png:
                    image = Image.open(io.BytesIO(png)).convert("RGBA")
            else:
                image = Image.open(path).convert("RGBA")
            if image is not None:
                image = image.resize((size, size), Image.Resampling.LANCZOS)
        except OSError as exc:
            logger.debug("Failed to load icon %s: %s", path, exc)
            image = None

    ICON_CACHE[key] = image.copy() if image is not None else None
    return image.copy() if image is not None else None


def paste_rounded_icon(base: Image.Image, icon: Image.Image, box, radius: int):
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    fitted = icon.resize((width, height), Image.Resampling.LANCZOS)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    base.paste(fitted, (x1, y1), mask)


def wrap_text(draw, value, selected_font, max_width, max_lines):
    words = clean_markup(value).split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=selected_font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(lines)) < len(" ".join(words)):
        while lines[-1] and draw.textlength(lines[-1] + "…", font=selected_font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


# ---------------------------------------------------------------------------
# System theme (COSMIC / Pop!_OS wallpaper + accent colors)
# ---------------------------------------------------------------------------

_SYSTEM_THEME_CACHE: dict | None = None
_SYSTEM_THEME_FINGERPRINT = ""
THEME_WATCH_SECONDS = 2.0  # how often MAIN polls desktop environment changes


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) >= 8:
        raw = raw[:6]
    if len(raw) != 6:
        return CYAN
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _cosmic_is_dark() -> bool:
    path = Path.home() / ".config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark"
    try:
        return path.read_text(encoding="utf-8").strip().lower() != "false"
    except OSError:
        return True


def _cosmic_accent_hex() -> str | None:
    theme = "Dark" if _cosmic_is_dark() else "Light"
    path = Path.home() / f".config/cosmic/com.system76.CosmicTheme.{theme}/v2/accent"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'base:\s*"#([0-9A-Fa-f]{6,8})"', text)
    return match.group(1) if match else None


def _cosmic_panel_hex() -> str | None:
    theme = "Dark" if _cosmic_is_dark() else "Light"
    path = Path.home() / f".config/cosmic/com.system76.CosmicTheme.{theme}/v2/background"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # Prefer component.base (card-like), else background.base
    match = re.search(r"component:\s*\(\s*base:\s*\"#([0-9A-Fa-f]{6,8})\"", text)
    if match:
        return match.group(1)
    match = re.search(r'base:\s*"#([0-9A-Fa-f]{6,8})"', text)
    return match.group(1) if match else None


def _cosmic_same_on_all() -> bool:
    path = Path.home() / ".config/cosmic/com.system76.CosmicBackground/v1/same-on-all"
    try:
        return path.read_text(encoding="utf-8").strip().lower() == "true"
    except OSError:
        return False


def _parse_cosmic_wallpaper_source(config_path: Path) -> Path | None:
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r'source:\s*Path\("([^"]+)"\)', text)
    if not match:
        match = re.search(r'Path\("([^"]+)"\)', text)
    if not match:
        return None
    wallpaper = Path(match.group(1))
    return wallpaper if wallpaper.is_file() else None


def _cosmic_wallpaper_path() -> Path | None:
    """Resolve the active COSMIC wallpaper (respects same-on-all)."""
    root = Path.home() / ".config/cosmic/com.system76.CosmicBackground/v1"
    if not root.is_dir():
        return None

    # When same-on-all is true, COSMIC applies `all` to every display.
    # Per-output files can stay stale and must not win.
    if _cosmic_same_on_all():
        wallpaper = _parse_cosmic_wallpaper_source(root / "all")
        if wallpaper is not None:
            return wallpaper

    outputs = [p for p in root.glob("output.*") if p.is_file()]
    outputs.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in outputs:
        wallpaper = _parse_cosmic_wallpaper_source(path)
        if wallpaper is not None:
            return wallpaper

    return _parse_cosmic_wallpaper_source(root / "all")


def _gsettings_wallpaper_path() -> Path | None:
    for key in ("picture-uri-dark", "picture-uri"):
        try:
            out = subprocess.check_output(
                ["gsettings", "get", "org.gnome.desktop.background", key],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip().strip("'\"")
        except (subprocess.SubprocessError, OSError):
            continue
        if out.startswith("file://"):
            out = unquote(urlparse(out).path)
        path = Path(out)
        if path.is_file():
            return path
    return None


def _theme_watch_paths() -> list[Path]:
    """COSMIC / desktop files that define wallpaper and accent."""
    home = Path.home()
    paths = [
        home / ".config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark",
        home / ".config/cosmic/com.system76.CosmicTheme.Dark/v2/accent",
        home / ".config/cosmic/com.system76.CosmicTheme.Dark/v2/background",
        home / ".config/cosmic/com.system76.CosmicTheme.Light/v2/accent",
        home / ".config/cosmic/com.system76.CosmicTheme.Light/v2/background",
        home / ".config/cosmic/com.system76.CosmicBackground/v1/all",
        home / ".config/cosmic/com.system76.CosmicBackground/v1/backgrounds",
        home / ".config/cosmic/com.system76.CosmicBackground/v1/same-on-all",
    ]
    bg_root = home / ".config/cosmic/com.system76.CosmicBackground/v1"
    if bg_root.is_dir():
        paths.extend(sorted(bg_root.glob("output.*")))
    return paths


def system_theme_fingerprint() -> str:
    """Cheap fingerprint of desktop wallpaper/accent/theme inputs."""
    parts: list[str] = []
    for path in _theme_watch_paths():
        try:
            st = path.stat()
            parts.append(f"{path}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{path}:missing")
        # Also hash the Path("...") source line so wallpaper swaps are detected
        # even if another stale output.* file would have been preferred before.
        if "CosmicBackground" in str(path) and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r'source:\s*Path\("([^"]+)"\)', text)
                if match:
                    parts.append(f"src:{path.name}:{match.group(1)}")
                elif path.name == "same-on-all":
                    parts.append(f"same:{text.strip()}")
            except OSError:
                pass

    wallpaper = _cosmic_wallpaper_path() or _gsettings_wallpaper_path()
    if wallpaper is not None:
        try:
            st = wallpaper.stat()
            parts.append(f"wp:{wallpaper}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"wp:{wallpaper}:missing")
    else:
        parts.append("wp:none")
        # GNOME fallback only when COSMIC wallpaper config is absent
        for key in ("picture-uri-dark", "picture-uri", "primary-color", "secondary-color"):
            try:
                out = subprocess.check_output(
                    ["gsettings", "get", "org.gnome.desktop.background", key],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                ).strip()
                parts.append(f"gs:{key}:{out}")
            except (subprocess.SubprocessError, OSError):
                parts.append(f"gs:{key}:na")

    return hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def load_system_theme(force: bool = False) -> dict:
    """Load Pop!_OS / COSMIC wallpaper + accent colors (reload when desktop changes)."""
    global _SYSTEM_THEME_CACHE, _SYSTEM_THEME_FINGERPRINT
    fingerprint = system_theme_fingerprint()
    if (
        not force
        and _SYSTEM_THEME_CACHE is not None
        and fingerprint == _SYSTEM_THEME_FINGERPRINT
    ):
        return _SYSTEM_THEME_CACHE

    wallpaper_path = _cosmic_wallpaper_path() or _gsettings_wallpaper_path()
    accent_hex = _cosmic_accent_hex()
    panel_hex = _cosmic_panel_hex()

    accent = _hex_to_rgb(accent_hex) if accent_hex else CYAN
    panel = _hex_to_rgb(panel_hex) if panel_hex else PANEL
    wallpaper = None
    accent2 = BLUE

    if wallpaper_path and wallpaper_path.is_file():
        try:
            wallpaper = Image.open(wallpaper_path).convert("RGB")
            extracted = _extract_theme_colors(wallpaper)
            # Prefer COSMIC accent when available; still take secondary from wallpaper
            if not accent_hex:
                accent = extracted[0]
            accent2 = extracted[1]
            if not panel_hex:
                panel = extracted[2]
        except OSError as exc:
            logger.debug("Failed to load wallpaper %s: %s", wallpaper_path, exc)

    changed = bool(_SYSTEM_THEME_FINGERPRINT) and fingerprint != _SYSTEM_THEME_FINGERPRINT
    theme = {
        "wallpaper": wallpaper,
        "wallpaper_path": str(wallpaper_path) if wallpaper_path else "",
        "accent": accent,
        "accent2": accent2,
        "panel": panel,
        "is_dark": _cosmic_is_dark(),
        "fingerprint": fingerprint,
        "changed": changed,
    }
    if changed:
        logger.info(
            "Desktop environment theme updated (wallpaper=%s accent=%s dark=%s)",
            theme["wallpaper_path"] or "none",
            accent,
            theme["is_dark"],
        )
    _SYSTEM_THEME_CACHE = theme
    _SYSTEM_THEME_FINGERPRINT = fingerprint
    return theme


def system_backdrop(theme: dict | None = None) -> Image.Image:
    """Immersive desktop-themed background from wallpaper + COSMIC accents."""
    from PIL import ImageEnhance

    theme = theme or load_system_theme()
    accent = tuple(theme["accent"])
    ar, ag, ab = accent

    if theme.get("wallpaper") is not None:
        backdrop = _cover_fit(theme["wallpaper"], (WIDTH, HEIGHT))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.82)
        backdrop = ImageEnhance.Contrast(backdrop).enhance(1.12)
        backdrop = ImageEnhance.Color(backdrop).enhance(1.2)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (WIDTH, HEIGHT), (*BG, 255))

    wash = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    wash_draw.rectangle((0, 0, WIDTH, 44), fill=(ar, ag, ab, 70))
    for y in range(200, HEIGHT):
        t = (y - 200) / max(1, HEIGHT - 200)
        wash_draw.line((0, y, WIDTH, y), fill=(4, 6, 10, int(40 + 180 * t)))
    image = Image.alpha_composite(image, wash).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=accent, width=3)
    return image


def tech_background():
    """Legacy fallback — prefer system_backdrop for MAIN."""
    return system_backdrop()


def draw_corner_marks(draw, box, color):
    x1, y1, x2, y2 = box
    length = 10
    for points in (
        (x1, y1 + length, x1, y1, x1 + length, y1),
        (x2 - length, y1, x2, y1, x2, y1 + length),
        (x1, y2 - length, x1, y2, x1 + length, y2),
        (x2 - length, y2, x2, y2, x2, y2 - length),
    ):
        draw.line(points, fill=color, width=2)


# Region refreshed every second (must match clock placement in render_dashboard)
MAIN_CLOCK_CROP = (16, 6, 380, 82)


def render_dashboard(now, last_notification=None):
    """MAIN mode: wallpaper-first desktop HUD matching GAMER/MULTIMEDIA energy."""
    from PIL import ImageEnhance

    theme = load_system_theme()
    accent = tuple(theme["accent"])
    accent2 = tuple(theme["accent2"])
    ar, ag, ab = accent

    if theme.get("wallpaper") is not None:
        backdrop = _cover_fit(theme["wallpaper"], (WIDTH, HEIGHT))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.82)
        backdrop = ImageEnhance.Contrast(backdrop).enhance(1.12)
        backdrop = ImageEnhance.Color(backdrop).enhance(1.2)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (WIDTH, HEIGHT), (*BG, 255))

    wash = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    # Compact clock band — frees vertical space for weather / notifications
    wash_draw.rectangle((0, 0, WIDTH, 4), fill=(ar, ag, ab, 110))
    wash_draw.rounded_rectangle((14, 6, 466, 84), radius=12, fill=(0, 0, 0, 130))
    for y in range(120, HEIGHT):
        t = (y - 120) / max(1, HEIGHT - 120)
        wash_draw.line((0, y, WIDTH, y), fill=(4, 6, 10, int(50 + 190 * t)))
    image = Image.alpha_composite(image, wash)

    image_rgb = image.convert("RGB")
    draw = ImageDraw.Draw(image_rgb)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=accent, width=3)

    clock = now.strftime("%H:%M:%S")
    clock_font = font(FONT_MONO, 50)
    draw.text((24, 47), clock, font=clock_font, fill=(0, 0, 0), anchor="lm")
    draw.text((22, 45), clock, font=clock_font, fill=WHITE, anchor="lm")

    # Date without year; weekday raised into the freed row
    date_str = now.strftime("%d %b").upper()
    day_str = now.strftime("%A").upper()
    draw.text((446, 16), date_str, font=font(FONT_BOLD, 28), fill=accent, anchor="ra")
    draw.text((446, 48), day_str, font=font(FONT_MEDIUM, 22), fill=accent2, anchor="ra")

    draw.rectangle((22, 90, 458, 93), fill=accent)

    note_scrim = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(note_scrim).rounded_rectangle((14, 100, 466, 308), radius=16, fill=(0, 0, 0, 140))
    image_rgb = Image.alpha_composite(image_rgb.convert("RGBA"), note_scrim).convert("RGB")
    draw = ImageDraw.Draw(image_rgb)

    if last_notification is None:
        weather = fetch_weather()
        if weather is not None and weather.ok:
            # Hero: glyph + large temp; condition top-right; compact meta; rain strip
            icon_box = (26, 110, 80, 164)
            _draw_weather_icon(draw, icon_box, weather.weather_code, accent)

            temp = f"{weather.temperature:.0f}°"
            temp_font = font(FONT_BOLD, 56)
            draw.text((94, 140), temp, font=temp_font, fill=(0, 0, 0), anchor="lm")
            draw.text((92, 138), temp, font=temp_font, fill=WHITE, anchor="lm")

            cond = weather.condition or "—"
            cond_font = font(FONT_MEDIUM, 22)
            while cond and draw.textlength(cond, font=cond_font) > 230:
                cond = cond[:-1]
            if cond != (weather.condition or ""):
                cond = cond.rstrip() + "…"
            draw.text((446, 118), cond, font=cond_font, fill=accent2, anchor="ra")

            meta_font = font(FONT_MONO, 17)
            meta_left = f"máx {weather.temp_max:.0f}° · mín {weather.temp_min:.0f}°"
            draw.text((92, 176), meta_left, font=meta_font, fill=MUTED, anchor="lm")

            rain_left = remaining_day_precip_prob(
                weather.hourly_precip_prob, now.hour, weather.precip_prob
            )
            parts = [
                ("Chuva ", MUTED),
                (f"{rain_left:.0f}%", ERROR_AMBER if rain_left >= 50 else accent),
                (f"  ·  Um {weather.humidity:.0f}%", MUTED),
                (f"  ·  {weather.wind_kmh:.0f} km/h", MUTED),
            ]
            left_w = int(draw.textlength(meta_left, font=meta_font))
            right_budget = max(80, 446 - (92 + left_w + 28))

            def _parts_width(items):
                return sum(draw.textlength(t, font=meta_font) for t, _ in items)

            while len(parts) > 2 and _parts_width(parts) > right_budget:
                parts.pop()
            x = 446 - int(_parts_width(parts))
            for text, color in parts:
                draw.text((x, 176), text, font=meta_font, fill=color, anchor="lm")
                x += int(draw.textlength(text, font=meta_font))

            _draw_rain_timeline(
                draw,
                (24, 200, 456, 304),
                weather.hourly_precip_prob,
                now.hour,
                accent,
                font(FONT_MONO, 16),
                font(FONT_MONO, 18),
            )
        else:
            icon_box = (28, 126, 124, 270)
            draw.rounded_rectangle(
                (icon_box[0] - 3, icon_box[1] - 3, icon_box[2] + 3, icon_box[3] + 3),
                radius=14,
                outline=accent,
                width=3,
            )
            draw.rounded_rectangle(icon_box, radius=12, fill=(10, 12, 16))
            draw.line((54, 198, 98, 198), fill=accent, width=4)
            draw.line((76, 176, 76, 220), fill=accent, width=4)
            draw.text((144, 148), "Nenhuma notificação", font=font(FONT_BOLD, 32), fill=WHITE)
            draw.text((144, 196), "Clima indisponível · aguardando alertas", font=font(FONT_REGULAR, 20), fill=accent)
            draw.text((446, 124), now.strftime("%H:%M"), font=font(FONT_MONO, 17), fill=MUTED, anchor="ra")
    else:
        icon_box = (28, 126, 124, 270)
        draw.rounded_rectangle(
            (icon_box[0] - 3, icon_box[1] - 3, icon_box[2] + 3, icon_box[3] + 3),
            radius=14,
            outline=accent,
            width=3,
        )
        draw.rounded_rectangle(icon_box, radius=12, fill=(10, 12, 16))
        app_icon = load_notification_icon(last_notification.icon, last_notification.app, 88)
        if app_icon is not None:
            paste_rounded_icon(image_rgb, app_icon, icon_box, radius=12)
        else:
            draw.text((76, 198), "!", font=font(FONT_BOLD, 36), fill=WHITE, anchor="mm")
        draw.text(
            (446, 124),
            last_notification.received_at.strftime("%H:%M"),
            font=font(FONT_MONO, 17),
            fill=MUTED,
            anchor="ra",
        )
        app_name = (last_notification.app or "SISTEMA").upper()
        draw.text((144, 128), app_name, font=font(FONT_MONO, 15), fill=accent)
        title_font = font(FONT_BOLD, 30)
        body_font = font(FONT_REGULAR, 21)
        title = wrap_text(draw, last_notification.title, title_font, 300, 1)
        body = wrap_text(draw, clean_notification_body(last_notification.body), body_font, 300, 5)
        draw.text((144, 156), title[0] if title else "Nova notificação", font=title_font, fill=WHITE)
        body_y = 198
        for line in body:
            draw.text((144, body_y), line, font=body_font, fill=(230, 232, 240))
            body_y += 28

    return image_rgb


def render_error_screen(
    title: str,
    detail: str = "",
    hint: str = "Tentando recuperar…",
    now: datetime | None = None,
):
    """Fullscreen error / recovery status for the IPS panel."""
    now = now or datetime.now().astimezone()
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=ERROR_RED, width=3)
    draw.rectangle((0, 0, WIDTH, 8), fill=ERROR_RED)
    draw.rounded_rectangle((16, 28, 464, 292), radius=16, fill=PANEL, outline=ERROR_RED, width=2)
    draw_corner_marks(draw, (28, 40, 452, 280), ERROR_AMBER)

    badge = "ERRO"
    bw = max(64, int(draw.textlength(badge, font=font(FONT_MONO, 14)) + 24))
    draw.rounded_rectangle((36, 48, 36 + bw, 74), radius=8, fill=ERROR_RED)
    draw.text((36 + bw // 2, 61), badge, font=font(FONT_MONO, 14), fill=(12, 12, 14), anchor="mm")
    draw.text((446, 61), now.strftime("%H:%M:%S"), font=font(FONT_MONO, 16), fill=MUTED, anchor="rm")

    title_text = (title or "Falha na tela").strip() or "Falha na tela"
    title_font = font(FONT_BOLD, 28)
    title_y = 96
    for line in wrap_text(draw, title_text, title_font, 400, 2):
        draw.text((40, title_y), line, font=title_font, fill=WHITE)
        title_y += 34

    draw.line((36, title_y + 6, 444, title_y + 6), fill=ERROR_AMBER, width=2)
    body_y = title_y + 20
    detail_font = font(FONT_REGULAR, 18)
    detail_text = clean_markup(detail) if detail else "Sem detalhes adicionais."
    for line in wrap_text(draw, detail_text, detail_font, 400, 4):
        draw.text((40, body_y), line, font=detail_font, fill=MUTED)
        body_y += 24

    hint_font = font(FONT_MEDIUM, 16)
    draw.text((40, 258), hint, font=hint_font, fill=ERROR_AMBER)
    draw.rectangle((36, 278, 444, 284), fill=(20, 24, 28))
    draw.rectangle((36, 278, 220, 284), fill=ERROR_RED)
    return image


def try_show_error(
    lcd: ResilientLcd,
    title: str,
    detail: str = "",
    hint: str = "Tentando recuperar…",
) -> bool:
    """Best-effort error frame; never raises into the caller."""
    try:
        refresh_full_frame(lcd, render_error_screen(title, detail, hint))
        return True
    except Exception as exc:
        logger.debug("Could not show error screen: %s", exc)
        return False


def render_notification(item, now):
    """Fullscreen notification overlay using the current desktop theme."""
    from PIL import ImageEnhance

    theme = load_system_theme()
    accent = tuple(theme["accent"])
    accent2 = tuple(theme["accent2"])
    ar, ag, ab = accent

    if theme.get("wallpaper") is not None:
        backdrop = _cover_fit(theme["wallpaper"], (WIDTH, HEIGHT))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.7)
        backdrop = ImageEnhance.Color(backdrop).enhance(1.15)
        image = backdrop.convert("RGBA")
    else:
        image = Image.new("RGBA", (WIDTH, HEIGHT), (*BG, 255))

    wash = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    wash_draw = ImageDraw.Draw(wash)
    wash_draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(4, 6, 10, 90))
    wash_draw.rounded_rectangle((14, 16, 466, 304), radius=18, fill=(0, 0, 0, 130))
    wash_draw.rectangle((0, 0, WIDTH, 40), fill=(ar, ag, ab, 70))
    image = Image.alpha_composite(image, wash).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=accent, width=3)
    draw.rounded_rectangle((14, 16, 466, 304), radius=18, outline=accent, width=2)

    icon_box = (36, 40, 132, 136)
    draw.rounded_rectangle(
        (icon_box[0] - 4, icon_box[1] - 4, icon_box[2] + 4, icon_box[3] + 4),
        radius=16,
        outline=accent,
        width=3,
    )
    draw.rounded_rectangle(icon_box, radius=14, fill=(10, 12, 16))
    app_icon = load_notification_icon(item.icon, item.app, 88)
    if app_icon is not None:
        paste_rounded_icon(image, app_icon, icon_box, radius=14)
    else:
        draw.text((84, 88), "!", font=font(FONT_BOLD, 36), fill=WHITE, anchor="mm")

    draw.text((446, 36), item.received_at.strftime("%H:%M"), font=font(FONT_MONO, 14), fill=MUTED, anchor="ra")

    app_label = (item.app or "SISTEMA").upper()
    aw = max(70, int(draw.textlength(app_label, font=font(FONT_MONO, 12)) + 20))
    draw.rounded_rectangle((152, 42, 152 + aw, 66), radius=8, fill=accent)
    draw.text((152 + aw // 2, 54), app_label, font=font(FONT_MONO, 12), fill=(12, 12, 14), anchor="mm")

    title_font = font(FONT_BOLD, 28)
    title_lines = wrap_text(draw, item.title or "Nova notificação", title_font, 290, 2)
    title_y = 78
    for line in title_lines:
        draw.text((152, title_y), line, font=title_font, fill=WHITE)
        title_y += 32

    draw.line((36, 156, 444, 156), fill=accent, width=2)
    body_font = font(FONT_REGULAR, 22)
    body_lines = wrap_text(draw, clean_notification_body(item.body), body_font, 400, 4)
    body_y = 172
    for line in body_lines:
        draw.text((36, body_y), line, font=body_font, fill=(232, 238, 245))
        body_y += 28

    draw.rectangle((16, 308, 464, 314), fill=(20, 24, 28))
    draw.rectangle((16, 308, 340, 314), fill=accent)
    return image



def _parse_dbus_string_line(line: str) -> str | None:
    """Parse `string "..."` possibly with a leading `variant` prefix."""
    match = re.match(r'\s*(?:variant\s+)?string "(.*)', line)
    if not match:
        return None
    value = match.group(1)
    if line.rstrip().endswith('"'):
        return dbus_unescape(value[:-1])
    return None  # multiline start handled by caller


def _app_label_from_portal_id(app_id: str) -> str:
    """Turn Flatpak/desktop ids (com.discordapp.Discord) into a short label."""
    app_id = (app_id or "").strip()
    if not app_id:
        return "App"
    if "." in app_id:
        return app_id.rsplit(".", 1)[-1]
    return app_id


def _notification_dedup_key(title: str, body: str) -> tuple[str, str]:
    return ((title or "").strip(), clean_notification_body(body or "").strip())


def _is_duplicate_notification(title: str, body: str) -> bool:
    """True if the same title/body was enqueued within the dedup window."""
    key = _notification_dedup_key(title, body)
    if not key[0] and not key[1]:
        return False
    now = time.monotonic()
    while _recent_notifications and now - _recent_notifications[0][0] > _NOTIFICATION_DEDUP_SECONDS:
        _recent_notifications.popleft()
    for ts, prev_title, prev_body in _recent_notifications:
        if (prev_title, prev_body) == key:
            return True
    _recent_notifications.append((now, key[0], key[1]))
    return False


def _enqueue_notification(app: str, app_icon: str, title: str, body: str, image_path: str = ""):
    app, title, body = redact_whatsapp_notification(
        app, title, body, app_icon, image_path
    )
    if _is_duplicate_notification(title, body):
        logger.debug("Skipping duplicate notification (app=%r title=%r)", app, title)
        return
    icon = _prefer_site_icon(app_icon, image_path)
    note = Notification(app, icon, title, body, datetime.now().astimezone())
    notification_queue.put(note)
    resolved = resolve_icon_path(note.icon, note.app)
    logger.info(
        "Desktop notification received (app=%r app_icon=%r image_path=%r -> %s)",
        note.app,
        app_icon,
        image_path or "",
        resolved or note.icon or "fallback",
    )


def notification_monitor():
    """Mirror every desktop notification path used on GNOME/Flatpak.

    Paths:
      - org.freedesktop.Notifications.Notify (native/Electron host apps)
      - org.freedesktop.impl.portal.Notification.AddNotification (portal backend)
      - org.gtk.Notifications.AddNotification (GNOME gtk bridge / direct GTK apps)

    Flatpak clients hit the public portal first; GNOME then fans out to impl and
    gtk with the real app_id. We watch those (not the public portal) so labels
    like Discord stay correct. Notify is often forwarded twice by the shell;
    content dedup collapses copies within a short window.
    """
    global monitor_process
    command = [
        "stdbuf", "-oL", "dbus-monitor", "--session",
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
        "type='method_call',interface='org.freedesktop.impl.portal.Notification',member='AddNotification'",
        "type='method_call',interface='org.gtk.Notifications',member='AddNotification'",
    ]
    try:
        monitor_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        capturing = False
        values: list[str] = []
        image_path = ""
        pending_hint = None  # "image-path" when next variant string is the value
        multiline = None
        multiline_target = None  # "value" or "hint"

        # AddNotification: public portal = (id, dict); impl/gtk = (app_id, id, dict)
        portal_capturing = False
        portal_depth = 0
        portal_seen_depth = False
        portal_strings: list[str] = []
        portal_fields: dict[str, str] = {}
        portal_pending_key = None
        portal_multiline = None

        def reset_notify():
            nonlocal capturing, values, image_path, pending_hint, multiline, multiline_target
            capturing = True
            values = []
            image_path = ""
            pending_hint = None
            multiline = None
            multiline_target = None

        def reset_portal():
            nonlocal portal_capturing, portal_depth, portal_seen_depth
            nonlocal portal_strings, portal_fields, portal_pending_key, portal_multiline
            portal_capturing = True
            portal_depth = 0
            portal_seen_depth = False
            portal_strings = []
            portal_fields = {}
            portal_pending_key = None
            portal_multiline = None

        def finalize_note():
            nonlocal capturing, values, image_path, pending_hint, multiline, multiline_target
            if len(values) < 4:
                capturing = False
                return
            _enqueue_notification(values[0], values[1], values[2], values[3], image_path)
            capturing = False
            values = []
            image_path = ""
            pending_hint = None
            multiline = None
            multiline_target = None

        def finalize_portal():
            nonlocal portal_capturing, portal_depth, portal_seen_depth
            nonlocal portal_strings, portal_fields, portal_pending_key, portal_multiline
            if not portal_capturing:
                return
            # Public portal: [id]; impl/gtk: [app_id, id]
            if len(portal_strings) >= 2:
                app_id = portal_strings[0]
            else:
                app_id = ""
            title = portal_fields.get("title") or ""
            body = portal_fields.get("body") or ""
            icon = portal_fields.get("icon") or ""
            if title or body:
                app = _app_label_from_portal_id(app_id)
                persisted = _persist_notification_icon(icon) if icon else ""
                _enqueue_notification(app, persisted or icon, title, body, "")
            portal_capturing = False
            portal_depth = 0
            portal_seen_depth = False
            portal_strings = []
            portal_fields = {}
            portal_pending_key = None
            portal_multiline = None

        for raw_line in monitor_process.stdout:
            if not running:
                break
            line = raw_line.rstrip("\n")
            if line.startswith("method call") and "member=Notify" in line:
                finalize_portal()
                reset_notify()
                continue
            if line.startswith("method call") and "member=AddNotification" in line:
                finalize_note()
                capturing = False
                reset_portal()
                continue

            if portal_capturing:
                if portal_multiline is not None:
                    portal_multiline += "\n" + line
                    if line.rstrip().endswith('"'):
                        text = dbus_unescape(portal_multiline[:-1])
                        if portal_pending_key:
                            if portal_pending_key not in portal_fields:
                                portal_fields[portal_pending_key] = text
                            portal_pending_key = None
                        elif len(portal_strings) < 2:
                            portal_strings.append(text)
                        portal_multiline = None
                    continue

                portal_depth += line.count("[") + line.count("(")
                portal_depth -= line.count("]") + line.count(")")
                if portal_depth > 0:
                    portal_seen_depth = True

                # Collect leading strings only before the notification dict opens.
                if not portal_seen_depth and len(portal_strings) < 2:
                    match = re.match(r'\s+string "(.*)', line)
                    if match:
                        value = match.group(1)
                        if line.rstrip().endswith('"'):
                            portal_strings.append(dbus_unescape(value[:-1]))
                        else:
                            portal_multiline = value
                elif portal_depth >= 1:
                    key_match = re.match(
                        r'\s+string "(title|body|icon)"\s*$', line
                    )
                    if key_match:
                        portal_pending_key = key_match.group(1)
                        continue
                    if portal_pending_key:
                        if "variant" in line and "string \"" in line:
                            parsed = _parse_dbus_string_line(line)
                            if parsed is not None:
                                if portal_pending_key not in portal_fields:
                                    portal_fields[portal_pending_key] = parsed
                                portal_pending_key = None
                            else:
                                match = re.match(r'\s*variant\s+string "(.*)', line)
                                if match:
                                    portal_multiline = match.group(1)
                            continue
                        if re.match(r"\s+variant\s*$", line):
                            continue
                        portal_pending_key = None

                if portal_seen_depth and portal_depth <= 0:
                    finalize_portal()
                continue

            if not capturing:
                continue

            # Expire timeout ends the Notify args — finalize even if hints were empty.
            if re.match(r"\s+int32\s+", line):
                finalize_note()
                continue

            if multiline is not None:
                multiline += "\n" + line
                if line.rstrip().endswith('"'):
                    text = dbus_unescape(multiline[:-1])
                    if multiline_target == "hint" and not image_path:
                        image_path = _persist_notification_icon(text) or text
                        pending_hint = None
                    elif multiline_target == "value" and len(values) < 4:
                        values.append(text)
                    multiline = None
                    multiline_target = None
                continue

            # Hint key then variant string value (Chrome site icon).
            hint_key = re.match(r'\s+string "(image-path|image_path)"\s*$', line)
            if hint_key:
                pending_hint = hint_key.group(1)
                continue

            if pending_hint:
                if "variant" in line and "string \"" in line:
                    parsed = _parse_dbus_string_line(line)
                    if parsed is not None:
                        if not image_path:
                            # Persist immediately — Chrome deletes scoped temp icons quickly.
                            image_path = _persist_notification_icon(parsed) or parsed
                        pending_hint = None
                    else:
                        # multiline variant string
                        match = re.match(r'\s*variant\s+string "(.*)', line)
                        if match:
                            multiline = match.group(1)
                            multiline_target = "hint"
                    continue
                if re.match(r"\s+variant\s*$", line):
                    continue
                # Unexpected token — stop waiting for this hint value
                pending_hint = None

            # Top-level Notify strings: app, app_icon, title, body (ignore later action strings).
            if len(values) < 4:
                match = re.match(r'\s+string "(.*)', line)
                if match:
                    value = match.group(1)
                    if line.rstrip().endswith('"'):
                        values.append(dbus_unescape(value[:-1]))
                    else:
                        multiline = value
                        multiline_target = "value"
    except Exception as exc:
        logger.warning("Notification monitor stopped: %s", exc)


def stop(*_args):
    global running
    running = False
    if monitor_process and monitor_process.poll() is None:
        monitor_process.terminate()


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def mark_frame_ok():
    global last_successful_frame_at
    last_successful_frame_at = time.monotonic()


def brightness_for_mode(mode: Mode) -> int:
    """Panel brightness for the active mode (LOCKED dims to 10%)."""
    if mode == Mode.LOCKED:
        return LOCKED_BRIGHTNESS
    return BRIGHTNESS


def apply_mode_brightness(lcd: ResilientLcd, mode: Mode):
    """Apply mode brightness and keep ACTIVE_BRIGHTNESS in sync for soft nudges."""
    global ACTIVE_BRIGHTNESS
    level = brightness_for_mode(mode)
    if level == ACTIVE_BRIGHTNESS:
        return
    ACTIVE_BRIGHTNESS = level
    try:
        lcd.SetBrightness(level=level)
        logger.info("Brightness set to %s%% for mode %s", level, mode.name)
    except Exception as exc:
        logger.debug("SetBrightness(%s) failed: %s", level, exc)


def refresh_full_frame(lcd: ResilientLcd, image: Image.Image):
    """Send a full-frame PIL image and mark heartbeat."""
    lcd.DisplayPILImage(image)
    mark_frame_ok()


def display_image(lcd: ResilientLcd, image: Image.Image, x: int = 0, y: int = 0):
    """Partial or full image write with heartbeat."""
    lcd.DisplayPILImage(image, x, y)
    mark_frame_ok()


def redraw_current_mode(lcd: ResilientLcd, mode_manager, now, last_notification):
    """Redraw the active mode screen (used after notification overlay / recovery)."""
    mode = mode_manager.state.current_mode
    if mode == Mode.MULTIMEDIA:
        refresh_full_frame(lcd, render_multimedia_mode(mode_manager.state.multimedia, now))
    elif mode == Mode.GAMER:
        refresh_full_frame(lcd, render_gamer_mode(mode_manager.state.gamer, now))
    elif mode == Mode.LOCKED:
        refresh_full_frame(lcd, render_locked_mode(now, load_system_theme()))
    else:
        refresh_full_frame(lcd, render_dashboard(now, last_notification))


def wait_for_display(lcd: ResilientLcd):
    """Retry bring_up until the display answers or the process is stopping."""
    global last_recovery_at
    while running:
        try:
            lcd.bring_up()
            last_recovery_at = time.monotonic()
            return
        except Exception as exc:
            logger.error(
                "Display unavailable (%s); retrying in %ss",
                exc,
                RECOVER_SLEEP_SECONDS,
            )
            lcd.closeSerial()
            lcd.com_port = "AUTO"
            time.sleep(RECOVER_SLEEP_SECONDS)


def recover_display(lcd: ResilientLcd, mode_manager, last_notification, reason: str):
    """Full automatic recovery after freeze / I/O failure / watchdog."""
    global last_recovery_at, recovery_hard_next, last_soft_nudge_at
    now_mono = time.monotonic()
    if now_mono - last_recovery_at < RECOVERY_COOLDOWN_SECONDS:
        return False
    logger.warning("Automatic display recovery (%s)", reason)
    last_recovery_at = now_mono
    # Show a visible fault state before tearing down the serial link.
    try_show_error(
        lcd,
        "ERRO DE DISPLAY",
        reason,
        "Reconectando a tela…",
    )
    # Alternate: soft first, then hard if we recover again soon
    if "watchdog" in reason.lower() or "nudge" in reason.lower():
        recovery_hard_next = False
    wait_for_display(lcd)
    if not running:
        return False
    try:
        # Confirm bring-up with the error frame, then restore the active mode.
        try_show_error(
            lcd,
            "DISPLAY RECUPERADO",
            reason,
            "Restaurando o painel…",
        )
        time.sleep(0.4)
        redraw_current_mode(lcd, mode_manager, datetime.now().astimezone(), last_notification)
        last_soft_nudge_at = time.monotonic()
        logger.info("Display recovery succeeded")
        # Next failure within a short window uses hard reset
        recovery_hard_next = True
        return True
    except (serial.SerialException, OSError) as redraw_exc:
        logger.warning("Redraw after recovery failed (%s); escalating to hard reset", redraw_exc)
        try_show_error(
            lcd,
            "FALHA NA RECUPERAÇÃO",
            str(redraw_exc),
            "Nova tentativa em breve…",
        )
        recovery_hard_next = True
        return False


def needs_watchdog_recovery() -> bool:
    if last_successful_frame_at <= 0:
        return False
    return (time.monotonic() - last_successful_frame_at) >= WATCHDOG_SECONDS


# ---------------------------------------------------------------------------
# Main entry (import-safe)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    mode_manager = ModeManager(config_mode=args.mode)

    # Determine which mode to show initially
    initial_mode = mode_manager.state.current_mode
    logger.info("Starting in mode: %s", initial_mode.name)

    # ---------------------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------------------

    lcd = ResilientLcd(com_port="AUTO", display_width=320, display_height=480)

    try:
        recovery_hard_next = True  # first start / wake blank panel with hardware reset
        wait_for_display(lcd)
        threading.Thread(target=notification_monitor, name="notification-monitor", daemon=True).start()

        last_second = None
        last_date = None
        last_minute = None
        overlay_until = 0.0
        overlay_active = False
        last_notification = None

        # Render initial screen based on mode
        apply_mode_brightness(lcd, initial_mode)
        if initial_mode == Mode.MAIN:
            refresh_full_frame(lcd, render_dashboard(datetime.now().astimezone(), last_notification))
        elif initial_mode == Mode.MULTIMEDIA:
            refresh_full_frame(lcd, render_multimedia_mode(mode_manager.state.multimedia, datetime.now().astimezone()))
        elif initial_mode == Mode.GAMER:
            refresh_full_frame(lcd, render_gamer_mode(mode_manager.state.gamer, datetime.now().astimezone()))
        elif initial_mode == Mode.LOCKED:
            refresh_full_frame(lcd, render_locked_mode(datetime.now().astimezone(), load_system_theme()))

        # Detection interval (how often to check for mode changes)
        DETECTION_INTERVAL = 3.0  # seconds (game / media)
        LOCK_POLL_INTERVAL = 1.0  # seconds (session lock — snappier)

        # Mode-specific state
        last_mode_detection_time = 0.0
        last_lock_poll_time = 0.0
        mode_render_cache = None
        mode_render_time = 0.0
        MODE_CACHE_TTL = 3.0  # full frames are expensive on Rev A serial
        last_track_key = ""
        last_mode = mode_manager.state.current_mode
        last_soft_nudge_at = time.monotonic()
        soft_nudge_count = 0
        boot_confirm_at = time.monotonic() + BOOT_CONFIRM_HARD_SECONDS
        boot_confirm_done = False
        last_theme_check_at = 0.0
        last_theme_fingerprint = load_system_theme().get("fingerprint", "")

        while running:
            try:
                now = datetime.now().astimezone()
                current_time = time.monotonic()

                # Cold-boot: only second-reset if the panel never accepted a frame.
                # A blind Reset on a working Rev A often leaves it garbled/frozen.
                if not boot_confirm_done and current_time >= boot_confirm_at:
                    boot_confirm_done = True
                    if last_successful_frame_at > 0 and (
                        current_time - last_successful_frame_at
                    ) < WATCHDOG_SECONDS:
                        logger.info(
                            "Boot confirm skipped — display already receiving frames"
                        )
                    else:
                        logger.info("Boot confirm: second hard reset (no successful frames yet)")
                        recovery_hard_next = True
                        last_recovery_at = 0.0  # bypass cooldown for this one-shot
                        if recover_display(lcd, mode_manager, last_notification, "boot confirm hard reset"):
                            mode_render_time = 0.0
                            last_second = None
                            last_date = None
                            last_minute = None
                            last_track_key = ""
                        continue

                # --- Session lock poll (fast path, highest priority; runs even during overlays) ---
                if current_time - last_lock_poll_time >= LOCK_POLL_INTERVAL:
                    last_lock_poll_time = current_time
                    previous = mode_manager.state.current_mode
                    if mode_manager.poll_lock():
                        mode_render_time = 0.0
                        last_second = None
                        last_date = None
                        last_minute = None
                        last_track_key = ""
                        last_mode = mode_manager.state.current_mode
                        overlay_active = False
                        apply_mode_brightness(lcd, mode_manager.state.current_mode)
                        if mode_manager.state.current_mode == Mode.LOCKED:
                            theme = load_system_theme()
                            last_theme_fingerprint = theme.get("fingerprint", "")
                            last_theme_check_at = current_time
                            refresh_full_frame(lcd, render_locked_mode(now, theme))
                            last_second = now.strftime("%H:%M:%S")
                            last_soft_nudge_at = current_time
                            last_mode_detection_time = current_time
                            continue
                        if mode_manager.state.current_mode == Mode.MAIN and previous == Mode.LOCKED:
                            theme = load_system_theme()
                            last_theme_fingerprint = theme.get("fingerprint", "")
                            last_theme_check_at = current_time
                            refresh_full_frame(lcd, render_dashboard(now, last_notification))
                            last_second = now.strftime("%H:%M:%S")
                            last_date = now.strftime("%Y-%m-%d")
                            last_minute = now.strftime("%H:%M")
                            last_soft_nudge_at = current_time
                            last_mode_detection_time = 0.0  # re-evaluate game/media ASAP
                            continue

                # --- Mode detection (periodic, paused during notification overlay / lock) ---
                if (
                    not overlay_active
                    and mode_manager.state.current_mode != Mode.LOCKED
                    and current_time - last_mode_detection_time >= DETECTION_INTERVAL
                ):
                    previous = mode_manager.state.current_mode
                    mode_manager.detect_and_switch()
                    last_mode_detection_time = current_time
                    if mode_manager.state.current_mode != previous:
                        mode_render_time = 0.0
                        last_second = None
                        last_date = None
                        last_minute = None
                        last_track_key = ""
                        last_mode = mode_manager.state.current_mode
                        overlay_active = False
                        apply_mode_brightness(lcd, mode_manager.state.current_mode)
                        if mode_manager.state.current_mode == Mode.MAIN:
                            theme = load_system_theme()
                            last_theme_fingerprint = theme.get("fingerprint", "")
                            last_theme_check_at = current_time
                            refresh_full_frame(lcd, render_dashboard(now, last_notification))
                            last_second = now.strftime("%H:%M:%S")
                            last_date = now.strftime("%Y-%m-%d")
                            last_minute = now.strftime("%H:%M")
                            last_soft_nudge_at = current_time
                            continue
                        last_soft_nudge_at = current_time

                # --- Notifications: never show overlays while LOCKED ---
                if mode_manager.state.current_mode == Mode.LOCKED:
                    drained = None
                    try:
                        while True:
                            drained = notification_queue.get_nowait()
                    except queue.Empty:
                        pass
                    if drained is not None:
                        last_notification = drained
                    if overlay_active:
                        overlay_active = False
                        refresh_full_frame(lcd, render_locked_mode(now, load_system_theme()))
                        last_second = now.strftime("%H:%M:%S")
                        last_soft_nudge_at = current_time
                else:
                    try:
                        item = notification_queue.get_nowait()
                        while True:
                            item = notification_queue.get_nowait()
                    except queue.Empty:
                        if "item" in locals():
                            last_notification = item
                            display_image(lcd, render_notification(item, now))
                            overlay_until = time.monotonic() + NOTIFICATION_SECONDS
                            overlay_active = True
                            del item

                if overlay_active:
                    if mode_manager.state.current_mode == Mode.LOCKED:
                        overlay_active = False
                        refresh_full_frame(lcd, render_locked_mode(now, load_system_theme()))
                        last_second = now.strftime("%H:%M:%S")
                        continue
                    if time.monotonic() >= overlay_until:
                        redraw_current_mode(lcd, mode_manager, now, last_notification)
                        overlay_active = False
                        mode_render_time = 0.0
                        last_second = now.strftime("%H:%M:%S")
                        last_date = now.strftime("%Y-%m-%d")
                        last_minute = now.strftime("%H:%M")
                    time.sleep(0.05)
                    continue

                # Clear dashboard notification area after retention idle
                if last_notification is not None:
                    age = (now - last_notification.received_at).total_seconds()
                    if age >= NOTIFICATION_RETENTION_SECONDS:
                        logger.info("Clearing notification area after %.0f min idle", age / 60)
                        last_notification = None
                        if mode_manager.state.current_mode == Mode.MAIN:
                            refresh_full_frame(lcd, render_dashboard(now, None))
                            last_date = now.strftime("%Y-%m-%d")
                            last_minute = now.strftime("%H:%M")
                            last_second = now.strftime("%H:%M:%S")

                # Watchdog: screen went dark / serial silent without raising
                if needs_watchdog_recovery():
                    raise serial.SerialException(
                        f"watchdog: no successful frame for {WATCHDOG_SECONDS}s"
                    )

                # Periodic soft nudge: wakes a blank panel even when writes "succeed"
                if current_time - last_soft_nudge_at >= SOFT_NUDGE_SECONDS:
                    last_soft_nudge_at = current_time
                    logger.info("Periodic soft nudge (brightness + redraw)")
                    try:
                        lcd._wake_panel()
                        redraw_current_mode(lcd, mode_manager, now, last_notification)
                    except (serial.SerialException, OSError) as nudge_exc:
                        raise serial.SerialException(f"nudge failed: {nudge_exc}") from nudge_exc

                # --- Render based on current mode ---
                try:
                    if mode_manager.state.current_mode == Mode.MAIN:
                        # MAIN mode: clock + notifications (with partial refresh optimization)
                        current_second = now.strftime("%H:%M:%S")
                        current_date = now.strftime("%Y-%m-%d")
                        current_minute = now.strftime("%H:%M")
                        if current_second != last_second:
                            updated = render_dashboard(now, last_notification)
                            x1, y1, x2, y2 = MAIN_CLOCK_CROP
                            display_image(lcd, updated.crop((x1, y1, x2, y2)), x1, y1)
                            last_second = current_second
                        if current_date != last_date or current_minute != last_minute:
                            refresh_full_frame(lcd, render_dashboard(now, last_notification))
                            last_date = current_date
                            last_minute = current_minute

                    elif mode_manager.state.current_mode == Mode.MULTIMEDIA:
                        # Avoid hammering Rev A with full frames + MPRIS every second
                        media = mode_manager.state.multimedia
                        track_key = f"{media.title}|{media.artist}|{media.app_name}"
                        due = current_time - mode_render_time >= MODE_CACHE_TTL
                        track_changed = track_key != last_track_key
                        if due or track_changed or mode_render_time == 0.0:
                            mode_manager.state.multimedia = mode_manager.multimedia_detector.detect()
                            media = mode_manager.state.multimedia
                            track_key = f"{media.title}|{media.artist}|{media.app_name}"
                            mode_render_cache = render_multimedia_mode(media, now)
                            mode_render_time = current_time
                            last_track_key = track_key
                            display_image(lcd, mode_render_cache)

                    elif mode_manager.state.current_mode == Mode.GAMER:
                        if current_time - mode_render_time >= MODE_CACHE_TTL:
                            mode_manager.state.gamer = mode_manager.gamer_detector.detect()
                            mode_render_cache = render_gamer_mode(
                                mode_manager.state.gamer, now
                            )
                            mode_render_time = current_time
                            display_image(lcd, mode_render_cache)

                    elif mode_manager.state.current_mode == Mode.LOCKED:
                        if current_time - last_theme_check_at >= THEME_WATCH_SECONDS:
                            last_theme_check_at = current_time
                            theme = load_system_theme()
                            fp = theme.get("fingerprint", "")
                            if fp and fp != last_theme_fingerprint:
                                logger.info("Redrawing LOCKED after desktop environment change")
                                last_theme_fingerprint = fp
                                refresh_full_frame(lcd, render_locked_mode(now, theme))
                                last_second = now.strftime("%H:%M:%S")
                                continue

                        current_second = now.strftime("%H:%M:%S")
                        if current_second != last_second:
                            updated = render_locked_mode(now, load_system_theme())
                            x1, y1, x2, y2 = LOCKED_CLOCK_CROP
                            display_image(lcd, updated.crop((x1, y1, x2, y2)), x1, y1)
                            last_second = current_second
                except Exception as exc:
                    # Serial failures must use the recovery path below
                    if isinstance(exc, (serial.SerialException, OSError)):
                        raise
                    logger.error("Mode render failed (%s); staying on current mode", exc)
                    time.sleep(0.5)

                time.sleep(0.05)

            except (serial.SerialException, OSError) as exc:
                overlay_active = False
                last_second = None
                last_date = None
                last_minute = None
                mode_render_cache = None
                mode_render_time = 0.0
                last_track_key = ""
                recover_display(lcd, mode_manager, last_notification, str(exc))

    finally:
        if monitor_process and monitor_process.poll() is None:
            monitor_process.terminate()
        lcd.closeSerial()
