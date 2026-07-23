#!/usr/bin/env python3
"""Tech dashboard with Pop!_OS/COSMIC notification mirroring and multi-mode support.

Modes:
  - MAIN (default): Clock, date, and notification dashboard
  - MULTIMEDIA:     Spotify-like media info display (auto-detected via MPRIS2)
  - GAMER:          Gaming overlay with game info, FPS, hardware stats (auto-detected)

Usage:
  python clock-display.py                    # Auto-detect mode
  python clock-display.py --mode main        # Force MAIN mode
  python clock-display.py --mode multimedia  # Force MULTIMEDIA mode
  python clock-display.py --mode gamer       # Force GAMER mode
"""

import argparse
import hashlib
import html
import io
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import serial
from PIL import Image, ImageDraw, ImageFont

from library.lcd.lcd_comm import LcdComm
from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation
from library.log import logger

# Import mode system
from modes import Mode, ModeManager, MultimediaInfo, GamerInfo  # noqa: F401
from modes import render_multimedia_mode, render_gamer_mode  # noqa: F401
from modes import _cover_fit, _extract_theme_colors  # noqa: F401


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Turing Smart Screen — Clock Dashboard with Multi-Modes")
    parser.add_argument(
        "--mode",
        choices=["main", "multimedia", "gamer"],
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
GRID = (10, 20, 34)
ACCENT_LINE = (24, 48, 72)
FONT_REGULAR = "res/fonts/roboto/Roboto-Regular.ttf"
FONT_MEDIUM = "res/fonts/roboto/Roboto-Medium.ttf"
FONT_BOLD = "res/fonts/roboto/Roboto-Bold.ttf"
FONT_MONO = "res/fonts/roboto-mono/RobotoMono-Bold.ttf"
NOTIFICATION_SECONDS = 5
NOTIFICATION_RETENTION_SECONDS = 30 * 60  # clear dashboard note after 30 min idle
BRIGHTNESS = 100
ORIENTATION = Orientation.REVERSE_LANDSCAPE
SERIAL_WRITE_TIMEOUT = 2
RECOVER_SLEEP_SECONDS = 3
WATCHDOG_SECONDS = 12  # no successful frame → force recovery
RECOVERY_COOLDOWN_SECONDS = 5
SOFT_NUDGE_SECONDS = 45  # periodic soft reconnect to wake a blank panel
# After cold boot the first Reset often "succeeds" while the panel stays blank.
BOOT_CONFIRM_HARD_SECONDS = 20

running = True
notification_queue = queue.Queue()
monitor_process = None
last_successful_frame_at = 0.0
last_recovery_at = 0.0
last_soft_nudge_at = 0.0
recovery_hard_next = False


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

    def _wake_panel(self):
        """Rev A often accepts serial writes while the panel stays dark — force on."""
        try:
            self.ScreenOn()
        except Exception as exc:
            logger.debug("ScreenOn ignored: %s", exc)
        self.SetBrightness(level=BRIGHTNESS)
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
_SYSTEM_THEME_CHECKED_AT = 0.0
_SYSTEM_THEME_TTL = 30.0  # re-read wallpaper/accent periodically


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


def _cosmic_wallpaper_path() -> Path | None:
    root = Path.home() / ".config/cosmic/com.system76.CosmicBackground/v1"
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("output.*")) + [root / "all"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = re.search(r'Path\("([^"]+)"\)', text)
        if match:
            wallpaper = Path(match.group(1))
            if wallpaper.is_file():
                return wallpaper
    return None


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


def load_system_theme(force: bool = False) -> dict:
    """Load Pop!_OS / COSMIC wallpaper + accent colors (cached)."""
    global _SYSTEM_THEME_CACHE, _SYSTEM_THEME_CHECKED_AT
    now = time.monotonic()
    if (
        not force
        and _SYSTEM_THEME_CACHE is not None
        and now - _SYSTEM_THEME_CHECKED_AT < _SYSTEM_THEME_TTL
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

    theme = {
        "wallpaper": wallpaper,
        "wallpaper_path": str(wallpaper_path) if wallpaper_path else "",
        "accent": accent,
        "accent2": accent2,
        "panel": panel,
        "is_dark": _cosmic_is_dark(),
    }
    _SYSTEM_THEME_CACHE = theme
    _SYSTEM_THEME_CHECKED_AT = now
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
MAIN_CLOCK_CROP = (18, 48, 360, 150)


def render_dashboard(now, last_notification=None):
    """MAIN mode: wallpaper-first desktop HUD matching GAMER/MULTIMEDIA energy."""
    import socket
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
    wash_draw.rectangle((0, 0, WIDTH, 44), fill=(ar, ag, ab, 75))
    # Soft contrast behind clock / date (same idea as MULTIMEDIA)
    wash_draw.rounded_rectangle((14, 40, 466, 168), radius=16, fill=(0, 0, 0, 130))
    for y in range(210, HEIGHT):
        t = (y - 210) / max(1, HEIGHT - 210)
        wash_draw.line((0, y, WIDTH, y), fill=(4, 6, 10, int(50 + 190 * t)))
    image = Image.alpha_composite(image, wash)

    image_rgb = image.convert("RGB")
    draw = ImageDraw.Draw(image_rgb)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=accent, width=3)

    host = socket.gethostname().split(".")[0][:18].upper() or "POP OS"
    badge = f"COSMIC · {host}"
    badge_w = max(120, int(draw.textlength(badge, font=font(FONT_MONO, 12)) + 24))
    draw.rounded_rectangle((22, 18, 22 + badge_w, 40), radius=8, fill=accent)
    draw.text((22 + badge_w // 2, 29), badge, font=font(FONT_MONO, 12), fill=(12, 12, 14), anchor="mm")

    clock = now.strftime("%H:%M:%S")
    draw.text((26, 76), clock, font=font(FONT_MONO, 58), fill=(0, 0, 0), anchor="lm")
    draw.text((24, 74), clock, font=font(FONT_MONO, 58), fill=WHITE, anchor="lm")

    date_str = now.strftime("%d %b").upper()
    year_str = now.strftime("%Y")
    day_str = now.strftime("%A").upper()
    draw.text((446, 58), date_str, font=font(FONT_BOLD, 22), fill=accent, anchor="ra")
    draw.text((446, 86), year_str, font=font(FONT_MONO, 16), fill=WHITE, anchor="ra")
    draw.text((446, 112), day_str, font=font(FONT_MEDIUM, 16), fill=accent2, anchor="ra")

    draw.rectangle((22, 160, 458, 163), fill=accent)

    note_scrim = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(note_scrim).rounded_rectangle((14, 176, 466, 308), radius=16, fill=(0, 0, 0, 140))
    image_rgb = Image.alpha_composite(image_rgb.convert("RGBA"), note_scrim).convert("RGB")
    draw = ImageDraw.Draw(image_rgb)

    icon_box = (28, 192, 116, 280)
    draw.rounded_rectangle(
        (icon_box[0] - 3, icon_box[1] - 3, icon_box[2] + 3, icon_box[3] + 3),
        radius=14,
        outline=accent,
        width=3,
    )
    draw.rounded_rectangle(icon_box, radius=12, fill=(10, 12, 16))

    if last_notification is None:
        draw.line((52, 236, 92, 236), fill=accent, width=4)
        draw.line((72, 216, 72, 256), fill=accent, width=4)
        draw.text((136, 208), "Nenhuma notificação", font=font(FONT_BOLD, 26), fill=WHITE)
        draw.text((136, 248), "Desktop pronto · aguardando alertas", font=font(FONT_REGULAR, 16), fill=accent)
        draw.text((446, 188), now.strftime("%H:%M"), font=font(FONT_MONO, 14), fill=MUTED, anchor="ra")
    else:
        app_icon = load_notification_icon(last_notification.icon, last_notification.app, 80)
        if app_icon is not None:
            paste_rounded_icon(image_rgb, app_icon, icon_box, radius=12)
        else:
            draw.text((72, 236), "!", font=font(FONT_BOLD, 34), fill=WHITE, anchor="mm")
        draw.text(
            (446, 188),
            last_notification.received_at.strftime("%H:%M"),
            font=font(FONT_MONO, 14),
            fill=MUTED,
            anchor="ra",
        )
        app_name = (last_notification.app or "SISTEMA").upper()
        draw.text((136, 192), app_name, font=font(FONT_MONO, 12), fill=accent)
        title_font = font(FONT_BOLD, 24)
        body_font = font(FONT_REGULAR, 18)
        title = wrap_text(draw, last_notification.title, title_font, 290, 1)
        body = wrap_text(draw, clean_notification_body(last_notification.body), body_font, 290, 2)
        draw.text((136, 214), title[0] if title else "Nova notificação", font=title_font, fill=WHITE)
        body_y = 250
        for line in body:
            draw.text((136, body_y), line, font=body_font, fill=(230, 232, 240))
            body_y += 24

    return image_rgb


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


def notification_monitor():
    """Watch Notify calls; prefer image-path (site icon) over app_icon (browser logo)."""
    global monitor_process
    command = [
        "stdbuf", "-oL", "dbus-monitor", "--session",
        "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
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

        def finalize_note():
            nonlocal capturing, values, image_path, pending_hint, multiline, multiline_target
            if len(values) < 4:
                capturing = False
                return
            app, app_icon, title, body = values[0], values[1], values[2], values[3]
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
            capturing = False
            values = []
            image_path = ""
            pending_hint = None
            multiline = None
            multiline_target = None

        for raw_line in monitor_process.stdout:
            if not running:
                break
            line = raw_line.rstrip("\n")
            if line.startswith("method call") and "member=Notify" in line:
                capturing = True
                values = []
                image_path = ""
                pending_hint = None
                multiline = None
                multiline_target = None
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
    # Alternate: soft first, then hard if we recover again soon
    if "watchdog" in reason.lower() or "nudge" in reason.lower():
        recovery_hard_next = False
    wait_for_display(lcd)
    if not running:
        return False
    try:
        redraw_current_mode(lcd, mode_manager, datetime.now().astimezone(), last_notification)
        last_soft_nudge_at = time.monotonic()
        logger.info("Display recovery succeeded")
        # Next failure within a short window uses hard reset
        recovery_hard_next = True
        return True
    except (serial.SerialException, OSError) as redraw_exc:
        logger.warning("Redraw after recovery failed (%s); escalating to hard reset", redraw_exc)
        recovery_hard_next = True
        return False


def needs_watchdog_recovery() -> bool:
    if last_successful_frame_at <= 0:
        return False
    return (time.monotonic() - last_successful_frame_at) >= WATCHDOG_SECONDS


# ---------------------------------------------------------------------------
# Mode manager initialization
# ---------------------------------------------------------------------------

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
    if initial_mode == Mode.MAIN:
        refresh_full_frame(lcd, render_dashboard(datetime.now().astimezone(), last_notification))
    elif initial_mode == Mode.MULTIMEDIA:
        refresh_full_frame(lcd, render_multimedia_mode(mode_manager.state.multimedia, datetime.now().astimezone()))
    elif initial_mode == Mode.GAMER:
        refresh_full_frame(lcd, render_gamer_mode(mode_manager.state.gamer, datetime.now().astimezone()))

    # Detection interval (how often to check for mode changes)
    DETECTION_INTERVAL = 3.0  # seconds

    # Mode-specific state
    last_mode_detection_time = 0.0
    mode_render_cache = None
    mode_render_time = 0.0
    MODE_CACHE_TTL = 3.0  # full frames are expensive on Rev A serial
    last_track_key = ""
    last_mode = mode_manager.state.current_mode
    last_soft_nudge_at = time.monotonic()
    boot_confirm_at = time.monotonic() + BOOT_CONFIRM_HARD_SECONDS
    boot_confirm_done = False

    while running:
        try:
            now = datetime.now().astimezone()
            current_time = time.monotonic()

            # Cold-boot: first "hard-ready" often leaves Rev A blank — second Reset wakes it.
            if not boot_confirm_done and current_time >= boot_confirm_at:
                boot_confirm_done = True
                logger.info("Boot confirm: second hard reset to wake panel after login")
                recovery_hard_next = True
                last_recovery_at = 0.0  # bypass cooldown for this one-shot
                if recover_display(lcd, mode_manager, last_notification, "boot confirm hard reset"):
                    mode_render_time = 0.0
                    last_second = None
                    last_date = None
                    last_minute = None
                    last_track_key = ""
                continue

            # --- Mode detection (periodic, paused during notification overlay) ---
            if not overlay_active and current_time - last_mode_detection_time >= DETECTION_INTERVAL:
                previous = mode_manager.state.current_mode
                mode_manager.detect_and_switch()
                last_mode_detection_time = current_time
                if mode_manager.state.current_mode != previous:
                    # Force an immediate full redraw on mode change
                    mode_render_time = 0.0
                    last_second = None
                    last_date = None
                    last_minute = None
                    last_track_key = ""
                    last_mode = mode_manager.state.current_mode
                    overlay_active = False

            # --- Notifications: overlay for 5s in any mode ---
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
                if time.monotonic() >= overlay_until:
                    redraw_current_mode(lcd, mode_manager, now, last_notification)
                    overlay_active = False
                    mode_render_time = 0.0
                    last_second = now.strftime("%H:%M:%S")
                    last_date = now.strftime("%Y-%m-%d")
                    last_minute = now.strftime("%H:%M")
                time.sleep(0.05)
                continue

            # Clear dashboard notification area after 30 minutes without a new one
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
                    elif media.is_playing and media.length > 0:
                        media.position = min(media.length, media.position + 0.2)

                elif mode_manager.state.current_mode == Mode.GAMER:
                    if current_time - mode_render_time >= MODE_CACHE_TTL:
                        mode_manager.state.gamer = mode_manager.gamer_detector.detect()
                        mode_render_cache = render_gamer_mode(
                            mode_manager.state.gamer, now
                        )
                        mode_render_time = current_time
                        display_image(lcd, mode_render_cache)
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
