#!/usr/bin/env python3
"""
Shared NetSuite plumbing: sign-in, navigation, list extraction, evidence capture.

READ-ONLY. Everything here navigates and screenshots. Nothing creates, edits,
deletes, or submits anything in NetSuite.

WHY NETSUITE NEEDS ITS OWN MODULE
---------------------------------
Three things differ from Workato and each one breaks a naive port:

  1. Sign-in and the application live on different hosts. You authenticate at
     system.netsuite.com and end up on <account>.app.netsuite.com. A "are we
     logged in" check written against one host is wrong for the other.
  2. A user with more than one role lands on a role picker, not the app. That
     page looks logged in and is not yet usable.
  3. Almost every list is inside an iframe, and the useful ones paginate. Reading
     document.body of the outer page finds nothing at all.

MFA
---
NetSuite pushes most administrator roles through 2FA. No script can complete
that. Form sign-in is attempted, and when it does not land the run hands over to
a person in the browser session, exactly as the Workato captures do.
"""

import re
import time

from workato_sox_capture import (
    APP_NAME, SCROLL_BY_JS, SCROLL_JS,
    activate_app, grab_screen, log, now_stamp, slug,
)

LOGIN_URL = "https://system.netsuite.com/pages/customerlogin.jsp?country=US"

# Pages that mean "not signed in yet", even though they render fine.
LOGIN_MARKERS = ("customerlogin.jsp", "/pages/login", "login.nl", "/idp/", "saml")
# The role picker - signed in, but not yet anywhere useful. NetSuite spells this
# "chooserole" on the way in and "changerole" once inside; matching only the
# latter meant a successful sign-in was reported as "2FA needed" and the account
# warning never fired.
ROLE_MARKERS = ("chooserole", "changerole", "rolelist", "setuprole")

EMAIL_SELECTORS = (
    "input#userName", "input[name='email']", "input#email",
    "input[type='email']", "input[name='userName']",
)
PASSWORD_SELECTORS = (
    "input#password", "input[name='password']", "input[type='password']",
)
SUBMIT_SELECTORS = (
    "input#submitButton", "button#submitButton", "input[type='submit']",
    "button[type='submit']", "button:has-text('Log In')", "button:has-text('Login')",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def account_host(account_id):
    """7258820_SB1 -> 7258820-sb1.app.netsuite.com (the host NetSuite serves the UI on)."""
    slug_id = str(account_id).strip().lower().replace("_", "-")
    return f"{slug_id}.app.netsuite.com"


def account_base(account_id):
    return f"https://{account_host(account_id)}"


# --------------------------------------------------------------------------- #
# sign-in                                                                      #
# --------------------------------------------------------------------------- #
def _fill_first(frame, selectors, value):
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            if el.count() and el.is_visible():
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def has_visible_password(page):
    for sel in PASSWORD_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                return True
        except Exception:
            continue
    return False


def page_text(page):
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def at_role_picker(page):
    url = page.url.lower()
    if any(m in url for m in ROLE_MARKERS):
        return True
    txt = page_text(page).lower()
    return "choose a role" in txt or "select a role" in txt


def looks_logged_in(page, expect_host):
    """Signed in AND on the expected account, not just past the password box.

    The host check matters: signing in at system.netsuite.com can land you on a
    different account than the one being reviewed, and screenshots from the wrong
    account are evidence for the wrong population.
    """
    url = page.url.lower()
    if any(m in url for m in LOGIN_MARKERS):
        return False
    if has_visible_password(page):
        return False
    if at_role_picker(page):
        return False
    return expect_host.lower() in url


def try_form_login(page, username, password):
    """Fill and submit the NetSuite login form. True if it was submitted."""
    if not username or not password:
        return False
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        time.sleep(2)
        if not _fill_first(page, EMAIL_SELECTORS, username):
            log("  no email field on the NetSuite login page - handing over to a person")
            return False
        if not _fill_first(page, PASSWORD_SELECTORS, password):
            log("  no password field on the NetSuite login page - handing over to a person")
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


def sign_in(ctx, page, account_id, username, password, mode, session_url="",
            timeout_s=900):
    """Get to a usable page on the right account. Returns the page, or None.

    Order matters: try the stored session first (NetSuite often keeps you signed
    in), then the configured credentials, then hand over to a person. MFA and SSO
    always end up in the last branch - nothing can automate a push notification.
    """
    host = account_host(account_id)
    base = account_base(account_id)

    try:
        page.goto(base, wait_until="domcontentloaded")
        time.sleep(3)
    except Exception:
        pass

    if looks_logged_in(page, host):
        log(f"Already signed in to {host}.")
        return page

    if mode == "form" and username and password:
        log(f"Attempting NetSuite form sign-in as {username}")
        if try_form_login(page, username, password):
            # NetSuite's redirect chain is slow, and the role picker often appears
            # well after 30s. Judging too early reported a working sign-in as a
            # failure.
            for _ in range(30):
                if at_role_picker(page):
                    log("  signed in, and NetSuite is asking which ROLE to use")
                    break
                if looks_logged_in(page, host):
                    log(f"Signed in with the configured credentials. At {page.url}")
                    return page
                time.sleep(2)
            else:
                log("  form sign-in did not land - 2FA or SSO needs a person.")

    # Hand over.
    start = time.time()
    last = 0
    last_probe = time.time()
    warned_account = False
    activate_app(APP_NAME)
    while time.time() - start < timeout_s:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if not pages:
            log("!! The browser window was closed.")
            return None
        for p in pages:
            try:
                if looks_logged_in(p, host):
                    return p
            except Exception:
                pass

        here = pages[-1]
        on_role_picker = False
        try:
            on_role_picker = at_role_picker(here)
        except Exception:
            pass

        if time.time() - last > 15:
            from workato_sox_capture import phase

            phase("awaiting_login", session_url)
            if on_role_picker:
                log("Waiting: NetSuite is asking which ROLE to use. Pick an "
                    f"administrator role for account {account_id} in the browser session.")
                # The role picker names the account each role belongs to. Say so when
                # it is not the one under review, rather than let someone pick a role
                # in a different account and produce evidence for the wrong population.
                if not warned_account and account_id.lower().replace("_", "-") not in \
                        here.url.lower().replace("_", "-"):
                    log(f"   ! the default role is NOT in {account_id} - check the account "
                        f"column before choosing")
                    warned_account = True
            else:
                log("Waiting for NetSuite sign-in (2FA or SSO may be required).")
            for p in pages:
                log(f"   open tab: {p.url}")
            if session_url:
                log(f"   -> open {session_url} and finish signing in there.")
            last = time.time()

        # Re-navigating settles a sign-in the page object did not end up reflecting.
        # But NEVER while the role picker is up: that throws away a sign-in that has
        # already succeeded and drops the browser back at an empty login form, which
        # is exactly what it used to do.
        if not on_role_picker and time.time() - last_probe > 20:
            last_probe = time.time()
            try:
                here.goto(base, wait_until="domcontentloaded")
                time.sleep(3)
                if looks_logged_in(here, host):
                    return here
            except Exception:
                pass
        time.sleep(2)
    return None


# --------------------------------------------------------------------------- #
# reading NetSuite lists                                                       #
# --------------------------------------------------------------------------- #
# NetSuite renders most lists inside an iframe, and the outer document has none
# of the data. This runs against every frame and keeps the richest result.
EXTRACT_JS = r"""
() => {
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
  const tables = Array.from(document.querySelectorAll('table'));
  let best = null;

  for (const t of tables) {
    const trs = Array.from(t.querySelectorAll('tr'));
    if (trs.length < 2) continue;
    const rows = trs.map(tr =>
      Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText || '').trim()));
    const wide = rows.filter(r => r.length >= 2);
    if (wide.length < 2) continue;
    // NetSuite nests layout tables; the one with the most data rows is the list.
    if (!best || wide.length > best.rows.length) {
      best = {rows: wide, cols: Math.max(...wide.map(r => r.length))};
    }
  }
  if (!best) return null;

  // First row is the header when it names columns rather than holding data.
  let headers = [];
  let body = best.rows;
  const first = best.rows[0];
  const firstLooksHeader = first.length >= 2 && !first.some(c => EMAIL.test(c));
  if (firstLooksHeader) { headers = first; body = best.rows.slice(1); }

  return {
    headers: headers,
    rows: body,
    emails: body.filter(r => r.some(c => EMAIL.test(c))).length,
    url: location.href,
    title: document.title,
    text: (document.body && document.body.innerText || '').slice(0, 3000),
  };
}
"""


def read_list(page):
    """Best table found anywhere on the page, including inside iframes."""
    best = None
    frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    for frame in frames:
        try:
            data = frame.evaluate(EXTRACT_JS)
        except Exception:
            continue
        if not data or not data.get("rows"):
            continue
        if not best or len(data["rows"]) > len(best["rows"]):
            best = data
    return best


def header_index(headers, *names):
    for i, h in enumerate(headers):
        low = h.strip().lower()
        if any(n in low for n in names):
            return i
    return -1


def cell(row, idx):
    return row[idx].strip() if 0 <= idx < len(row) else ""


# --------------------------------------------------------------------------- #
# evidence capture                                                             #
# --------------------------------------------------------------------------- #
class NsCapture:
    """Whole-screen evidence capture, framed exactly like the Workato captures."""

    def __init__(self, page, out_dir, settle, account_id, display=1, max_parts=12):
        self.page = page
        self.out_dir = out_dir
        self.settle = settle
        self.account_id = account_id
        self.host = account_host(account_id)
        self.display = display
        self.max_parts = max_parts
        self.seq = 0
        self.manifest = []
        self.warnings = []

    def warn(self, msg):
        log(f"  !! {msg}")
        self.warnings.append(msg)

    def goto(self, url, retries=2):
        for attempt in range(1, retries + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                log(f"    could not open ({type(exc).__name__})")
                continue
            try:
                self.page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass
            time.sleep(self.settle)
            if not looks_logged_in(self.page, self.host):
                if attempt < retries:
                    log(f"    landed on {self.page.url} - retrying")
                    continue
                return False
            return True
        return False

    def shoot(self, kind, desc, url, part=1):
        self.seq += 1
        # Every capture re-checks the account is still the one under review. A
        # session that silently switched account would produce evidence for the
        # wrong population and look perfectly normal.
        if self.host not in self.page.url.lower():
            self.warn(f"{self.host} is not in the page URL for: {desc} ({self.page.url}) "
                      f"- CHECK THIS SCREENSHOT")
        self.page.bring_to_front()
        activate_app(APP_NAME)
        time.sleep(1.0)
        ts = now_stamp()
        name = (f"{self.seq:02d}_ns_{kind}_{slug(desc, 26)}_"
                f"{ts.strftime('%Y%m%d-%H%M%S')}{'' if part == 1 else f'_p{part}'}.png")
        path = self.out_dir / name
        grab_screen(path, self.display)
        log(f"  captured -> {name}")
        self.manifest.append({
            "seq": self.seq, "part": part, "kind": kind, "description": desc,
            "url": url, "captured": ts.isoformat(), "file": str(path),
        })

    def shoot_scrolled(self, kind, desc, url):
        """Capture the page, scrolling for the parts a long list needs."""
        info = None
        try:
            info = self.page.evaluate(SCROLL_JS)
        except Exception:
            pass
        planned = 1
        if info:
            remaining = info["total"] - info["height"] - info["top"]
            if remaining > info["height"] * 0.25:
                import math
                planned = min(self.max_parts,
                              1 + math.ceil(remaining / max(1, info["height"] * 0.9)))
        for part in range(1, planned + 1):
            if part > 1:
                try:
                    before = self.page.evaluate(
                        "() => (document.querySelector('[data-sox-scroll]') "
                        "|| document.scrollingElement).scrollTop")
                    after = self.page.evaluate(SCROLL_BY_JS, int(info["height"] * 0.9))
                    if after - before < 100:
                        break
                except Exception:
                    break
                time.sleep(1.0)
            self.shoot(kind, desc, url, part=part)
        return planned
