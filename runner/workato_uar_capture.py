#!/usr/bin/env python3
"""
Workato User Access Review (SOX UA-04) - collaborator listing + screenshot evidence.

READ-ONLY. It opens the workspace's collaborator pages, reads who is listed, and
screenshots what it read. It never invites, removes, suspends or edits anyone.

WHAT IT PRODUCES
----------------
  users.csv / users.json   every collaborator: name, email, role, status, and
                           whether this review counts them as active
  screenshots + manifest   whole-screen captures of the pages the list came from,
                           so the listing can be tied back to what was on screen
  .xlsx workbook           Active and Inactive tabs, plus the evidence screenshots

ON "ACTIVE" AND "INACTIVE"
--------------------------
Workato does not label collaborators with a single active/inactive flag. It shows a
state per row - active, pending/invited, suspended, deactivated - and the wording
varies by plan and by page. This script records the RAW status exactly as the page
showed it, and separately derives an `active` verdict from it. Both go in the
output. When a status is one it does not recognise, the row is marked `unknown` and
raised as a warning rather than quietly counted as active: a user access review that
under-counts access is the failure that matters.

POINT IN TIME
-------------
The listing is as-of the moment it runs. Workato does not expose a historical
collaborator list, so a review "for Q3" is a current snapshot labelled with that
period, evidenced by the timestamp in each screenshot. That is how the period
argument is used, and the workbook says so on its face.

Usage
-----
  python3 workato_uar_capture.py --period "Q3 FY26"
  python3 workato_uar_capture.py --period "Q3 FY26" --out ./uar_q3
  python3 workato_uar_capture.py --capture-only      # no workbook
"""

import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from playwright.sync_api import sync_playwright

from workato_sox_capture import (
    APP_NAME, BASE, PX_PER_ROW, SCROLL_BY_JS, SCROLL_JS,
    activate_app, ensure_workspace, grab_screen, interactive, launch_browser,
    log, now_stamp, phase, slug, workbook_copy,
)
import environments

# Where collaborators live. Workato has moved this more than once and it differs by
# plan, so every candidate is tried and the first one that actually renders a list
# of people wins. The one that worked is recorded in the manifest.
MEMBER_URLS = (
    f"{BASE}/members/collaborators",
    f"{BASE}/collaborators",
    f"{BASE}/members/users",
    f"{BASE}/members/team",
    f"{BASE}/settings/collaborators",
    f"{BASE}/workspace/collaborators",
    f"{BASE}/settings/members",
    f"{BASE}/admin/members",
    f"{BASE}/teams",
    # Last: bare /members redirects to whichever admin sub-page Workato defaults to
    # (error alerts, in at least one workspace), which is not a roster at all.
    f"{BASE}/members",
)

# A page has to look like a roster, not merely contain an email address. The first
# version of this checked only "is there an email on the page", and happily accepted
# the Workspace admin error-alerts screen, reading a notification recipient chip as
# the entire collaborator list. Text on a real roster page.
ROSTER_WORDS = ("collaborator", "member", "role", "invite", "permission",
                "last active", "team", "access")
# Column headers a roster has and a settings form does not.
ROSTER_HEADERS = ("email", "role", "status", "name", "collaborator", "member",
                  "user", "last active", "permission")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Raw status -> does this review count the person as holding access?
# Anything not listed here is "unknown" and gets a warning; it is never assumed
# inactive, because assuming inactive is what hides live access from a reviewer.
STATUS_ACTIVE = ("active", "enabled", "accepted", "member", "admin", "analyst", "operator")
STATUS_INACTIVE = ("deactivated", "disabled", "suspended", "removed", "revoked", "inactive", "expired")
STATUS_PENDING = ("pending", "invited", "invitation sent", "awaiting")

# Pull every row that mentions an email address, from a table or from the card and
# list layouts Workato uses on narrower pages. Scrolling re-runs this, and rows are
# merged by email, so a virtualised list that only renders what is on screen still
# ends up complete.
EXTRACT_JS = r"""
() => {
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  const out = [];
  const seen = new Set();

  // An email inside a form control is a setting, not a person on a roster - the
  // notification-recipient chip on Workato's admin screen is exactly this, and
  // reading it as the collaborator list is how this went wrong before.
  const inFormControl = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const tag = n.tagName;
      if (tag === 'FORM' || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
      if (n.getAttribute && n.getAttribute('contenteditable') === 'true') return true;
      const role = n.getAttribute && n.getAttribute('role');
      if (role === 'combobox' || role === 'textbox' || role === 'listbox') return true;
    }
    return false;
  };

  const push = (cells, el, source) => {
    if (el && inFormControl(el)) return;
    const text = cells.filter(Boolean).join(' | ');
    const m = text.match(EMAIL);
    if (!m) return;
    const key = m[0].toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push({email: m[0], cells: cells, text: text, source: source});
  };

  let fromTable = 0;
  document.querySelectorAll('tr').forEach(tr => {
    const cells = Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText||'').trim());
    if (!cells.length) return;
    const before = out.length;
    push(cells, tr, 'table');
    if (out.length > before) fromTable++;
  });

  if (out.length === 0) {
    // No table: the smallest elements holding exactly one email, so a card or list
    // row is captured without dragging the whole page in with it.
    const candidates = Array.from(document.querySelectorAll('li,div,article,section'))
      .filter(el => {
        if (inFormControl(el)) return false;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 400) return false;
        return (t.match(new RegExp(EMAIL.source, 'g')) || []).length === 1;
      });
    candidates.sort((a, b) => (a.innerText||'').length - (b.innerText||'').length);
    const claimed = [];
    candidates.forEach(el => {
      if (claimed.some(c => c.contains(el) || el.contains(c))) return;
      claimed.push(el);
      const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      push(lines, el, 'card');
    });
  }

  const headers = Array.from(document.querySelectorAll('thead th, tr th'))
    .map(th => (th.innerText || '').trim()).filter(Boolean);

  const body = (document.body && document.body.innerText || '').toLowerCase();
  // Every email on the page, including ones in form controls, purely so the caller
  // can tell "a page with people on it" from "a settings page with one address".
  const allEmails = (document.body && document.body.innerText || '')
    .match(new RegExp(EMAIL.source, 'g')) || [];

  return {
    rows: out, headers: headers, url: location.href, title: document.title,
    fromTable: fromTable,
    emailCount: new Set(allEmails.map(e => e.toLowerCase())).size,
    bodyText: body.slice(0, 4000),
  };
}
"""


def classify(raw):
    """Map a raw status string onto the verdict this review uses.

    Returns (active, bucket). `active` is None when the status is not recognised -
    deliberately not False, so an unknown state can never be silently written off
    as "no access".
    """
    t = (raw or "").strip().lower()
    if not t:
        return None, "unknown"
    for s in STATUS_INACTIVE:
        if s in t:
            return False, "inactive"
    for s in STATUS_PENDING:
        if s in t:
            # Pending access is not yet access, but it is not nothing either - it is
            # reported in its own bucket so a reviewer decides rather than the tool.
            return False, "pending"
    for s in STATUS_ACTIVE:
        if s in t:
            return True, "active"
    return None, "unknown"


def status_from_row(cells, headers):
    """Best guess at which cell holds the status, and what it says."""
    lower_headers = [h.lower() for h in headers]
    for i, h in enumerate(lower_headers):
        if "status" in h or "state" in h:
            if i < len(cells) and cells[i].strip():
                return cells[i].strip()
    # No status column: take the first cell whose text is a status word.
    for c in cells:
        t = c.strip().lower()
        if any(s in t for s in STATUS_INACTIVE + STATUS_PENDING + ("active",)):
            return c.strip()
    return ""


def field_from_row(cells, headers, names):
    for i, h in enumerate([h.lower() for h in headers]):
        if any(n in h for n in names):
            if i < len(cells) and cells[i].strip():
                return cells[i].strip()
    return ""


def name_from_row(cells, headers, email):
    got = field_from_row(cells, headers, ("name", "user", "collaborator", "member"))
    if got and "@" not in got:
        return got
    for c in cells:
        t = c.strip()
        if t and "@" not in t and not EMAIL_RE.search(t) and len(t) < 80:
            if not any(s in t.lower() for s in STATUS_INACTIVE + STATUS_PENDING + ("active",)):
                return t
    return email.split("@")[0]


class UarCapture:
    def __init__(self, page, out_dir, settle, workspace, display, max_parts):
        self.page = page
        self.out_dir = out_dir
        self.settle = settle
        self.workspace = workspace
        self.display = display
        self.max_parts = max_parts
        self.seq = 0
        self.manifest = []
        self.warnings = []
        self.rejected = []

    def warn(self, msg):
        log(f"  !! {msg}")
        self.warnings.append(msg)

    def shoot(self, kind, desc, url, parts=1):
        """Whole-screen capture, scrolling for extra parts on long listings."""
        self.seq += 1
        self.page.bring_to_front()
        activate_app(APP_NAME)
        time.sleep(1.0)
        ts = now_stamp()
        fname = (f"{self.seq:02d}_uar_{kind}_{ts.strftime('%Y%m%d-%H%M%S')}"
                 f"{'' if parts == 1 else f'_p{parts}'}.png")
        path = self.out_dir / fname
        grab_screen(path, self.display)
        log(f"  captured -> {fname}")
        self.manifest.append({
            "seq": self.seq, "part": parts, "kind": kind, "description": desc,
            "url": url, "captured": ts.isoformat(), "file": str(path),
        })

    @staticmethod
    def roster_verdict(data, requested, landed):
        """Decide whether a page is really a collaborator roster.

        Returns (is_roster, reason). Containing an email address is not enough -
        Workato's admin error-alerts screen contains one, in a notification-recipient
        field, and accepting it produced a "user access review" listing one person
        who was not a collaborator at all. A roster has to show its structure.
        """
        # A redirect off the requested path means Workato sent us to its default
        # admin sub-page rather than the page asked for.
        req_path = requested.split("?")[0].rstrip("/")
        landed_path = landed.split("?")[0].split("#")[0].rstrip("/")
        redirected = not landed_path.startswith(req_path)

        rows = data.get("rows", [])
        if not rows:
            return False, "no rows with an email address"

        body = data.get("bodyText", "")
        words = [w for w in ROSTER_WORDS if w in body]
        headers = [h.lower() for h in data.get("headers", [])]
        header_hits = [h for h in headers if any(k in h for k in ROSTER_HEADERS)]

        if redirected and not header_hits:
            return False, (f"redirected to {landed_path} and it has no roster columns "
                           f"- this is not the page that was asked for")

        # A real table with roster-looking columns is conclusive, even for a
        # one-person workspace.
        if data.get("fromTable", 0) >= 1 and header_hits:
            return True, f"table with columns {header_hits}"

        # No table: several distinct people plus roster wording on the page.
        if len(rows) >= 3 and words:
            return True, f"{len(rows)} people listed, page mentions {words[:3]}"

        return False, (f"only {len(rows)} email-bearing row(s), no roster columns"
                       f"{' and no roster wording' if not words else ''}"
                       f" - looks like a settings page, not a roster")

    def find_members_page(self):
        """Open the first candidate URL that actually renders a collaborator roster."""
        rejected = []
        for url in MEMBER_URLS:
            log(f"  trying {url}")
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                log(f"    could not open ({type(exc).__name__})")
                rejected.append(f"{url}: could not open")
                continue
            try:
                self.page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(self.settle)

            landed = self.page.url
            if "/users/sign_in" in landed:
                self.warn("the session dropped while looking for the collaborator page")
                return None

            data = self.page.evaluate(EXTRACT_JS)
            ok, why = self.roster_verdict(data, url, landed)
            if ok:
                log(f"    accepted: {why}")
                if landed.rstrip("/") != url.rstrip("/"):
                    log(f"    (landed on {landed})")
                return landed
            log(f"    rejected: {why}")
            rejected.append(f"{url} -> {landed}: {why}")

        self.rejected = rejected
        return None

    def collect(self, url):
        """Read the listing, scrolling to pull in rows rendered only on demand."""
        by_email = {}
        headers = []
        parts = 0

        info = self.page.evaluate(SCROLL_JS)
        planned = 1
        if info:
            remaining = info["total"] - info["height"] - info["top"]
            if remaining > info["height"] * 0.25:
                planned = min(self.max_parts,
                              1 + math.ceil(remaining / max(1, info["height"] * 0.9)))

        for part in range(1, planned + 1):
            if part > 1:
                before = self.page.evaluate(
                    "() => (document.querySelector('[data-sox-scroll]') "
                    "|| document.scrollingElement).scrollTop")
                after = self.page.evaluate(SCROLL_BY_JS, int(info["height"] * 0.9))
                if after - before < 100:
                    break
                time.sleep(1.0)

            data = self.page.evaluate(EXTRACT_JS)
            if data["headers"] and not headers:
                headers = data["headers"]
            for row in data["rows"]:
                by_email.setdefault(row["email"].lower(), row)
            parts += 1
            self.shoot("collaborators", f"Workato collaborators - {url}", url, parts=part)

        return list(by_email.values()), headers, parts

    def to_users(self, rows, headers):
        users = []
        for row in rows:
            cells = row["cells"]
            email = row["email"]
            raw = status_from_row(cells, headers)
            active, bucket = classify(raw)
            if bucket == "unknown":
                self.warn(f"{email}: status {raw!r} not recognised - classify this by hand "
                          f"before signing off (counted as neither active nor inactive)")
            users.append({
                "name": name_from_row(cells, headers, email),
                "email": email,
                "role": field_from_row(cells, headers, ("role", "permission", "access")),
                "status_raw": raw,
                "status": bucket,
                "active": active,
                "last_activity": field_from_row(cells, headers,
                                                ("last", "activity", "seen", "login")),
                "source_row": row["text"][:500],
            })
        users.sort(key=lambda u: (u["status"] != "active", u["email"].lower()))
        return users


def write_outputs(out_dir, users, meta):
    (out_dir / "users.json").write_text(json.dumps(
        {**meta, "users": users}, indent=2))

    cols = ["name", "email", "role", "status", "status_raw", "active", "last_activity"]
    with (out_dir / "users.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for u in users:
            w.writerow(u)
    log(f"  wrote users.csv and users.json ({len(users)} users)")


HEAD_FILL = PatternFill("solid", fgColor="1D2433")
ACTIVE_FILL = PatternFill("solid", fgColor="EFF9F3")
INACTIVE_FILL = PatternFill("solid", fgColor="FDF3E7")


def _sheet(wb, title, users, meta, note):
    ws = wb.create_sheet(title=title[:31])
    ws["A1"] = f"Workato User Access Review - {meta['period']} - {title}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Workspace: {meta['workspace']}   ·   Listed: {meta['captured']}   ·   "
                f"{len(users)} user(s)")
    ws["A3"] = note
    ws["A3"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[3].height = 28

    cols = ["Name", "Email", "Role", "Status (as shown)", "Verdict", "Last activity"]
    for i, c in enumerate(cols, start=1):
        cell = ws.cell(row=5, column=i, value=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
    for w, col in zip((26, 34, 22, 22, 14, 18), "ABCDEF"):
        ws.column_dimensions[col].width = w

    for r, u in enumerate(users, start=6):
        ws.cell(row=r, column=1, value=u["name"])
        ws.cell(row=r, column=2, value=u["email"])
        ws.cell(row=r, column=3, value=u["role"])
        ws.cell(row=r, column=4, value=u["status_raw"])
        ws.cell(row=r, column=5, value=u["status"])
        ws.cell(row=r, column=6, value=u["last_activity"])
        fill = ACTIVE_FILL if u["active"] else INACTIVE_FILL
        for c in range(1, 7):
            ws.cell(row=r, column=c).fill = fill
    if not users:
        ws["A6"] = "None."
    return ws


def build_workbook(manifest_path, out_path, max_width=1400):
    data = json.loads(Path(manifest_path).read_text())
    users = data["users"]
    meta = {k: data.get(k, "") for k in ("period", "workspace", "captured", "source_url")}

    active = [u for u in users if u["active"] is True]
    inactive = [u for u in users if u["active"] is False]
    unknown = [u for u in users if u["active"] is None]

    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet(title="Summary")
    ws["A1"] = f"Workato User Access Review - {meta['period']}"
    ws["A1"].font = Font(bold=True, size=16)
    rows = [
        ("Workspace", meta["workspace"]),
        ("Listing taken", meta["captured"]),
        ("Source page", meta["source_url"]),
        ("", ""),
        ("Total collaborators", len(users)),
        ("Active", len(active)),
        ("Inactive / suspended", len([u for u in inactive if u["status"] == "inactive"])),
        ("Pending invitation", len([u for u in inactive if u["status"] == "pending"])),
        ("Status not recognised", len(unknown)),
    ]
    for i, (k, v) in enumerate(rows, start=3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=bool(k))
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 60

    n = len(rows) + 4
    ws.cell(row=n, column=1, value=(
        "This listing is a point-in-time snapshot taken on the date above and labelled "
        f"with {meta['period']}. Workato does not publish a historical collaborator list, "
        "so it evidences access as it stood when the capture ran - not on the last day of "
        "the period. Each row is evidenced by the screenshots in this workbook."))
    ws.cell(row=n, column=1).alignment = Alignment(wrap_text=True)
    ws.row_dimensions[n].height = 60

    if unknown:
        m = n + 2
        ws.cell(row=m, column=1, value=(
            f"{len(unknown)} user(s) had a status this tool does not recognise. They are "
            "counted as neither active nor inactive and are listed on the Unrecognised tab. "
            "Classify them by hand before signing off.")).font = Font(bold=True, color="98600C")
        ws.cell(row=m, column=1).alignment = Alignment(wrap_text=True)
        ws.row_dimensions[m].height = 44

    _sheet(wb, "Active", active, meta,
           "Users this review counts as holding access. Confirm each is still appropriate.")
    _sheet(wb, "Inactive", [u for u in inactive if u["status"] == "inactive"], meta,
           "Users shown as deactivated, suspended or disabled. Confirm access really is removed.")
    _sheet(wb, "Pending", [u for u in inactive if u["status"] == "pending"], meta,
           "Invitations not yet accepted. Access is not live, but the invitation is outstanding.")
    if unknown:
        _sheet(wb, "Unrecognised", unknown, meta,
               "Status could not be interpreted. Classify by hand - do not assume no access.")

    ev = wb.create_sheet(title="Evidence")
    ev.column_dimensions["A"].width = 200
    ev["A1"] = f"Screenshots - {meta['period']}"
    ev["A1"].font = Font(bold=True, size=14)
    img_dir = Path(manifest_path).parent / "_workbook_images"
    r = 3
    for c in data.get("captures", []):
        ev.cell(row=r, column=1, value=c["description"]).font = Font(bold=True)
        ev.cell(row=r + 1, column=1, value=c["url"])
        ts = datetime.fromisoformat(c["captured"]).strftime("%Y-%m-%d %H:%M:%S %Z")
        ev.cell(row=r + 2, column=1, value=f"Captured: {ts}")
        img = XLImage(workbook_copy(c["file"], img_dir))
        if img.width > max_width:
            scale = max_width / img.width
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)
        ev.add_image(img, f"A{r + 3}")
        r = r + 3 + math.ceil(img.height / PX_PER_ROW) + 3
    if r == 3:
        ev["A3"] = "No screenshots were captured."

    wb.save(out_path)
    log(f"Workbook written: {out_path}")


def run(args, out_dir):
    with sync_playwright() as p:
        ctx = launch_browser(p, args.profile, args.browser)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)

        # Same sign-in path as the change-management capture, including the
        # configured credentials and the fallback to a person for SSO/MFA.
        from workato_sox_capture import wait_for_login

        page = wait_for_login(ctx, page, username=args.username,
                              password=args.password, mode=args.login)
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

        phase("capturing", "collaborators")
        cap = UarCapture(page, out_dir, args.settle, args.workspace, args.display,
                         args.max_parts)

        url = cap.find_members_page()
        if not url:
            cap.shoot("not-found", "No collaborator page could be opened", page.url)
            manifest = out_dir / "manifest.json"
            manifest.write_text(json.dumps({
                "period": args.period, "workspace": args.workspace,
                "captured": now_stamp().isoformat(), "source_url": "",
                "users": [], "captures": cap.manifest,
                "rejected": cap.rejected,
                "warnings": cap.warnings + [
                    "No collaborator roster could be found at any known URL. No user "
                    "list was produced - this is NOT evidence that there are no users."],
            }, indent=2))
            ctx.close()
            raise SystemExit(
                "No collaborator roster could be found. Each candidate was opened and "
                "rejected:\n  " + "\n  ".join(cap.rejected or list(MEMBER_URLS))
                + "\n\nNo user list was produced. This is NOT evidence that the workspace "
                  "has no users.\nOpen the browser session, navigate to the collaborators "
                  "list by hand, and add that URL to MEMBER_URLS.")

        log(f"Collaborator page: {url}")
        rows, headers, parts = cap.collect(url)
        log(f"  {len(rows)} collaborator row(s) across {parts} screen(s)")
        users = cap.to_users(rows, headers)

        if not users:
            cap.warn("the page opened but no collaborator rows could be read - the listing "
                     "may be rendered in a way this tool does not understand. Check the "
                     "screenshots; do not read this as an empty user list.")

        meta = {
            "period": args.period,
            "workspace": args.workspace,
            "captured": now_stamp().isoformat(),
            "source_url": url,
        }
        write_outputs(out_dir, users, meta)

        manifest = out_dir / "manifest.json"
        manifest.write_text(json.dumps({
            **meta, "users": users, "captures": cap.manifest, "warnings": cap.warnings,
        }, indent=2))
        log(f"Manifest written: {manifest} ({len(cap.manifest)} screenshots)")

        active = sum(1 for u in users if u["active"] is True)
        inactive = sum(1 for u in users if u["active"] is False)
        unknown = sum(1 for u in users if u["active"] is None)
        log(f"Users: {len(users)} total · {active} active · {inactive} inactive/pending "
            f"· {unknown} unrecognised")

        if cap.warnings:
            print("\n==== WARNINGS - resolve these before signing off ====")
            for w in cap.warnings:
                print("  " + w)
            print()
        ctx.close()
        return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="", help='review period label, e.g. "Q3 FY26"')
    ap.add_argument("--out", default=None, help="output folder")
    ap.add_argument("--profile", default="~/.workato_sox_browser_profile")
    ap.add_argument("--browser", choices=["chromium", "chrome"], default="chromium")
    ap.add_argument("--settle", type=float, default=4.0)
    ap.add_argument("--workspace", default="Production")
    ap.add_argument("--display", type=int, default=1)
    ap.add_argument("--max-parts", type=int, default=12,
                    help="max scrolled screens of the user list")
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--env", default="workato")
    ap.add_argument("--username", default="")
    args = ap.parse_args()

    if not args.period:
        args.period = f"Q{(datetime.now().month - 1) // 3 + 1} {datetime.now().year}"

    args.password = ""
    args.login = "interactive"
    try:
        cfg = environments.find(args.env)
        if cfg and cfg.get("enabled"):
            args.username = args.username or cfg.get("username", "")
            args.password = cfg.get("password", "")
            args.login = cfg.get("login", "interactive")
            if args.workspace == ap.get_default("workspace") and cfg.get("workspace"):
                args.workspace = cfg["workspace"]
    except Exception as exc:
        log(f"!! environments.yaml could not be read ({exc}); signing in by hand.")

    out_dir = Path(args.out) if args.out else Path(
        f"uar_{slug(args.period)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx = out_dir / f"Workato User Access Review - {args.period}.xlsx"

    if args.build_only:
        m = out_dir / "manifest.json"
        if not m.exists():
            sys.exit(f"No manifest at {m}")
        build_workbook(m, xlsx)
        return

    phase("starting", args.period)
    manifest = run(args, out_dir)
    if not args.capture_only:
        phase("building_workbook", str(xlsx))
        build_workbook(manifest, xlsx)
    phase("done", str(xlsx))
    log("Done.")


if __name__ == "__main__":
    main()
