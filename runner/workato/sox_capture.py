#!/usr/bin/env python3
"""
Workato CM Review (SOX) - automated screenshot capture + workbook builder.

READ-ONLY. This script only navigates Workato pages and takes screenshots.
It never starts, stops, edits, creates, or deletes anything.

Screenshots are WHOLE-SCREEN macOS captures (same as Cmd+Shift+3), so they show the
browser URL bar and the Mac menu-bar clock. Keep the browser window on screen while
it runs. macOS will ask once to allow Terminal "Screen Recording" permission.

Usage
-----
  # first run (opens a browser window; log in via SSO once when prompted)
  python3 workato_sox_capture.py

  # options
  python3 workato_sox_capture.py --month "September 2026" --config-sheet "https://docs.google.com/spreadsheets/d/.../edit"
  python3 workato_sox_capture.py --month "September 2026"   # reuses the sheet URL saved in sox_config.json
  python3 workato_sox_capture.py --config-csv recipes.csv   # local CSV instead of a Google Sheet
  python3 workato_sox_capture.py --capture-only             # screenshots + manifest only
  python3 workato_sox_capture.py --build-only --out <dir>   # rebuild workbook from a previous run
  python3 workato_sox_capture.py --browser chromium         # Playwright's test build instead of Google Chrome
  python3 workato_sox_capture.py --no-pause                 # skip the confirmation prompt

Config sheet columns (one row per recipe):
  Tab | Folder | Folder ID | Recipe ID | Recipe Name | Capture (Y/N) | Notes
Only rows with Capture=Y are screenshotted. Recipes found in a folder but absent from the
sheet are reported as warnings (never captured silently).

Setup (once)
------------
  pip3 install playwright openpyxl pillow
  python3 -m playwright install chromium
"""

import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image
from playwright.sync_api import sync_playwright

# The screen layer is shared with every other capture in this repo, and it is
# already macOS + Linux + Windows. Duplicating it here is how the two drift.
from core.platform import (  # noqa: F401 - several are re-exported for this package
    IS_LINUX, IS_MAC, IS_WINDOWS, WINDOW_OFFSET_Y,
    clear_profile_lock, enter_pressed, grab_screen, interactive, log, now_stamp,
    slug, workbook_copy,
)
from core.platform import activate_app as _core_activate
from core.platform import screen_size as _core_screen_size

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
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
GID_RE = re.compile(r"[#&?]gid=(\d+)")

def _config_file():
    """Where the remembered --config-sheet URL is kept.

    On the data volume when there is one. It used to sit next to this script,
    inside the image, so a rebuild silently forgot the sheet and the next run
    died asking for a flag the operator had already supplied once.
    """
    data = Path(os.environ.get("CAPTURE_DATA_DIR", "/data"))
    if data.is_dir():
        return data / "sox_config.json"
    return Path(__file__).with_name("sox_config.json")


CONFIG_FILE = _config_file()


# --------------------------------------------------------------------------- #
# config sheet: one row per recipe                                             #
#   Tab | Folder | Folder ID | Recipe ID | Recipe Name | Capture (Y/N) | Notes #
# --------------------------------------------------------------------------- #
def _truthy(v):
    return str(v or "").strip().upper() in ("Y", "YES", "TRUE", "1", "X")


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def match_recipe_by_name(wanted, candidates):
    """candidates: {rid: name}. Exact match after stripping punctuation/spaces wins;
    otherwise the candidate whose tokens contain all of wanted's tokens with the fewest
    extras. Returns (rid, reason) or (None, reason)."""
    wn = _norm_name(wanted)
    exact = [rid for rid, n in candidates.items() if _norm_name(n) == wn]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, f"ambiguous exact match: {exact}"
    wt = _tokens(wanted)
    scored = sorted(
        ((len(_tokens(n) - wt), rid) for rid, n in candidates.items() if wt and wt <= _tokens(n)),
    )
    if not scored:
        return None, "no candidate contains all the words of the configured name"
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, f"ambiguous: {[rid for _, rid in scored if _ == scored[0][0]]}"
    return scored[0][1], "token match"


def parse_config_csv(text):
    """Turn the sheet rows into the SECTIONS structure.
      - Capture=Y rows with a Recipe ID  -> known   (captured)
      - Capture=N rows with a Recipe ID  -> skip    (not captured, not flagged as new)
      - rows with a name but no ID       -> by_name (resolved against the folder page)"""
    rdr = csv.DictReader(io.StringIO(text))
    norm = lambda k: re.sub(r"[^a-z]", "", (k or "").lower())
    sections, by_tab = [], {}
    for raw in rdr:
        r = {norm(k): (v or "").strip() for k, v in raw.items()}
        tab = r.get("tab", "")
        if not tab:
            continue
        sec = by_tab.get(tab)
        if not sec:
            sec = {"tab": tab, "folders": [], "_byfid": {}}
            by_tab[tab] = sec
            sections.append(sec)
        folder, fid, rid = r.get("folder", ""), r.get("folderid", ""), r.get("recipeid", "")
        name, group = r.get("recipename", ""), r.get("group", "")
        capture = _truthy(r.get("capture", "Y"))
        if not fid and not rid and not name:
            continue  # tab-only row (empty tab)
        key = fid or folder
        f = sec["_byfid"].get(key)
        if not f:
            f = {"name": folder or f"folder {fid}", "fid": fid or None,
                 "known": {}, "skip": {}, "by_name": [], "groups": {}}
            sec["_byfid"][key] = f
            sec["folders"].append(f)
        if rid:
            (f["known"] if capture else f["skip"])[rid] = name
            if group:
                f["groups"][rid] = group
        elif name:
            f["by_name"].append({"name": name, "group": group, "capture": capture})
    for sec in sections:
        sec.pop("_byfid", None)
        for f in sec["folders"]:
            f["expected"] = len(f["known"]) + len(f["skip"]) + len(f["by_name"])
            # a row with a Folder ID but no recipe => screenshot the folder's assets page only
            f["assets_only"] = f["expected"] == 0
    if not sections:
        raise ValueError("config sheet parsed but contained no rows with a Tab")
    return sections


def sheet_ids(url):
    m = SHEET_ID_RE.search(url)
    if not m:
        raise ValueError(f"not a Google Sheets URL: {url}")
    g = GID_RE.search(url)
    return m.group(1), (g.group(1) if g else "0")


def sheet_csv_url(url):
    """gviz endpoint returns the tab as CSV inline (no download), same-origin with the sheet page."""
    sid, gid = sheet_ids(url)
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={gid}"


def _csv_looks_valid(body):
    first = body.splitlines()[0] if body and body.strip() else ""
    return bool(first) and "," in first and "<html" not in body[:300].lower() and "tab" in first.lower()


def load_config_from_sheet(page, url):
    """Read the sheet as CSV from inside the automated browser: open the sheet in a tab
    (this is also where Google asks you to sign in, if needed) and fetch the CSV export
    from that page. Uses the browser's own session and certificate trust - no downloads,
    no separate HTTP client, corporate TLS proxies are fine."""
    csv_url = sheet_csv_url(url)
    for attempt in range(1, 4):
        body, err = "", ""
        tab = None
        try:
            tab = page.context.new_page()
            tab.goto(url, wait_until="domcontentloaded")
            time.sleep(2)
            if "accounts.google.com" in tab.url or "signin" in tab.url.lower():
                tab.bring_to_front()
                activate_browser()
                if not interactive():
                    raise SystemExit(
                        "The config sheet needs a Google sign-in, and there is no terminal "
                        "to wait on.\nOpen the browser session, sign in to Google so the "
                        "sheet is visible, and run again - the sheet URL is remembered.\n"
                        "Or avoid the sign-in entirely with --config-csv <file>.")
                print("\n>> Google sign-in needed to read the config sheet. Sign in in the browser window,")
                input("   wait until the sheet is visible, then press Enter here... ")
                tab.goto(url, wait_until="domcontentloaded")
                time.sleep(2)
            body = tab.evaluate(
                """async (u) => { const r = await fetch(u, {credentials: 'include'});
                                  if (!r.ok) throw new Error('HTTP ' + r.status);
                                  return await r.text(); }""",
                csv_url,
            )
        except Exception as e:
            err = str(e).splitlines()[0]
        finally:
            try:
                if tab:
                    tab.close()
            except Exception:
                pass
        if _csv_looks_valid(body):
            log(f"Config sheet loaded ({len(body.splitlines()) - 1} rows).")
            return parse_config_csv(body)
        if "closed" in err.lower():
            sys.exit("!! The browser was closed while reading the config sheet. Re-run and keep the browser window open.")
        log(f"  config sheet read attempt {attempt}/3 failed: {err or 'response was not CSV (no access, or wrong gid?)'}")
        if attempt < 3:
            input("   Check the browser window (is the sheet visible / are you signed in to Google?), "
                  "then press Enter to retry... ")
    sys.exit("Giving up on the config sheet. Check the URL/sharing, or use --config-csv <file>.")


def load_config(args, page):
    if args.config_csv:
        text = Path(args.config_csv).expanduser().read_text()
        log(f"Config loaded from local CSV: {args.config_csv}")
        return parse_config_csv(text)
    url = args.config_sheet
    if not url and CONFIG_FILE.exists():
        url = json.loads(CONFIG_FILE.read_text()).get("config_sheet")
        if url:
            log(f"Using saved config sheet: {url}")
    if url:
        CONFIG_FILE.write_text(json.dumps({"config_sheet": url}, indent=2))  # remember even if load fails
        return load_config_from_sheet(page, url)
    if args.use_builtin:
        log("!! Using the BUILT-IN recipe list (--use-builtin). This list may be out of date.")
        return SECTIONS
    sys.exit(
        "No recipe list configured.\n"
        "  --config-sheet <Google Sheet URL>   remembered for later runs; needs a Google\n"
        "                                      sign-in the first time, so run it once from\n"
        "                                      a terminal or the browser session\n"
        "  --config-csv <file>                 no sign-in; the reliable choice unattended\n"
        "  --use-builtin                       the list compiled into this script\n"
        "\nStarted from the web page, the runner supplies one of these automatically - set\n"
        "config_sheet or config_csv for the workato environment to choose which.")


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #




def assets_url(fid):
    return f"{BASE}/?fid={fid}&asset_type=recipe#assets"


def versions_url(rid):
    return f"{BASE}/recipes/{rid}?change_type=major#versions"


def screen_size():
    """Whole-screen size in points.

    macOS reads it from the Finder as before. Everywhere else this deferred to a
    1440x900 guess, which in the container silently sized the browser window to
    less than the 1680x1050 display it was being photographed on - the shots came
    out with a dead strip down the side and nobody would call that a failure.
    """
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
    return _core_screen_size()


BROWSER_PID = None  # set once the automated browser is running


def find_browser_pid(profile_dir):
    """PID of the browser we launched: the main (non-Helper) process whose command
    line carries our --user-data-dir. Lets us target *our* Chrome even when the
    user's regular Google Chrome is open at the same time."""
    if IS_WINDOWS:
        return None  # no ps(1); window activation goes by title instead
    marker = Path(profile_dir).name  # tolerant of path normalisation differences
    for _ in range(10):
        try:
            out = subprocess.check_output(["ps", "-axo", "pid=,args="], text=True, timeout=10)
        except Exception:
            return None
        pids = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            pid, args_ = parts
            if f"--user-data-dir=" in args_ and marker in args_ and "--type=" not in args_:
                pids.append(int(pid))  # child processes carry --type=renderer/gpu/...; the browser doesn't
        if pids:
            return min(pids)
        time.sleep(0.5)
    return None


def frontmost_pid():
    if not IS_MAC:
        return None
    try:
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "System Events" to get unix id of first process whose frontmost is true'],
            text=True, timeout=10)
        return int(out.strip())
    except Exception:
        return None


def browser_window_id(pid):
    """CGWindowID of the largest normal-layer window owned by pid (via JXA + CoreGraphics)."""
    if not IS_MAC:
        return None
    if not pid:
        return None
    jxa = f"""
ObjC.import('CoreGraphics');
var list = $.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll | $.kCGWindowListExcludeDesktopElements, $.kCGNullWindowID);
var arr = ObjC.deepUnwrap(list) || [];
var wins = arr.filter(function(w){{ return w.kCGWindowOwnerPID == {pid} && w.kCGWindowLayer == 0 && w.kCGWindowBounds && w.kCGWindowBounds.Width > 300; }});
wins.sort(function(a,b){{ return (b.kCGWindowBounds.Width*b.kCGWindowBounds.Height) - (a.kCGWindowBounds.Width*a.kCGWindowBounds.Height); }});
wins.length ? String(wins[0].kCGWindowNumber) : '';
"""
    try:
        out = subprocess.check_output(["osascript", "-l", "JavaScript", "-e", jxa], text=True, timeout=10).strip()
        return int(out) if out else None
    except Exception:
        return None


def activate_browser(app_name_fallback="Chrom"):
    """Bring the automated browser window to the front. Returns True only when the
    frontmost app afterwards really is our browser process."""
    if not IS_MAC:
        # There is no AppleScript and no CGWindow list off macOS. In the container
        # Xvfb has no window manager and the browser is the only mapped window, so
        # there is nothing to raise; on Windows the shared helper raises it by
        # title. Report True because the browser IS what will be photographed -
        # returning False here would make every shot look unverified.
        _core_activate(app_name_fallback)
        return True
    if BROWSER_PID:
        script = (
            'tell application "System Events"\n'
            f'  set ps to (every process whose unix id is {BROWSER_PID})\n'
            '  if (count of ps) > 0 then set frontmost of item 1 of ps to true\n'
            'end tell'
        )
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        except Exception:
            pass
        time.sleep(0.7)
        return frontmost_pid() == BROWSER_PID
    # unknown PID: name-based activation is unreliable when two Chromes run -> report failure
    script = (
        'tell application "System Events"\n'
        f'  set ps to (every process whose name contains "{app_name_fallback}")\n'
        '  if (count of ps) > 0 then set frontmost of item 1 of ps to true\n'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    except Exception:
        pass
    return False


def activate_app(app_name):  # backwards-compatible alias
    activate_browser(app_name)


def _activate_non_mac(app_name):
    """Raise the browser on Linux/Windows via the shared implementation."""
    _core_activate(app_name)


MENU_BAR_POINTS = 26  # macOS menu bar height in points (captured at the display's scale factor)

# How much to leave uncovered at the top of the screen, per platform. The point is
# the same everywhere - the shot must contain a clock - but what provides it is
# not: macOS has its menu bar, the container draws an xclock strip, and Windows
# keeps its taskbar. Only macOS needs the strip STITCHED on afterwards, because
# only there do we capture a single window rather than the whole screen.
TOP_STRIP = MENU_BAR_POINTS if IS_MAC else WINDOW_OFFSET_Y


def _sandbox_ok():
    """Whether Chromium's sandbox can be left enabled.

    Enabled is preferable: it keeps the "--no-sandbox is not supported" banner out
    of the screenshots. As root on Linux, Chromium will not start at all with it
    enabled, and a container runs as root.
    """
    if IS_LINUX:
        try:
            return os.geteuid() != 0
        except AttributeError:
            return True
    return True


def capture_menu_bar(screen_w):
    """Screenshot of the Mac menu bar (with the clock) as a PIL image, or None."""
    bar = Path("/tmp/sox_menubar.png")
    try:
        subprocess.run(["screencapture", "-x", "-R", f"0,0,{screen_w or 1440},{MENU_BAR_POINTS}", str(bar)],
                       check=True, timeout=30)
        return Image.open(bar).convert("RGB")
    except Exception as e:
        log(f"  (menu bar strip not captured: {e})")
        return None


def stitch_with_menu_bar(path, screen_w):
    """Put the live Mac menu bar (clock) above the image at `path`."""
    # Non-macOS captures are whole-screen, so the clock is already in them.
    # Stitching a second copy on would be misleading, not helpful.
    if not IS_MAC:
        return
    top = capture_menu_bar(screen_w)
    if top is None:
        return
    body = Image.open(path).convert("RGB")
    out_w = max(body.width, top.width)
    canvas = Image.new("RGB", (out_w, top.height + body.height), (0, 0, 0))
    canvas.paste(top, (0, 0))
    canvas.paste(body, ((out_w - body.width) // 2, top.height))
    canvas.save(path, optimize=True)


def looks_like_real_capture(path):
    """Reject empty/blank images (e.g. a hidden window captured as a transparent rectangle)."""
    try:
        img = Image.open(path).convert("L")
        if img.width < 400 or img.height < 300:
            return False
        small = img.resize((64, 48))
        px = list(small.getdata())
        mean = sum(px) / len(px)
        var = sum((p - mean) ** 2 for p in px) / len(px)
        return var > 25  # near-uniform images have ~0 variance
    except Exception:
        return False


_WINDOW_FALLBACK_SAID = False


def capture_window(path, window_id, screen_w):
    """Capture one window by CGWindowID and add the menu bar. True on success.

    Capturing a single window by id is a macOS facility. On Linux and Windows the
    whole screen is grabbed instead, which for evidence is equivalent or better -
    it still carries the URL bar and the clock, and it cannot silently photograph
    the wrong window. Said once per run rather than per screenshot.
    """
    if not IS_MAC:
        global _WINDOW_FALLBACK_SAID
        if not _WINDOW_FALLBACK_SAID:
            log("  window-scoped capture is macOS-only; using whole-screen shots "
                "(same URL bar, same clock)")
            _WINDOW_FALLBACK_SAID = True
        try:
            grab_screen(path, 1)
        except Exception:
            return False
        return looks_like_real_capture(path)
    try:
        subprocess.run(["screencapture", "-x", "-o", "-l", str(window_id), str(path)], check=True, timeout=30)
    except Exception:
        return False
    if not looks_like_real_capture(path):
        return False
    stitch_with_menu_bar(path, screen_w)
    return True


def capture_page(path, page, screen_w):
    """Page image straight from the browser engine (always the right content), plus the menu bar.
    Shows the page content but not the browser's URL bar - the URL is recorded in the caption."""
    page.screenshot(path=str(path), full_page=False)
    stitch_with_menu_bar(path, screen_w)


def mac_screencapture(path, display=1, window_id=None, screen_w=None):
    """Whole-screen capture (Cmd+Shift+3 style), on whichever platform this is.

    Kept under its original name because the call sites read well, but it is no
    longer macOS-only: screencapture there, the shared mss grab on Linux and
    Windows.
    """
    if IS_MAC:
        subprocess.run(["screencapture", "-x", "-D", str(display), str(path)],
                       check=True, timeout=30)
        return
    grab_screen(path, display)




# --------------------------------------------------------------------------- #
# browser side                                                                 #
# --------------------------------------------------------------------------- #
class Capturer:
    def __init__(self, page, out_dir, settle, month, app_name, display, max_parts, workspace,
                 capture_mode="window", screen_w=None, max_height=8000):
        self.page = page
        self.screen_w = screen_w
        self.max_height = max_height  # cap for the tall-viewport capture (CSS px)
        self.out_dir = out_dir
        self.settle = settle
        self.month = month
        self.app_name = app_name
        self.display = display
        self.max_parts = max_parts
        self.workspace = workspace
        self.capture_mode = capture_mode
        self.window_fallback_noted = False
        self.seq = 0
        self.manifest = []
        self.warnings = []

    def warn(self, msg):
        log("  !! " + msg)
        self.warnings.append(msg)

    def hidden_px(self):
        """How many CSS px of real content are below the visible area (0 if everything fits).
        Looks at both the scroll container's hidden height and where the last row/item ends."""
        hidden = 0
        try:
            info = self.page.evaluate(SCROLL_JS)
            if info:
                hidden = max(hidden, info["total"] - info["height"] - info["top"])
            m = self.page.evaluate(CONTENT_BOTTOM_JS)
            if m and m["bottom"] > m["viewport"]:
                hidden = max(hidden, m["bottom"] - m["viewport"])
        except Exception:
            pass
        return max(0, int(hidden))

    def tall_capture(self, path, hidden):
        """Lay the page out in a taller (emulated) viewport so the whole list is visible,
        take the page image from the browser engine, then restore. Menu bar (clock) on top.
        Returns True on success."""
        cdp = None
        try:
            dims = self.page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
            needed = min(self.max_height, dims["h"] + hidden + 80)
            cdp = self.page.context.new_cdp_session(self.page)
            cdp.send("Emulation.setDeviceMetricsOverride",
                     {"width": dims["w"], "height": int(needed), "deviceScaleFactor": 0, "mobile": False})
            time.sleep(1.2)
            h2 = self.hidden_px()  # containers sized to the viewport grow; check if more is needed
            if h2 > 40 and needed < self.max_height:
                needed = min(self.max_height, needed + h2 + 80)
                cdp.send("Emulation.setDeviceMetricsOverride",
                         {"width": dims["w"], "height": int(needed), "deviceScaleFactor": 0, "mobile": False})
                time.sleep(1.0)
                h2 = self.hidden_px()
            self.page.screenshot(path=str(path), full_page=False)
            cdp.send("Emulation.clearDeviceMetricsOverride")
            cdp.detach()
            cdp = None
            stitch_with_menu_bar(path, self.screen_w)
            log(f"  tall capture: viewport {dims['h']}px -> {int(needed)}px so the full list fits")
            if h2 > 40:
                self.warn(f"list still {h2}px longer than the {self.max_height}px cap (--max-height) - bottom may be cut")
            return True
        except Exception as e:
            log(f"  tall capture failed ({str(e).splitlines()[0]}); using a normal window shot")
            try:
                if cdp:
                    cdp.send("Emulation.clearDeviceMetricsOverride")
                    cdp.detach()
            except Exception:
                pass
            return False

    def take(self, path, window_id, in_front, desc):
        """Capture chain. 'window': the browser window by id, else the page from the browser
        engine. 'page': always the browser engine. 'screen': raw whole screen, but only when
        our browser is verifiably in front - otherwise the page image."""
        mode = self.capture_mode
        if mode == "screen":
            if in_front:
                mac_screencapture(path, self.display)
                return
            if not self.window_fallback_noted:
                self.warn("browser is not in front; using window/page capture instead of whole-screen.")
                self.window_fallback_noted = True
            mode = "window"
        if mode == "window" and window_id and capture_window(path, window_id, self.screen_w):
            return
        if mode == "window" and not self.window_fallback_noted:
            self.warn("browser window could not be captured by window id (hidden by Stage Manager / another "
                      "Space / minimized?). Using the page image from the browser engine + Mac menu bar instead.")
            self.window_fallback_noted = True
        capture_page(path, self.page, self.screen_w)

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
        """One screenshot per page. If the list fits on screen: capture the browser window
        (URL bar included). If it doesn't: lay the page out in a taller viewport so the whole
        list is in one image. Either way the Mac menu bar (clock) is stitched on top."""
        self.seq += 1
        if self.workspace and not in_workspace(self.page, self.workspace):
            self.warn(f"'{self.workspace}' not visible on page for: {desc}  ({url}) - CHECK THIS SCREENSHOT")
        self.page.bring_to_front()
        in_front = activate_browser(self.app_name)
        window_id = browser_window_id(BROWSER_PID) if self.capture_mode != "page" else None
        time.sleep(0.5)

        ts = now_stamp()
        fname = f"{self.seq:02d}_{slug(tab, 25)}_{kind}_{slug(ident, 12)}_{ts.strftime('%Y%m%d-%H%M%S')}.png"
        path = self.out_dir / fname

        hidden = self.hidden_px()
        done = False
        if hidden > 40:
            done = self.tall_capture(path, hidden)
            if not done:
                self.warn(f"list longer than the screen and tall capture failed for: {desc} - bottom may be cut off")
        if not done:
            self.take(path, window_id, in_front, desc)
        log(f"  captured -> {fname}")

        self.manifest.append(
            {
                "seq": self.seq,
                "part": 1,
                "parts": 1,
                "tab": tab,
                "kind": kind,
                "description": desc,
                "url": url,
                "captured": ts.isoformat(),
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

        skip = folder.get("skip", {})
        by_name = folder.get("by_name", [])
        groups = folder.get("groups", {})

        if not fid and known:
            rid0 = next(iter(known))
            fid = self.resolve_fid_from_recipe(rid0, name)
            log(f"  resolved fid={fid} from recipe {rid0}")
        if not fid:
            self.warn(f"folder '{name}' has no Folder ID and no known Recipe ID to resolve it from - "
                      f"fill in Folder ID in the config sheet. Skipped {len(by_name)} recipe(s): "
                      + ", ".join(e["name"] for e in by_name))
            return

        recipes = dict(known)  # ONLY the listed recipes are captured
        ok = self.goto(assets_url(fid), must_contain=f"fid={fid}")
        if not ok:
            self.warn(f"could not stay on folder {fid} ({name}); assets screenshot may be wrong")
        discovered = self.discover_recipes()

        if folder.get("assets_only"):
            log(f"  assets-only folder; page shows {len(discovered)} recipe link(s)"
                + (": " + ", ".join(f"{r} '{n}'" for r, n in discovered.items()) if discovered else ""))
            self.shoot(tab, "assets", fid, f"{name} - Assets (filter: Recipes)", assets_url(fid))
            return
        if len(discovered) < expected:
            log(f"  ! expected {expected} recipes, page shows {len(discovered)} - reloading once")
            self.page.reload(wait_until="domcontentloaded")
            time.sleep(self.settle + 2)
            discovered = self.discover_recipes() or discovered

        # resolve name-only rows against what the folder page shows
        for e in by_name:
            rid, why = match_recipe_by_name(e["name"], discovered)
            if rid:
                log(f"  resolved '{e['name']}' -> {rid} '{discovered[rid]}' ({why}); add this ID to the sheet")
                (recipes if e["capture"] else skip)[rid] = discovered[rid]
                if e["capture"]:
                    known[rid] = discovered[rid]
                if e["group"]:
                    groups[rid] = e["group"]
            else:
                self.warn(f"could not resolve '{e['name']}' in folder '{name}' ({why}) - "
                          f"fill in its Recipe ID in the config sheet")

        # compare page vs config: flag new / missing, but never change what we capture
        for rid, rname in discovered.items():
            if rid not in known and rid not in skip:
                self.warn(f"NEW recipe in '{name}' not in config sheet: {rid} '{rname}' - add it to the sheet (Capture Y/N)")
            elif rid in known and rname and not known[rid]:
                known[rid] = rname
        for rid in known:
            if discovered and rid not in discovered:
                self.warn(f"recipe {rid} '{known[rid]}' is in the config sheet but not visible in folder '{name}'")
        self.shoot(tab, "assets", fid, f"{name} - Assets (filter: Recipes)", assets_url(fid))

        for rid, rname in recipes.items():
            label = rname or f"recipe {rid}"
            if groups.get(rid):
                label = f"[{groups[rid]}] {label}"
            log(f"  Recipe {rid}: {label}")
            self.goto(versions_url(rid), must_contain=f"/recipes/{rid}")
            self.shoot(tab, "versions", rid, f"{label} (ID {rid}) - Versions (filter: Recipe change)", versions_url(rid))


LOGIN_PATH_MARKERS = ("/users/sign_in", "/users/password", "/login", "okta.com", "accounts.google.com", "/saml")
APP_UI_MARKERS = ("projects", "recipes", "connections", "workspace")


def looks_logged_in(page):
    u = page.url.lower()
    if not u.startswith(BASE) or any(m in u for m in LOGIN_PATH_MARKERS):
        return False
    txt = page_text(page).lower()
    if "sign in" in txt and "password" in txt:
        return False
    return sum(m in txt for m in APP_UI_MARKERS) >= 2




def live_pages(ctx):
    return [p for p in ctx.pages if not p.is_closed()]


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
    """Fill and submit the sign-in form. True once it was submitted.

    Cannot complete an SSO redirect or an MFA challenge - nothing can without a
    person - so the caller falls back to waiting for one.
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
        log(f"  form sign-in could not be attempted: {type(exc).__name__}")
        return False


def wait_for_login(ctx, page, timeout_s=600, username="", password="", mode="interactive"):
    """Returns the page that is logged in (SSO may finish in a different tab), or None.

    The credential arguments are part of the signature because the user access
    review calls this with them. Dropping them made every UA-04 run fail with
    "unexpected keyword argument" before it opened a page.
    """
    page.goto(BASE, wait_until="domcontentloaded")
    time.sleep(3)

    if mode == "form" and username and password and not looks_logged_in(page):
        log(f"Attempting form sign-in as {username}")
        if try_form_login(page, username, password):
            # Workato's redirect chain can outlast a single check.
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
        if time.time() - last_print > 15:
            log("Waiting for login in the browser window (it may be behind this Terminal).")
            for p in pages:
                log(f"   open tab: {p.url}")
            session = os.environ.get("CAPTURE_SESSION_URL", "")
            if session:
                log(f"   -> open {session} and sign in there.")
            else:
                log("   -> if you ARE logged in and see the Workato dashboard, press Enter here to continue.")
            last_print = time.time()

        # A sign-in completed through redirects does not always leave the page
        # object reflecting it; re-navigating settles that.
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


def ensure_workspace(page, name):
    """Block until the Workato UI shows the expected workspace name (e.g. 'Production').
    The script never switches workspaces itself - you do it in the browser window."""
    while True:
        page.goto(BASE, wait_until="domcontentloaded")
        time.sleep(3)
        if in_workspace(page, name):
            log(f"Workspace check OK: '{name}' detected in the page.")
            return
        print(f"\n!! Workspace '{name}' NOT detected on the current page.")
        print("   In the browser window, switch to CoreWeave > Production (workspace switcher, top-left),")
        input("   then press Enter here to re-check... ")


def capture(args, out_dir):
    profile_dir = Path(args.profile).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    sw, sh = screen_size()
    # A Chromium killed without shutting down leaves SingletonLock in the profile
    # and refuses to start next time, complaining that another process on another
    # computer holds it. That reads like a concurrency bug; it is debris from a
    # cancelled run or a container restart.
    clear_profile_lock(profile_dir)
    app_name = "Chrom"  # substring match: Chromium / Google Chrome / Google Chrome for Testing
    with sync_playwright() as p:
        kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            no_viewport=True,  # let the real window size drive the page
            # Keeping the sandbox on avoids Chromium's "--no-sandbox is not
            # supported" banner appearing in every screenshot, which matters when
            # the screenshot is the evidence. But Chromium refuses to start as
            # root with the sandbox enabled, and the container runs as root - so
            # there it is the difference between shots with a banner and no shots
            # at all.
            chromium_sandbox=_sandbox_ok(),
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--window-position=0,{TOP_STRIP}",
                f"--window-size={sw},{sh - TOP_STRIP}",
            ],
        )
        if args.browser == "chrome":
            kwargs["channel"] = "chrome"  # the installed Google Chrome, not "Chrome for Testing"
        ctx = p.chromium.launch_persistent_context(**kwargs)
        global BROWSER_PID
        BROWSER_PID = find_browser_pid(str(profile_dir))
        wid = browser_window_id(BROWSER_PID)
        log(f"Browser: {'Google Chrome' if args.browser == 'chrome' else 'Chromium'}"
            f" (pid {BROWSER_PID or 'UNKNOWN'}, window id {wid or 'UNKNOWN'})")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)

        page = wait_for_login(ctx, page,
                              username=getattr(args, "username", ""),
                              password=getattr(args, "password", ""),
                              mode=getattr(args, "login", "interactive"))
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
            ensure_workspace(page, args.workspace)

        sections = load_config(args, page)
        n_recipes = sum(len(f["known"]) + sum(e["capture"] for e in f.get("by_name", []))
                        for s in sections for f in s["folders"])
        n_skip = sum(len(f.get("skip", {})) for s in sections for f in s["folders"])
        n_byname = sum(len(f.get("by_name", [])) for s in sections for f in s["folders"])
        log(f"Plan: {len(sections)} tabs, {sum(len(s['folders']) for s in sections)} folders, "
            f"{n_recipes} recipes to capture ({n_skip} marked Capture=N, {n_byname} to resolve by name).")
        for s in sections:
            if not s["folders"]:
                log(f"   {s['tab']} | (empty tab)")
            for f in s["folders"]:
                fid_txt = f["fid"] or "MISSING"
                if f.get("assets_only"):
                    log(f"   {s['tab']} | {f['name']} (fid {fid_txt}): assets page only")
                    continue
                log(f"   {s['tab']} | {f['name']} (fid {fid_txt}): "
                    + ", ".join(f["known"])
                    + ("".join(f", name:'{e['name']}'" for e in f.get("by_name", [])))
                    + (f"  [skip: {', '.join(f['skip'])}]" if f.get("skip") else ""))

        if not args.no_pause:
            input("Confirm the plan above and that the browser shows CoreWeave > Production, then press Enter "
                  "to start capture (then leave the browser window alone - it must stay on screen)... ")

        if not BROWSER_PID:
            log("!! Could not identify the script's browser process; switching to whole-screen capture is unsafe "
                "with another Chrome open. Close your other Chrome windows, or re-run with --browser chromium.")
        cap = Capturer(page, out_dir, args.settle, args.month, app_name, args.display, args.max_parts,
                       args.workspace, args.capture_mode, sw, args.max_height)
        for section in sections:
            log(f"=== Tab: {section['tab']} ===")
            for folder in section["folders"]:
                cap.run_folder(section["tab"], folder)

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(
            {"month": args.month, "workspace": args.workspace, "tabs": [s["tab"] for s in sections],
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
    wb = Workbook()
    wb.remove(wb.active)
    sheets = {}
    tabs = data.get("tabs") or [s["tab"] for s in SECTIONS]
    for tab in tabs:
        ws = wb.create_sheet(title=re.sub(r"[\[\]\*\?/\\:]", "-", tab)[:31])
        ws.column_dimensions["A"].width = 200
        ws["A1"] = f"Workato CM Review - {month} - {tab}"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = "Reviewer comments (e.g. \"Reviewed - no recipe changes\"):"
        ws["A2"].font = Font(bold=True)
        ws["A3"].fill = PatternFill("solid", fgColor="FFF2CC")
        ws["A3"].alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[3].height = 60
        sheets[tab] = {"ws": ws, "row": 5}

    wb_img_dir = Path(manifest_path).parent / "_workbook_images"
    for c in data["captures"]:
        ws_info = sheets[c["tab"]]
        ws, r = ws_info["ws"], ws_info["row"]
        ws.cell(row=r, column=1, value=c["description"]).font = Font(bold=True)
        img = XLImage(workbook_copy(c["file"], wb_img_dir))
        if img.width > max_width:
            scale = max_width / img.width
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)
        ws.add_image(img, f"A{r + 1}")
        ws_info["row"] = r + 1 + math.ceil(img.height / PX_PER_ROW) + 2

    empty = [t for t, i in sheets.items() if i["row"] == 5]
    for t in empty:
        sheets[t]["ws"]["A5"] = "No recipes captured for this tab this month."

    wb.save(out_path)
    log(f"Workbook written: {out_path}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default=datetime.now().strftime("%B %Y"), help='e.g. "September 2026"')
    ap.add_argument("--out", default=None, help="output folder (default ./sox_<Month>_<timestamp>)")
    ap.add_argument("--profile", default="~/.workato_sox_browser_profile", help="persistent browser profile dir")
    ap.add_argument("--browser", choices=["chromium", "chrome"], default="chrome",
                    help="chrome = your installed Google Chrome (default); chromium = Playwright's test build")
    ap.add_argument("--config-sheet", default=None,
                    help="Google Sheet URL listing the recipes to capture (remembered in sox_config.json)")
    ap.add_argument("--config-csv", default=None, help="local CSV with the same columns (instead of a sheet)")
    ap.add_argument("--settle", type=float, default=4.0, help="seconds to wait after each page load")
    ap.add_argument("--workspace", default="Production",
                    help="workspace name that must be visible on every page ('' to disable the check)")
    ap.add_argument("--display", type=int, default=1, help="display number for screencapture (1 = main)")
    # "window" means capture-by-window-id, which only macOS can do. Defaulting to
    # it elsewhere would take the fallback path on every single shot; "screen"
    # gets there directly and is what the other platforms actually do.
    ap.add_argument("--capture-mode", choices=["window", "page", "screen"],
                    default="window" if IS_MAC else "screen",
                    help="window (default) = the script's browser window by window id + Mac menu bar (clock) "
                         "on top; falls back to 'page' if the window can't be captured. "
                         "page = page image from the browser engine + menu bar (always correct content, no URL bar). "
                         "screen = raw whole-screen shot, only when the browser is verifiably in front.")
    ap.add_argument("--use-builtin", action="store_true",
                    help="use the built-in recipe list instead of a config sheet/CSV (may be stale)")
    ap.add_argument("--max-parts", type=int, default=4, help=argparse.SUPPRESS)  # legacy, unused
    ap.add_argument("--max-height", type=int, default=8000,
                    help="cap (CSS px) for the taller viewport used when a list doesn't fit on screen (default 8000)")
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--build-only", action="store_true", help="rebuild workbook from --out/manifest.json")
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--env", default="workato",
                    help="section in config/environments.yaml to sign in with")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else Path(f"sox_{slug(args.month)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"Workato CM Review - {args.month}.xlsx"

    if args.build_only:
        manifest = out_dir / "manifest.json"
        if not manifest.exists():
            sys.exit(f"No manifest at {manifest}; pass --out <folder from a previous run>")
        build_workbook(manifest, xlsx_path)
        return

    # Credentials, if any are configured. No config at all is fine: the sign-in is
    # then interactive, which is how this script worked before.
    args.password = ""
    args.login = "interactive"
    try:
        from core import environments

        cfg = environments.find(args.env)
        if cfg and cfg.get("enabled"):
            args.username = cfg.get("username", "")
            args.password = cfg.get("password", "")
            args.login = cfg.get("login", "interactive")
            log(f"Environment '{cfg['name']}': login={args.login}"
                f"{', user=' + args.username if args.username else ''}")
        else:
            args.username = ""
    except Exception as exc:
        args.username = ""
        log(f"!! environments.yaml could not be read ({exc}); signing in by hand.")

    manifest = capture(args, out_dir)
    if not args.capture_only:
        build_workbook(manifest, xlsx_path)
    log("Done. Next: upload the .xlsx to Google Drive, open with Google Sheets (File > Save as Google Sheets).")


if __name__ == "__main__":
    main()
