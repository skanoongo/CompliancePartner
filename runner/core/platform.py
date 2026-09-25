#!/usr/bin/env python3
"""
Shared capture platform: logging, phases, screen capture, and the browser.

Everything here is system-agnostic. It was originally inside the Workato change
management script, which meant the NetSuite captures had to import from Workato
to take a screenshot - a dependency that said the opposite of what the code does.
It lives here so each system's package depends on this and never on a sibling.

The screen layer is the reason this is not just "utils": the evidence framing -
whole-screen capture including the URL bar and a clock, and a browser window
positioned to leave the clock strip visible - has to be identical for every
system, or two workbooks in the same review would not be comparable.
"""

import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# Phases the job API watches for. Emitted as their own line so the reader greps for
# a marker instead of pattern-matching prose that changes.
PHASES = ("starting", "awaiting_login", "checking_workspace", "capturing",
          "building_workbook", "done")


def phase(name, detail=""):
    assert name in PHASES, name
    print(f"PHASE::{name}::{detail}", flush=True)


def slug(s, n=40):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:n]


def now_stamp():
    return datetime.now().astimezone()

# --------------------------------------------------------------------------- #
# platform layer                                                               #
#                                                                              #
# Everything that touches the screen lives here, so the rest of the script is  #
# platform-blind. macOS keeps its original screencapture/AppleScript path;     #
# Linux and Windows grab the screen through mss.                              #
# --------------------------------------------------------------------------- #
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
IS_WINDOWS = platform.system() == "Windows"

# The container reserves a strip at the top of the X display for the clock and
# starts the browser below it, so the browser can never cover the timestamp.
WINDOW_OFFSET_Y = int(os.environ.get("CAPTURE_WINDOW_OFFSET_Y", "0"))


def screen_size():
    """Whole-screen size in points. Falls back to 1440x900."""
    if IS_MAC:
        try:
            out = subprocess.check_output(
                ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                text=True, timeout=10,
            )
            _, _, w, h = [int(x.strip()) for x in out.strip().split(",")]
            return w, h
        except Exception:
            return 1440, 900

    if IS_LINUX:
        # SCREEN_W/SCREEN_H are what the container told Xvfb to create, so they are
        # more trustworthy than parsing xdpyinfo; fall back to xdpyinfo, then default.
        try:
            return int(os.environ["SCREEN_W"]), int(os.environ["SCREEN_H"])
        except (KeyError, ValueError):
            pass
        try:
            out = subprocess.check_output(["xdpyinfo"], text=True, timeout=10)
            m = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # Per-monitor DPI aware, so the numbers below are real pixels and the
            # browser window we size from them is not silently scaled.
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
            w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
            h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
            if w and h:
                return int(w), int(h)
        except Exception:
            pass

    return 1440, 900


def _activate_windows(app_name):
    """Raise the first top-level window whose title mentions app_name."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    target = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if app_name.lower() in buf.value.lower():
            target.append(hwnd)
            return False        # stop at the first match
        return True

    user32.EnumWindows(each, 0)
    if not target:
        return
    hwnd = target[0]
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE, in case it is minimised
    user32.SetForegroundWindow(hwnd)


def activate_app(app_name):
    """Bring the browser window to the front so the OS screenshot shows it.
    Matches by name substring ("Chrom" covers Chromium, Google Chrome, and
    Playwright's "Google Chrome for Testing")."""
    try:
        if IS_MAC:
            script = (
                'tell application "System Events"\n'
                f'  set ps to (every process whose name contains "{app_name}")\n'
                '  if (count of ps) > 0 then set frontmost of item 1 of ps to true\n'
                'end tell'
            )
            subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        elif IS_LINUX:
            # Xvfb runs without a window manager, so there is usually nothing to
            # raise - the browser is the only mapped window. Best-effort only.
            if shutil.which("wmctrl"):
                subprocess.run(["wmctrl", "-a", app_name], timeout=10, capture_output=True)
        elif IS_WINDOWS:
            _activate_windows(app_name)
    except Exception:
        pass


def _mss_grab(path, display):
    """Grab a whole screen through mss - browser, URL bar and the on-screen clock.

    mss.monitors[0] is the bounding box of every screen; [1] is the primary. That
    makes `display` mean the same thing it does for macOS screencapture -D.
    """
    import mss  # imported lazily: only the mss platforms need it

    with mss.mss() as sct:
        mons = sct.monitors
        mon = mons[display] if 0 < display < len(mons) else mons[0]
        shot = sct.grab(mon)
    Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(path)


def grab_screen(path, display=1):
    """Whole-screen capture: browser, URL bar, clock. Same framing on all platforms."""
    if IS_MAC:
        subprocess.run(["screencapture", "-x", "-D", str(display), str(path)],
                       check=True, timeout=30)
        return
    if IS_LINUX or IS_WINDOWS:
        _mss_grab(path, display)
        return
    raise RuntimeError(f"No screen-capture backend for {platform.system()}")


def interactive():
    """True only when there is a real terminal to prompt on.

    The container runs this script with no TTY. Without this guard select() on a
    redirected stdin reports "readable" immediately, which the login wait would
    read as the operator pressing Enter and march on before SSO finished.
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def workbook_copy(src, dst_dir, scale=0.5):
    """Half-size JPEG copy for the workbook (Retina PNGs are too heavy to embed 30x)."""
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / (Path(src).stem + ".jpg")
    img = Image.open(src).convert("RGB")
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img.save(dst, quality=85, optimize=True)
    return str(dst)


SCROLL_JS = """
() => {
  const els = [document.scrollingElement, ...document.querySelectorAll('*')];
  let best = null;
  for (const el of els) {
    if (!el) continue;
    const st = getComputedStyle(el);
    const scrollable = el === document.scrollingElement || /(auto|scroll)/.test(st.overflowY);
    if (scrollable && el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 300) {
      if (!best || el.clientHeight > best.clientHeight) best = el;
    }
  }
  if (!best) return null;
  best.setAttribute('data-sox-scroll', '1');
  return {top: best.scrollTop, height: best.clientHeight, total: best.scrollHeight};
}
"""
SCROLL_BY_JS = """
(dy) => { const el = document.querySelector('[data-sox-scroll]') || document.scrollingElement;
          el.scrollTop += dy; return el.scrollTop; }
"""

def enter_pressed():
    """Non-blocking check for Enter in the terminal. False when there is no terminal.

    Without the interactive() guard a redirected stdin (the container, or any
    `< /dev/null` run) reports readable straight away, which would be read as the
    operator confirming login and start the capture before SSO had finished.
    """
    if not interactive():
        return False
    if IS_WINDOWS:
        try:
            import msvcrt
        except ImportError:
            return False
        pressed = False
        while msvcrt.kbhit():
            if msvcrt.getwch() in ("\r", "\n"):
                pressed = True
        return pressed
    import select
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if r:
        sys.stdin.readline()
        return True
    return False

APP_NAME = "Chrom"  # substring match: Chromium / Google Chrome / Google Chrome for Testing


def launch_browser(p, profile, browser="chromium"):
    """Open the persistent browser, sized to leave the clock strip uncovered.

    Shared by every capture in this repo. The window geometry is part of how the
    evidence is framed, so it must not be re-derived per script and drift.
    """
    profile_dir = Path(profile).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    sw, sh = screen_size()
    kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=False,
        no_viewport=True,  # let the real window size drive the page
        args=[
            "--disable-blink-features=AutomationControlled",
            # Leave the reserved strip (the clock) uncovered. WINDOW_OFFSET_Y is 0
            # on a normal desktop, where the OS keeps its own bar on top anyway.
            f"--window-position=0,{WINDOW_OFFSET_Y}",
            f"--window-size={sw},{sh - WINDOW_OFFSET_Y}",
        ],
    )
    if browser == "chrome":
        kwargs["channel"] = "chrome"
    return p.chromium.launch_persistent_context(**kwargs)

PX_PER_ROW = 20  # default 15pt row ~ 20px
