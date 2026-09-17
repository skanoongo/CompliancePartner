#!/usr/bin/env python3
"""
Workato CM Review (SOX) - automated screenshot capture + workbook builder.

READ-ONLY. This script only navigates Workato pages and takes screenshots.
It never starts, stops, edits, creates, or deletes anything.

Screenshots are WHOLE-SCREEN captures, so they show the browser URL bar and an
on-screen clock alongside the page itself - the evidence shows where the page came
from and when, not just what it said.

Runs on three platforms, with the same output on each:

  macOS      screencapture(1), same as Cmd+Shift+3. The clock in the shot is the Mac
             menu-bar clock. macOS asks once to allow Terminal "Screen Recording".
             Keep the browser window on screen while it runs.
  Linux/X11  grabs the X screen (this is what the Docker image does). The clock in the
             shot is an xclock running in the same X display, in a strip the browser
             window is positioned below so it cannot cover it.
  Windows    grabs the desktop, same as PrtScn. The clock in the shot is the taskbar
             clock, so leave the taskbar visible - do not run the browser full-screen.

Usage
-----
  # first run (opens a browser window; log in via SSO once when prompted)
  python3 workato_sox_capture.py

  # options
  python3 workato_sox_capture.py --month "September 2026"
  python3 workato_sox_capture.py --capture-only        # screenshots + manifest only
  python3 workato_sox_capture.py --build-only          # rebuild workbook from last manifest
  python3 workato_sox_capture.py --browser chrome      # use installed Google Chrome instead of bundled Chromium
  python3 workato_sox_capture.py --no-pause            # skip the "confirm workspace" prompt

Setup (once)
------------
  pip3 install playwright openpyxl pillow mss
  python3 -m playwright install chromium

Or skip all of that and use the container: see the CompliancePartner README.
"""

import argparse
import json
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

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Font
from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "https://app.workato.com"

# --------------------------------------------------------------------------- #
# What to capture. Recipe IDs are DISCOVERED from each folder's assets page;  #
# the "known" entries are only a fallback/validation if discovery fails.      #
# fid=None means the folder id is resolved from the known recipe's page.      #
# --------------------------------------------------------------------------- #
SECTIONS = [
    {
        # Excel sheet names max 31 chars ("1.Workday Payroll to NetSuite GL" is 32).
        # Rename in Google Sheets after upload if the exact template name is needed.
        "tab": "1. Workday Payroll to NS GL",
        "folders": [
            {
                "name": "Workday Payroll to NetSuite GL",
                "fid": None,
                "known": {"69111768": "Workday Payroll to NetSuite GL"},
                "expected": 1,
            },
        ],
    },
    {
        "tab": "2. CPQ",
        "folders": [
            {
                "name": "CPQ > Customer",
                "fid": "27517768",
                "known": {
                    "67397134": "CPQ | FUNC | SFDC to NS Customer Creation",
                    "67397133": "AdhocCustomerCreate",
                },
                "expected": 2,
            },
            {
                "name": "CPQ > Products",
                "fid": "27517769",
                "known": {
                    "67397135": "CPQ | FUNC | SFDC to NS Product Creation",
                    "67397136": "CPQ | SFDC to NS Product Creation",
                },
                "expected": 2,
            },
            {
                "name": "CPQ > Sales Order",
                "fid": "27517770",
                "known": {"67397137": "CPQ | SFDC to NS Sales Order SYNC"},
                "expected": 1,
            },
        ],
    },
    {
        "tab": "3. EDI - Arvato",
        "folders": [
            {
                "name": "EDI > Arvato",
                "fid": "30160599",
                "known": {
                    "70996708": "944_EDI | Sub: Item Receipt for Arvato",
                    "71436644": "945_EDI | Sub: Item Fulfillment for Arvato",
                    "70996481": "API_Arvato",
                    "71436642": "940_EDI | TO For Arvato",
                    "71436643": "945_EDI | Item Fulfillment for Arvato",
                    "70996707": "944_EDI | Item Receipt for Arvato",
                    "79574351": "864_EDI | TO freeze from Arvato",        # added Aug 2026
                    "79574352": "sub: 864_EDI| TO freeze",                 # added Aug 2026
                },
                "expected": 8,
            },
        ],
    },
    {
        "tab": "3.1 EDI - Common",
        "folders": [
            {
                "name": "EDI > Common",
                "fid": "30160786",
                "known": {
                    "70996713": "Sub: PO Header data validation",
                    "70996714": "Sub: TO Header data validation",
                    "70996711": "Sub: GenerateSuccessReport",
                    "70996710": "Sub: GenerateErrorReport",
                    "70996712": "Sub: Item Quantity Validation",
                    "73002661": "EDI_Staus_Report_944",
                    "71436645": "Sub: Check Orderful Transaction Status",
                    "71436646": "Sub: Load SKU-Serial data from Netsuite",
                    "71246613": "Sub: Get NS job Status",
                    "70996709": "Sub: Approve delivery in Orderful",
                },
                "expected": 10,
            },
        ],
    },
    {
        # Excel limits sheet names to 31 chars; rename to
        # "6. CoStar to NetSuite Fx Rates update" after uploading to Google Sheets if desired.
        "tab": "6. CoStar to NS Fx Rates update",
        "folders": [],
    },
]

RECIPE_RE = re.compile(r"/recipes/(\d+)")
FID_RE = re.compile(r"[?&]fid=(\d+)")


def project_names():
    """The projects that can be captured - one per workbook tab."""
    return [s["tab"] for s in SECTIONS]


def select_sections(selection):
    """Narrow SECTIONS to the chosen projects.

    `selection` is a comma-separated list of project names, matched against the tab
    name and against folder names inside it, case-insensitively. Empty, "all" or no
    match on a name raises rather than silently capturing everything: a review
    scoped to one project that quietly captured all of them would be wrong in a way
    nobody would notice until it was in front of an auditor.
    """
    if not selection or selection.strip().lower() in ("", "all", "*"):
        return SECTIONS

    wanted = [w.strip().lower() for w in selection.split(",") if w.strip()]
    chosen, unmatched = [], []
    for w in wanted:
        hit = [s for s in SECTIONS
               if w == s["tab"].lower()
               or w in s["tab"].lower()
               or any(w in f["name"].lower() for f in s["folders"])]
        if not hit:
            unmatched.append(w)
        for s in hit:
            if s not in chosen:
                chosen.append(s)

    if unmatched:
        raise SystemExit(
            "No project matches: " + ", ".join(unmatched) + "\n"
            "Known projects: " + " | ".join(project_names()))
    return chosen


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
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


def assets_url(fid):
    return f"{BASE}/?fid={fid}&asset_type=recipe#assets"


def versions_url(rid):
    return f"{BASE}/recipes/{rid}?change_type=major#versions"


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


# --------------------------------------------------------------------------- #
# browser side                                                                 #
# --------------------------------------------------------------------------- #
class Capturer:
    def __init__(self, page, out_dir, settle, month, app_name, display, max_parts, workspace):
        self.page = page
        self.out_dir = out_dir
        self.settle = settle
        self.month = month
        self.app_name = app_name
        self.display = display
        self.max_parts = max_parts
        self.workspace = workspace
        self.seq = 0
        self.manifest = []
        self.warnings = []

    def warn(self, msg):
        """Record a warning in one place, so it reaches BOTH the log and the manifest.

        Warnings that only ever went to the log were invisible to anyone reading the
        workbook, and warnings that only went to the manifest were invisible while a
        run was still going. Everything that needs a human goes through here.
        """
        log(f"  !! {msg}")
        self.warnings.append(msg)

    def goto(self, url, must_contain=None, retries=3):
        for attempt in range(1, retries + 1):
            self.page.goto(url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(self.settle)
            if must_contain is None or must_contain in self.page.url:
                return True
            log(f"  ! landed on {self.page.url} (expected '{must_contain}') - retry {attempt}/{retries}")
            time.sleep(2)
        return False

    def shoot(self, tab, kind, ident, desc, url):
        """Whole-screen capture. If the page is taller than the screen, scroll and
        capture additional parts (part 2, 3, ...) up to max_parts."""
        self.seq += 1
        if self.workspace and not in_workspace(self.page, self.workspace):
            self.warn(f"'{self.workspace}' not visible on page for: {desc}  ({url}) "
                      f"- CHECK THIS SCREENSHOT")
        self.page.bring_to_front()
        activate_app(self.app_name)
        time.sleep(1.0)

        info = self.page.evaluate(SCROLL_JS)
        planned = 1
        if info:
            remaining = info["total"] - info["height"] - info["top"]
            # only bother scrolling if a meaningful amount of content is hidden (> 1/4 screen)
            if remaining > info["height"] * 0.25:
                planned = min(self.max_parts, 1 + math.ceil(remaining / max(1, info["height"] * 0.9)))

        entries = []
        for part in range(1, planned + 1):
            if part > 1:
                before = self.page.evaluate("() => (document.querySelector('[data-sox-scroll]') || document.scrollingElement).scrollTop")
                after = self.page.evaluate(SCROLL_BY_JS, int(info["height"] * 0.9))
                if after - before < 100:
                    break  # nothing actually scrolled -> no more parts
                time.sleep(1.0)
            ts = now_stamp()
            fname = (f"{self.seq:02d}_{slug(tab, 25)}_{kind}_{slug(ident, 12)}"
                     f"_{ts.strftime('%Y%m%d-%H%M%S')}_p{part}.png")
            path = self.out_dir / fname
            grab_screen(path, self.display)
            entries.append({"part": part, "ts": ts, "path": path})
            log(f"  captured -> {fname}")

        total = len(entries)
        for e in entries:
            path = e["path"]
            if total == 1:  # single shot: drop the _p1 suffix
                new = path.with_name(path.name.replace("_p1.png", ".png"))
                path.rename(new)
                path = new
            self.manifest.append(
                {
                    "seq": self.seq,
                    "part": e["part"],
                    "parts": total,
                    "tab": tab,
                    "kind": kind,
                    "description": desc + (f" (part {e['part']}/{total})" if total > 1 else ""),
                    "url": url,
                    "captured": e["ts"].isoformat(),
                    "file": str(path),
                }
            )

    def discover_recipes(self):
        """Return ordered {id: name} of recipe links visible on the current assets page."""
        links = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/recipes/"]'))
                     .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim()}))"""
        )
        found = {}
        for l in links:
            m = RECIPE_RE.search(l["href"])
            if not m:
                continue
            rid = m.group(1)
            # anchor text often includes breadcrumbs/status lines; keep the first line only
            name = l["text"].split("\n")[0].strip()
            if rid not in found or (not found[rid] and name):
                found[rid] = name
        return found

    def resolve_fid_from_recipe(self, rid, folder_name):
        self.goto(f"{BASE}/recipes/{rid}")
        links = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="fid="]'))
                     .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim()}))"""
        )
        best = None
        for l in links:
            m = FID_RE.search(l["href"])
            if not m:
                continue
            if l["text"].strip().lower() == folder_name.lower():
                return m.group(1)
            best = m.group(1)  # last breadcrumb-ish link as fallback
        return best

    def run_folder(self, tab, folder):
        name, fid, known, expected = folder["name"], folder["fid"], folder["known"], folder["expected"]
        log(f"Folder: {name}")

        if not fid:
            rid0 = next(iter(known))
            fid = self.resolve_fid_from_recipe(rid0, name)
            log(f"  resolved fid={fid} from recipe {rid0}")

        recipes = dict(known)
        if fid:
            ok = self.goto(assets_url(fid), must_contain=f"fid={fid}")
            if not ok:
                self.warn(f"could not stay on folder {fid} ({name}); the assets screenshot "
                          f"is of whatever Workato redirected to - check it")
            discovered = self.discover_recipes()
            if discovered:
                # discovered order first, then any known ones not on the page
                recipes = discovered
                for k, v in known.items():
                    recipes.setdefault(k, v)
                    if not recipes[k]:
                        recipes[k] = v
            else:
                log("  ! no recipe links found on assets page; falling back to known IDs")
            if len(discovered) < expected:
                log(f"  ! expected {expected} recipes, page shows {len(discovered)} - reloading once")
                self.page.reload(wait_until="domcontentloaded")
                time.sleep(self.settle + 2)
                d2 = self.discover_recipes()
                if len(d2) > len(discovered):
                    recipes = d2
                    for k, v in known.items():
                        recipes.setdefault(k, v)
                    discovered = d2
                if len(discovered) < expected:
                    # The reload did not recover it. Recorded as a warning rather than
                    # left as one "!" line in the log: a population short of what the
                    # scope says should be there is either a real change to write up
                    # or a capture that missed something, and both need a person.
                    self.warn(f"{name}: expected {expected} recipes, found "
                              f"{len(discovered)} after a reload - population may have "
                              f"changed, or the page did not load fully")
            self.shoot(tab, "assets", fid, f"{name} - Assets (filter: Recipes) - {len(recipes)} recipes", assets_url(fid))
        else:
            self.warn(f"folder id for {name} could not be resolved; no assets screenshot taken")

        for rid, rname in recipes.items():
            label = rname or known.get(rid) or f"recipe {rid}"
            log(f"  Recipe {rid}: {label}")
            self.goto(versions_url(rid), must_contain=f"/recipes/{rid}")
            self.shoot(tab, "versions", rid, f"{label} (ID {rid}) - Versions (filter: Recipe change)", versions_url(rid))


LOGIN_PATH_MARKERS = ("/users/sign_in", "/users/password", "/login", "okta.com", "accounts.google.com", "/saml")


def has_visible_password_field(page):
    """A visible password box means we are still being asked to authenticate."""
    for sel in PASSWORD_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                return True
        except Exception:
            continue
    return False


def looks_logged_in(page):
    """True when the browser is sitting on an authenticated Workato page.

    Checked structurally rather than by looking for words like "recipes" in the
    body: Workato lands signed-in users on pages (the AIRO home page, for one)
    that contain none of them, and a signed-in session would be missed.
    """
    u = page.url.lower()
    if not u.startswith(BASE) or any(m in u for m in LOGIN_PATH_MARKERS):
        return False
    if has_visible_password_field(page):
        return False
    txt = page_text(page).lower()
    if not txt.strip():
        return False                       # still loading; decide on the next pass
    # Any of the app-shell words is a positive signal, but their absence is not a
    # negative one - being on a non-login Workato URL with no password prompt is
    # already the definition of signed in.
    return True


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


def live_pages(ctx):
    return [p for p in ctx.pages if not p.is_closed()]


# Sign-in form fields, most specific first. Workato's own form plus the generic
# shapes an identity provider is likely to render.
EMAIL_SELECTORS = (
    "input[name='user[email]']", "input#user_email",
    "input[type='email']", "input[name='username']", "input[name='email']",
)
PASSWORD_SELECTORS = (
    "input[name='user[password]']", "input#user_password",
    "input[type='password']", "input[name='password']",
)
SUBMIT_SELECTORS = (
    "input[type='submit']", "button[type='submit']",
    "button:has-text('Log in')", "button:has-text('Sign in')",
)


def _fill_first(page, selectors, value):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def try_form_login(page, username, password):
    """Fill and submit the sign-in form. Returns True only if we got as far as
    submitting it - not that the login succeeded, which the caller checks.

    This deliberately cannot handle SSO redirects or MFA challenges. Those need a
    person, and the caller falls back to asking for one.
    """
    if not username or not password:
        return False
    try:
        page.goto(f"{BASE}/users/sign_in", wait_until="domcontentloaded")
        time.sleep(2)
        if not _fill_first(page, EMAIL_SELECTORS, username):
            log("  no email field on the sign-in page - handing over to a person")
            return False
        if not _fill_first(page, PASSWORD_SELECTORS, password):
            log("  no password field on the sign-in page - handing over to a person")
            return False
        for sel in SUBMIT_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    el.click()
                    break
            except Exception:
                continue
        else:
            page.keyboard.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        time.sleep(3)
        return True
    except Exception as exc:
        log(f"  form login could not be attempted: {type(exc).__name__}")
        return False


def wait_for_login(ctx, page, timeout_s=600, username="", password="", mode="interactive"):
    """Returns the page that is logged in (SSO may finish in a different tab), or None."""
    page.goto(BASE, wait_until="domcontentloaded")
    time.sleep(3)

    if mode == "form" and username and password and not looks_logged_in(page):
        log(f"Attempting form sign-in as {username}")
        if try_form_login(page, username, password):
            # Give the redirect chain time to land before judging it. A single
            # check here would call a slow but successful login a failure.
            for _ in range(15):
                if looks_logged_in(page):
                    log(f"Signed in with the configured credentials. At {page.url}")
                    return page
                time.sleep(2)
            log("Form sign-in did not complete - SSO or MFA needs a person.")

    start = time.time()
    last_print = 0
    last_probe = time.time()
    activate_app("Chrom")
    while time.time() - start < timeout_s:
        pages = live_pages(ctx)
        if not pages:
            log("!! The browser window was closed. Re-run the script and keep the window open.")
            return None
        for p in pages:
            try:
                if looks_logged_in(p):
                    return p
            except Exception:
                pass

        # Poking the page object is not enough on its own: a sign-in can complete
        # through redirects the page object does not end up reflecting, leaving a
        # signed-in browser that still reads as the login screen. Re-navigating to
        # the app settles it - with a session cookie we land inside, without one we
        # come straight back to sign-in and nothing is lost.
        if time.time() - last_probe > 20:
            last_probe = time.time()
            try:
                probe = pages[-1]
                probe.goto(BASE, wait_until="domcontentloaded")
                time.sleep(3)
                if looks_logged_in(probe):
                    return probe
            except Exception:
                pass
        if time.time() - last_print > 15:
            phase("awaiting_login", os.environ.get("CAPTURE_SESSION_URL", ""))
            log("Waiting for login in the browser window (it may be behind this Terminal).")
            for p in pages:
                log(f"   open tab: {p.url}")
            if os.environ.get("CAPTURE_SESSION_URL"):
                log(f"   -> open {os.environ['CAPTURE_SESSION_URL']} and sign in there.")
            else:
                log("   -> if you ARE logged in and see the Workato dashboard, press Enter here to continue.")
            last_print = time.time()
        if enter_pressed():
            log("Enter pressed - continuing with the most recently opened tab.")
            return pages[-1]
        time.sleep(2)
    return None


def page_text(page):
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def in_workspace(page, name):
    return name.lower() in page_text(page).lower()


def ensure_workspace(page, name, tries=20, wait_s=15):
    """Block until the Workato UI shows the expected workspace name (e.g. 'Production').
    The script never switches workspaces itself - you do it in the browser window.

    With a terminal this waits on Enter, as before. With no terminal (the container)
    it re-checks on a timer and then gives up: capturing the wrong workspace would
    produce evidence for the wrong population, which is worse than no evidence.
    """
    attempt = 0
    while True:
        page.goto(BASE, wait_until="domcontentloaded")
        time.sleep(3)
        if in_workspace(page, name):
            log(f"Workspace check OK: '{name}' detected in the page.")
            return
        attempt += 1
        print(f"\n!! Workspace '{name}' NOT detected on the current page.")
        print("   In the browser window, switch to CoreWeave > Production (workspace switcher, top-left),")
        if interactive():
            input("   then press Enter here to re-check... ")
            continue
        if attempt >= tries:
            raise SystemExit(
                f"Workspace '{name}' was never visible after {attempt} checks. "
                f"Open the browser session, switch to the right workspace, and re-run. "
                f"Nothing was captured."
            )
        log(f"   no terminal to prompt on - re-checking in {wait_s}s ({attempt}/{tries})")
        time.sleep(wait_s)


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


def capture(args, out_dir):
    app_name = APP_NAME
    with sync_playwright() as p:
        ctx = launch_browser(p, args.profile, args.browser)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)

        page = wait_for_login(ctx, page,
                              username=args.username, password=args.password,
                              mode=args.login)
        if page is None:
            log("Login did not complete. Exiting.")
            try:
                ctx.close()
            except Exception:
                pass
            sys.exit(1)
        page.set_default_timeout(60000)
        page.bring_to_front()

        log(f"Logged in. Current URL: {page.url}")
        if args.workspace:
            phase("checking_workspace", args.workspace)
            ensure_workspace(page, args.workspace)
        if not args.no_pause and interactive():
            input("Confirm the browser shows workspace CoreWeave > Production, then press Enter to start capture "
                  "(then leave the browser window alone - it must stay on screen)... ")
        elif not args.no_pause:
            log("No terminal to pause on - starting capture (the workspace check above still applies).")

        cap = Capturer(page, out_dir, args.settle, args.month, app_name, args.display, args.max_parts,
                       args.workspace)
        sections = select_sections(args.project)
        if sections is not SECTIONS:
            log(f"Scoped to {len(sections)} of {len(SECTIONS)} projects: "
                + ", ".join(s["tab"] for s in sections))
        phase("capturing", str(sum(len(s["folders"]) for s in sections)))
        for section in sections:
            log(f"=== Tab: {section['tab']} ===")
            for folder in section["folders"]:
                cap.run_folder(section["tab"], folder)

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(
            {"month": args.month, "workspace": args.workspace,
             # What was in scope, so a reader can tell "nothing changed" apart from
             # "this was never looked at".
             "projects": [s["tab"] for s in sections],
             "captures": cap.manifest, "warnings": cap.warnings}, indent=2))
        log(f"Manifest written: {manifest_path} ({len(cap.manifest)} screenshots)")
        if cap.warnings:
            print("\n==== WARNINGS - review these screenshots before sending ====")
            for w_ in cap.warnings:
                print("  " + w_)
            print()
        ctx.close()
        return manifest_path


# --------------------------------------------------------------------------- #
# workbook side                                                                #
# --------------------------------------------------------------------------- #
PX_PER_ROW = 20  # default 15pt row ~ 20px


def build_workbook(manifest_path, out_path, max_width=1400):
    data = json.loads(Path(manifest_path).read_text())
    month = data["month"]
    workspace = data.get("workspace", "")
    # Projects the run actually covered. Older manifests have no such key, and for
    # those every project was in scope.
    scope = data.get("projects") or [s["tab"] for s in SECTIONS]

    wb = Workbook()
    wb.remove(wb.active)
    sheets = {}
    for s in SECTIONS:
        if s["tab"] not in scope:
            continue          # out of scope: no sheet at all, rather than a blank one
        ws = wb.create_sheet(title=s["tab"][:31])
        ws.column_dimensions["A"].width = 200
        ws["A1"] = f"Workato CM Review - {month} - {s['tab']}"
        ws["A1"].font = Font(bold=True, size=14)
        if workspace:
            ws["A2"] = f"Workspace: {workspace}"
        sheets[s["tab"]] = {"ws": ws, "row": 3}

    wb_img_dir = Path(manifest_path).parent / "_workbook_images"
    for c in data["captures"]:
        ws_info = sheets[c["tab"]]
        ws, r = ws_info["ws"], ws_info["row"]
        ws.cell(row=r, column=1, value=c["description"]).font = Font(bold=True)
        ws.cell(row=r + 1, column=1, value=c["url"])
        ts = datetime.fromisoformat(c["captured"]).strftime("%Y-%m-%d %H:%M:%S %Z")
        ws.cell(row=r + 2, column=1, value=f"Captured: {ts}")
        img = XLImage(workbook_copy(c["file"], wb_img_dir))
        if img.width > max_width:
            scale = max_width / img.width
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)
        ws.add_image(img, f"A{r + 3}")
        ws_info["row"] = r + 3 + math.ceil(img.height / PX_PER_ROW) + 3

    empty = [t for t, i in sheets.items() if i["row"] == 3]
    for t in empty:
        sheets[t]["ws"]["A3"] = "No changes / no recipes captured this month."

    wb.save(out_path)
    log(f"Workbook written: {out_path}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default=datetime.now().strftime("%B %Y"), help='e.g. "September 2026"')
    ap.add_argument("--out", default=None, help="output folder (default ./sox_<Month>_<timestamp>)")
    ap.add_argument("--profile", default="~/.workato_sox_browser_profile", help="persistent browser profile dir")
    ap.add_argument("--browser", choices=["chromium", "chrome"], default="chromium")
    ap.add_argument("--settle", type=float, default=4.0, help="seconds to wait after each page load")
    ap.add_argument("--workspace", default="Production",
                    help="workspace name that must be visible on every page ('' to disable the check)")
    ap.add_argument("--display", type=int, default=1, help="display number for screencapture (1 = main)")
    ap.add_argument("--max-parts", type=int, default=4, help="max scrolled screenshots per long page")
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--build-only", action="store_true", help="rebuild workbook from --out/manifest.json")
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--env", default="workato",
                    help="section in config/environments.yaml to sign in with")
    ap.add_argument("--project", default="all",
                    help='projects to capture, comma-separated, or "all" '
                         '(choices: ' + " | ".join(project_names()) + ")")
    ap.add_argument("--list-projects", action="store_true",
                    help="print the projects this script knows about and exit")
    ap.add_argument("--username", default="", help="overrides the configured username")
    # Deliberately no --password: a password on the command line is visible to
    # every process on the box via `ps`. It comes from the config file, which may
    # itself read it from the environment as ${VAR}.
    args = ap.parse_args()

    if args.list_projects:
        for p in project_names():
            print(p)
        return

    # Validate the scope before signing in to anything, so a typo fails in a
    # second rather than after a browser and an SSO round-trip.
    select_sections(args.project)

    # Credentials, if any are configured. No config file at all is fine: the login
    # is then interactive, which is how this script worked before.
    args.password = ""
    args.login = "interactive"
    try:
        import environments

        cfg = environments.find(args.env)
        if cfg and cfg.get("enabled"):
            args.username = args.username or cfg.get("username", "")
            args.password = cfg.get("password", "")
            args.login = cfg.get("login", "interactive")
            if args.workspace == ap.get_default("workspace") and cfg.get("workspace"):
                args.workspace = cfg["workspace"]
            log(f"Environment '{cfg['name']}': login={args.login}"
                f"{', user=' + args.username if args.username else ''}"
                f"{', password configured' if args.password else ', no password configured'}")
        elif cfg:
            log(f"Environment '{cfg['name']}' is present but not enabled - signing in by hand.")
        else:
            log(f"No environment '{args.env}' configured - signing in by hand.")
    except ImportError:
        pass
    except Exception as exc:                        # config errors must not be silent
        log(f"!! environments.yaml could not be read ({exc}); signing in by hand.")

    out_dir = Path(args.out) if args.out else Path(f"sox_{slug(args.month)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"Workato CM Review - {args.month}.xlsx"

    if args.build_only:
        manifest = out_dir / "manifest.json"
        if not manifest.exists():
            sys.exit(f"No manifest at {manifest}; pass --out <folder from a previous run>")
        build_workbook(manifest, xlsx_path)
        return

    phase("starting", args.month)
    manifest = capture(args, out_dir)
    if not args.capture_only:
        phase("building_workbook", str(xlsx_path))
        build_workbook(manifest, xlsx_path)
    phase("done", str(xlsx_path))
    log("Done. Next: upload the .xlsx to Google Drive, open with Google Sheets (File > Save as Google Sheets).")


if __name__ == "__main__":
    main()
