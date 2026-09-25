#!/usr/bin/env python3
"""
NetSuite User Access Review (SOX UA-04) - user + role listing with screenshot evidence.

READ-ONLY. Opens the Manage Users / Employees lists, reads who holds access and
with which roles, and screenshots what it read. It never creates, edits, disables
or deletes a user or a role.

WHAT "ACTIVE" MEANS HERE
-----------------------
NetSuite does not show one access flag. A person can be an inactive employee, an
active employee with login access switched off, or active with several roles. The
column that decides access is "Login Access" where the list exposes it, and the
inactive flag otherwise. Both are recorded exactly as displayed, and the verdict
is derived separately. A row this tool cannot interpret is marked `unknown` and
raised as a warning - never assumed inactive, because an unreviewed live login is
the failure that matters.

ROLES
-----
Manage Users lists a row per user-role pairing, so one person can appear several
times. The output keeps every pairing (a role is what actually grants access) and
also reports the distinct people count, which is what a reviewer signs off against.

Usage
-----
  python3 -m netsuite.uar_capture --period "Q3 FY26"
  python3 -m netsuite.uar_capture --account 7258820_SB1 --out ./ns_uar
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from playwright.sync_api import sync_playwright

from core.platform import (
    PX_PER_ROW,
    launch_browser,
    log,
    now_stamp,
    phase,
    slug,
    workbook_copy,
)
from core import environments
from netsuite import common as ns

# Where users live. Manage Users is the access list; the employee list is the
# fallback, and carries the inactive flag rather than login access.
USER_PATHS = (
    ("/app/setup/uiaccesslist.nl?whence=", "Manage Users"),
    ("/app/common/entity/employeelist.nl?whence=", "Employees"),
    ("/app/setup/rolelist.nl?whence=", "Roles"),
)

# Raw value -> does this row grant access?
NO_ACCESS = ("no", "false", "f", "disabled", "inactive", "locked", "revoked")
HAS_ACCESS = ("yes", "true", "t", "enabled", "active")


def classify(login_access, inactive):
    """(active, bucket) from the two columns NetSuite actually exposes."""
    la = (login_access or "").strip().lower()
    inact = (inactive or "").strip().lower()

    if inact in HAS_ACCESS:          # the "Inactive" column ticked = no access
        return False, "inactive"
    if la in NO_ACCESS:
        return False, "no-login"
    if la in HAS_ACCESS:
        return True, "active"
    if inact in NO_ACCESS:           # explicitly not inactive, no login column
        return True, "active"
    return None, "unknown"


def roster_verdict(data, label):
    """Is this really a user list? Structure, not just "a page loaded"."""
    if not data or not data.get("rows"):
        return False, "no table rows"
    headers = [h.lower() for h in data.get("headers", [])]
    wanted = ("name", "email", "role", "login", "id", "inactive")
    hits = [h for h in headers if any(w in h for w in wanted)]
    if hits:
        return True, f"columns {hits[:4]}"
    if data.get("emails", 0) >= 2:
        return True, f"{data['emails']} rows carry an email address"
    return False, (f"{len(data['rows'])} rows but no user columns and "
                   f"{data.get('emails', 0)} emails - not a user list")


class Uar:
    def __init__(self, cap):
        self.cap = cap
        self.page = cap.page

    def collect(self):
        """Open each list, screenshot it, and read the rows. Returns (users, sources)."""
        users = {}
        sources = []
        base = ns.account_base(self.cap.account_id)

        for path, label in USER_PATHS:
            url = base + path
            log(f"  opening {label}: {url}")
            if not self.cap.goto(url):
                self.cap.warn(f"could not open the {label} list at {url} - the role in use "
                              f"may not have permission to see it")
                continue

            data = ns.read_list(self.page)
            ok, why = roster_verdict(data, label)
            self.cap.shoot_scrolled("users", f"{label} - {self.cap.account_id}", url)
            if not ok:
                self.cap.warn(f"{label}: {why} - screenshot kept, but no rows were read "
                              f"from it")
                continue

            log(f"    accepted: {why} ({len(data['rows'])} rows)")
            sources.append({"label": label, "url": url,
                            "rows": len(data["rows"]), "headers": data["headers"]})
            self._merge(users, data, label)

        return list(users.values()), sources

    def _merge(self, users, data, label):
        h = data.get("headers", [])
        i_name = ns.header_index(h, "name", "user", "employee")
        i_mail = ns.header_index(h, "email")
        i_role = ns.header_index(h, "role")
        i_login = ns.header_index(h, "login access", "login")
        i_inact = ns.header_index(h, "inactive")

        for row in data["rows"]:
            name = ns.cell(row, i_name)
            email = ns.cell(row, i_mail)
            if not email:
                found = ns.EMAIL_RE.search(" ".join(row))
                email = found.group(0) if found else ""
            if not name and not email:
                continue

            role = ns.cell(row, i_role)
            login = ns.cell(row, i_login)
            inact = ns.cell(row, i_inact)
            active, bucket = classify(login, inact)

            key = f"{(email or name).lower()}|{role.lower()}"
            if key in users:
                continue
            if bucket == "unknown":
                self.cap.warn(
                    f"{email or name}{' / ' + role if role else ''}: access state could not "
                    f"be read from {label} (login={login!r} inactive={inact!r}) - classify "
                    f"by hand; counted as neither active nor inactive")
            users[key] = {
                "name": name or (email.split("@")[0] if email else ""),
                "email": email,
                "role": role,
                "login_access_raw": login,
                "inactive_raw": inact,
                "status": bucket,
                "active": active,
                "source": label,
                "source_row": " | ".join(row)[:500],
            }


# --------------------------------------------------------------------------- #
# outputs                                                                      #
# --------------------------------------------------------------------------- #
COLS = ["name", "email", "role", "status", "active",
        "login_access_raw", "inactive_raw", "source"]

HEAD_FILL = PatternFill("solid", fgColor="1D2433")
OK_FILL = PatternFill("solid", fgColor="EFF9F3")
OFF_FILL = PatternFill("solid", fgColor="FDF3E7")


def write_outputs(out_dir, users, meta):
    (out_dir / "users.json").write_text(json.dumps({**meta, "users": users}, indent=2))
    with (out_dir / "users.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for u in users:
            w.writerow(u)
    log(f"  wrote users.csv and users.json ({len(users)} rows)")


def _sheet(wb, title, rows, meta, note):
    ws = wb.create_sheet(title=title[:31])
    ws["A1"] = f"NetSuite User Access Review - {meta['period']} - {title}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Account: {meta['account']}   ·   Listed: {meta['captured']}   ·   "
                f"{len(rows)} row(s)")
    ws["A3"] = note
    ws["A3"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[3].height = 28
    for i, c in enumerate(["Name", "Email", "Role", "Login access", "Inactive",
                           "Verdict", "Source"], start=1):
        cell = ws.cell(row=5, column=i, value=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
    for w, col in zip((26, 32, 30, 14, 10, 13, 16), "ABCDEFG"):
        ws.column_dimensions[col].width = w
    for r, u in enumerate(rows, start=6):
        for i, v in enumerate([u["name"], u["email"], u["role"], u["login_access_raw"],
                               u["inactive_raw"], u["status"], u["source"]], start=1):
            ws.cell(row=r, column=i, value=v)
        fill = OK_FILL if u["active"] else OFF_FILL
        for i in range(1, 8):
            ws.cell(row=r, column=i).fill = fill
    if not rows:
        ws["A6"] = "None."
    return ws


def build_workbook(manifest_path, out_path, max_width=1400):
    data = json.loads(Path(manifest_path).read_text())
    users = data["users"]
    meta = {k: data.get(k, "") for k in ("period", "account", "captured")}

    active = [u for u in users if u["active"] is True]
    inactive = [u for u in users if u["active"] is False]
    unknown = [u for u in users if u["active"] is None]
    people = {(u["email"] or u["name"]).lower() for u in users if (u["email"] or u["name"])}
    people_active = {(u["email"] or u["name"]).lower() for u in active}

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(title="Summary")
    ws["A1"] = f"NetSuite User Access Review - {meta['period']}"
    ws["A1"].font = Font(bold=True, size=16)
    rows = [
        ("Account", meta["account"]),
        ("Listing taken", meta["captured"]),
        ("", ""),
        ("Distinct people", len(people)),
        ("Distinct people with access", len(people_active)),
        ("", ""),
        ("User-role rows", len(users)),
        ("Rows granting access", len(active)),
        ("Rows without access", len(inactive)),
        ("Rows not interpretable", len(unknown)),
    ]
    for i, (k, v) in enumerate(rows, start=3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=bool(k))
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 58

    n = len(rows) + 4
    ws.cell(row=n, column=1, value=(
        "NetSuite grants access per user-ROLE pairing, so one person can appear on "
        "several rows. Rows are the unit of access and the unit of review; the people "
        "counts above are for reconciliation against HR. This listing is a "
        f"point-in-time snapshot taken on the date above and labelled {meta['period']}."))
    ws.cell(row=n, column=1).alignment = Alignment(wrap_text=True)
    ws.row_dimensions[n].height = 58

    if unknown:
        m = n + 2
        ws.cell(row=m, column=1, value=(
            f"{len(unknown)} row(s) could not be interpreted and are counted as neither "
            "active nor inactive. They are on the Unrecognised tab. Classify them by "
            "hand - do not assume no access.")).font = Font(bold=True, color="98600C")
        ws.cell(row=m, column=1).alignment = Alignment(wrap_text=True)
        ws.row_dimensions[m].height = 44

    for src in data.get("sources", []):
        pass  # listed on the Evidence tab below

    _sheet(wb, "With access", active, meta,
           "Rows that grant NetSuite access. Confirm each role remains appropriate.")
    _sheet(wb, "Without access", inactive, meta,
           "Inactive users, or users whose login access is switched off. Confirm access "
           "really is removed.")
    if unknown:
        _sheet(wb, "Unrecognised", unknown, meta,
               "Access state could not be read. Classify by hand - do not assume no access.")

    ev = wb.create_sheet(title="Evidence")
    ev.column_dimensions["A"].width = 200
    ev["A1"] = f"Screenshots - {meta['period']}"
    ev["A1"].font = Font(bold=True, size=14)
    r = 3
    for s in data.get("sources", []):
        ev.cell(row=r, column=1, value=f"{s['label']}: {s['rows']} rows - {s['url']}")
        r += 1
    r += 1
    img_dir = Path(manifest_path).parent / "_workbook_images"
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

    wb.save(out_path)
    log(f"Workbook written: {out_path}")


# --------------------------------------------------------------------------- #
def run(args, out_dir):
    with sync_playwright() as p:
        ctx = launch_browser(p, args.profile, args.browser)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)

        page = ns.sign_in(ctx, page, args.account, args.username, args.password,
                          args.login, args.session_url, role=args.role)
        if page is None:
            log("Sign-in did not complete. Exiting.")
            try:
                ctx.close()
            except Exception:
                pass
            sys.exit(1)
        page.set_default_timeout(60000)
        page.bring_to_front()
        log(f"Signed in. Current URL: {page.url}")

        phase("capturing", "netsuite users")
        cap = ns.NsCapture(page, out_dir, args.settle, args.account, args.display,
                           args.max_parts)
        users, sources = Uar(cap).collect()

        if not sources:
            manifest = out_dir / "manifest.json"
            manifest.write_text(json.dumps({
                "period": args.period, "account": args.account,
                "captured": now_stamp().isoformat(), "users": [], "sources": [],
                "captures": cap.manifest,
                "warnings": cap.warnings + [
                    "No user list could be read. No listing was produced - this is NOT "
                    "evidence that the account has no users."],
            }, indent=2))
            ctx.close()
            if cap.auth_lost:
                raise SystemExit(
                    "The NetSuite session was not valid: every list redirected back to a "
                    "sign-in page.\nThis is NOT a permissions problem - the sign-in never "
                    "completed, usually because a two-factor code is still outstanding.\n"
                    "Open the browser session, finish signing in, and re-run.")
            raise SystemExit(
                "No NetSuite user list could be read. Tried:\n  "
                + "\n  ".join(f"{p} ({l})" for p, l in USER_PATHS)
                + "\nThe session was valid, so the signed-in role most likely lacks "
                  "permission for Setup > Users/Roles. Switch to an administrator role "
                  "and re-run.")

        if not users:
            cap.warn("lists opened but no user rows could be read - check the screenshots; "
                     "do not read this as an empty user list")

        meta = {"period": args.period, "account": args.account,
                "captured": now_stamp().isoformat()}
        write_outputs(out_dir, users, meta)

        manifest = out_dir / "manifest.json"
        manifest.write_text(json.dumps({
            **meta, "users": users, "sources": sources,
            "captures": cap.manifest, "warnings": cap.warnings}, indent=2))
        log(f"Manifest written: {manifest} ({len(cap.manifest)} screenshots)")

        a = sum(1 for u in users if u["active"] is True)
        i = sum(1 for u in users if u["active"] is False)
        k = sum(1 for u in users if u["active"] is None)
        log(f"Rows: {len(users)} · {a} with access · {i} without · {k} unrecognised")
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
    ap.add_argument("--period", default="")
    ap.add_argument("--account", default="", help="e.g. 7258820_SB1")
    ap.add_argument("--out", default=None)
    ap.add_argument("--profile", default="~/.netsuite_sox_browser_profile")
    ap.add_argument("--browser", choices=["chromium", "chrome"], default="chromium")
    ap.add_argument("--settle", type=float, default=4.0)
    ap.add_argument("--display", type=int, default=1)
    ap.add_argument("--max-parts", type=int, default=12)
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--env", default="netsuite")
    ap.add_argument("--username", default="")
    ap.add_argument("--role", default="",
                    help="role to select on the NetSuite role picker, e.g. Administrator. "
                         "Only chosen when it matches BOTH the role name and the account.")
    # Deliberately no --password: a password on the command line is visible to
    # every process on the box via `ps`.
    args = ap.parse_args()

    if not args.period:
        args.period = f"Q{(datetime.now().month - 1) // 3 + 1} {datetime.now().year}"

    args.password = ""
    args.login = "interactive"
    args.role = args.role or ""
    args.session_url = __import__("os").environ.get("CAPTURE_SESSION_URL", "")
    try:
        cfg = environments.find(args.env)
        if cfg and cfg.get("enabled"):
            args.username = args.username or cfg.get("username", "")
            args.password = cfg.get("password", "")
            args.login = cfg.get("login", "interactive")
            args.account = args.account or cfg.get("account_id", "")
            args.role = args.role or cfg.get("role", "Administrator")
            log(f"Environment '{cfg['name']}': login={args.login}"
                f"{', user=' + args.username if args.username else ''}"
                f"{', password configured' if args.password else ', no password configured'}")
    except Exception as exc:
        log(f"!! environments.yaml could not be read ({exc}); signing in by hand.")

    if not args.account:
        sys.exit("No NetSuite account id. Set account_id in config/environments.yaml "
                 "or pass --account 7258820_SB1.")

    out_dir = Path(args.out) if args.out else Path(
        f"ns_uar_{slug(args.period)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx = out_dir / f"NetSuite User Access Review - {args.period}.xlsx"

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
