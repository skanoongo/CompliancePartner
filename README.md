# Compliance Partner

A website and a capture runner, in Docker. The site is the Compliance Partner
prototype; behind two of its buttons are real SOX evidence runs.

```
Workato   →  Change management (CM-02)   →  Prepare
Workato   →  User access review (UA-04)  →  Prepare
NetSuite  →  User access review (UA-04)  →  Prepare
NetSuite  →  Change management (CM-02)   →  Prepare
```

**Change management** signs in to Workato, walks every in-scope recipe folder and
version history, screenshots each one, and builds an Excel workbook from what it saw.

**User access review** opens the workspace's collaborator listing and reads who has
access: name, email, role and the status exactly as the page showed it, split into
active, inactive/suspended and pending. The listing appears on the page itself, and
downloads as `users.csv`, `users.json` and a workbook with a tab per bucket.

**NetSuite user access review** reads Manage Users, Employees and Roles from the
configured account and records every user-role pairing with the access columns
NetSuite shows. Access in NetSuite is granted per user-ROLE, so one person appears on
several rows; the workbook reports both the rows (the unit of review) and the
distinct-people count (for reconciling against HR).

**NetSuite change management** walks the customization lists - scripts, deployments,
workflows, custom record types, fields and forms - and records the population and the
columns NetSuite shows for each. It deliberately does **not** claim to be an approval
trail: NetSuite keeps System Notes one record at a time, and approvals live in the
ticketing system the change was raised in. Matching population to approved changes
stays a human step, and the workbook says so on its Summary tab.

Every run shows progress as it happens and is evidenced by whole-screen screenshots.

Every other system and control on the page is still a simulation, and says so. That
line is deliberate: a compliance tool that looks the same whether or not it touched
a real system will eventually be believed when it should not be. Controls backed by
a real capture carry a **Live capture** badge.

The capture is **read-only**. It navigates and screenshots. It never starts, stops,
edits, creates or deletes anything in Workato.

## Quick start

```bash
docker compose up -d --build
open http://localhost:8080/
```

Select **Workato** → **Change management**, choose a **Workspace** and a **Project**,
then **Prepare**. The first run needs a one-time SSO sign-in: the page shows an
**Open the browser session** button, which opens the runner's own browser in a tab.
Sign in there and the capture carries on by itself. The session is remembered, so
later runs start straight away.

A full run takes roughly 20-40 minutes and produces ~30 screenshots; a single
project is proportionally quicker.

### User access review, and what "active" means

Workato has no single active/inactive flag on a collaborator. It shows a state per
row - active, pending, suspended, deactivated - worded differently by plan and page.
So the capture records the **raw status exactly as displayed** and derives a verdict
from it separately. Both are in every output.

A status it does not recognise is marked `unknown` and raised as a warning. It is
never assumed inactive, because assuming inactive is what hides live access from a
reviewer - the one error a user access review must not make.

The listing is **point-in-time**: Workato publishes no historical roster, so a
review "for Q3 FY26" is a current snapshot labelled with that period and evidenced
by the timestamp in each screenshot. The workbook says so on its face. The period
comes from the Period selector at the top of the page.

If no collaborator page can be opened, or the page opens but no rows parse, the run
says so loudly and produces no user list. An empty list is never reported as "this
workspace has no users".

### Workspace and Project

Both are part of the run's scope, so both are recorded on the job and in
`manifest.json`, and both are shown on the finished evidence.

**Workspace** is checked, not just labelled. The capture confirms the name is
visible on every page before it shoots and aborts if it is not, so a run can never
quietly collect evidence from the wrong workspace. The list comes from
`workspaces:` in `config/environments.yaml` - list the ones the account can really
reach, because a name that does not exist fails several minutes into a run, after
the sign-in.

**Project** narrows the capture to one area. Only the projects captured get a tab in
the workbook: an out-of-scope project has no sheet at all rather than an empty one,
so a scoped run cannot be misread as "nothing changed" everywhere else.

### NetSuite specifics

Three things differ from Workato and each one breaks a naive port, so
`netsuite_common.py` handles them:

- **Sign-in and the app are on different hosts.** You authenticate at
  `system.netsuite.com` and end up on `<account>.app.netsuite.com`. Every capture
  re-checks the account host on each screenshot, because evidence from the wrong
  account looks entirely normal.
- **A multi-role user lands on a role picker**, which looks signed in and is not yet
  usable. The capture detects it and asks for a role to be chosen.
- **Lists live in iframes and paginate.** Reading the outer document finds nothing,
  so extraction runs against every frame and keeps the richest table.

**2FA.** NetSuite pushes most administrator roles through it, and nothing can automate
that. Form sign-in is attempted; when it does not land, the run pauses and asks a
person to finish in the browser session — the same fallback as Workato.

## Sign-in (Okta)

Turn this on before anyone but you can reach the site.

```bash
cp config/auth.example.yaml config/auth.yaml
# fill in issuer / client_id / redirect_uri, then:
docker compose up -d --build
```

In Okta: **Applications → Create App Integration → OIDC → Web Application**. Web, not
SPA — the code exchange happens in the runner, so no token ever reaches the browser;
PKCE is used as well. The sign-in redirect URI must match `redirect_uri` exactly.

**Why it matters more than a usual login.** The runner keeps a browser signed in to
the system under review and serves it at `/session`. Unauthenticated, anyone who
opens the site gets an interactive, already-authenticated window into that system —
not a screenshot of one. The capture is read-only; that window is not.

With sign-in on, nginx gates `/`, `/api/`, `/session/` and `/choose` through
`auth_request`; only `/login`, `/auth/*` and the stylesheet are reachable signed out.
Browsers are redirected to `/login`, API calls get a 401.

**Entitlements.** After signing in, people land on `/choose` — the SOX systems their
Okta groups allow, mapped in `config/auth.yaml`. Deny by default: an unmapped group
grants nothing, and a user in no mapped group signs in to an empty picker rather than
to everything. The `?system=` the picker links to decides what is *shown*;
`/api/prepare` re-checks entitlement server-side on every run, so editing the URL
grants nothing.

With no `config/auth.yaml` the site has **no authentication** and says so on the login
page and the picker. That is the old behaviour, kept so an existing local demo does
not break the moment the file lands — but it is not a state to leave a shared
deployment in.

## Publishing it under its hostname

The site answers to `cw_CompliancePartnercore.internal.coreweave.com`. To reach it
by that name locally, add a hosts entry and serve it on port 80:

```bash
sudo sh -c 'echo "127.0.0.1  cw_CompliancePartnercore.internal.coreweave.com \
cw-compliancepartnercore.internal.coreweave.com" >> /etc/hosts'

WEB_PORT=80 docker compose up -d
open http://cw_CompliancePartnercore.internal.coreweave.com/
```

> **On the underscore.** Hostnames may not contain `_` (RFC 1123). Browsers are
> mostly forgiving, but resolvers, proxies and TLS certificate tooling are not, and
> Docker's own DNS refuses it as a network alias. nginx therefore answers to *both*
> spellings, and `cw-compliancepartnercore.internal.coreweave.com` is the one to use
> anywhere the underscore is rejected. If this name is going into real internal DNS,
> register the hyphenated form.

## What is in here

| Path | What it is |
|---|---|
| `web/` | nginx image serving the site and proxying the API and browser session |
| `web/html/index.html` | the Compliance Partner page |
| `web/html/live-capture.js` | replaces the simulated Prepare with a real run, for Workato/CM-02 only |
| `runner/` | the capture image: virtual desktop, browser, capture script, job API |
| `runner/workato_sox_capture.py` | change-management capture, and the shared browser/screen layer |
| `runner/workato_uar_capture.py` | user access review: collaborator listing + evidence |
| `runner/netsuite_common.py` | NetSuite sign-in, account checks, iframe list reading |
| `runner/netsuite_uar_capture.py` | NetSuite user + role listing |
| `runner/netsuite_sox_capture.py` | NetSuite customization change population |
| `runner/app.py` | the job API the page talks to |
| `runner/auth.py` | Okta OIDC sign-in, sessions, and SOX system entitlements |
| `web/html/login.html` | sign-in page |
| `web/html/choose.html` | which SOX system am I working on |
| `runner/entrypoint.sh` | builds the virtual desktop the capture photographs |
| `docs/ARCHITECTURE.md` | why the runner carries a whole desktop, and how the pieces fit |
| `docs/RUNBOOK.md` | running a monthly review, and what to do when one goes wrong |

## Why the runner carries a whole desktop

The screenshots are whole-screen, not page captures, because the evidence has to
show *where the page came from and when* - the browser URL bar and a clock - not
just what the page said. A cropped page screenshot proves nothing about its source.

So the container runs an actual X display: `Xvfb` for the screen, `xclock` in a
reserved strip at the top standing in for the macOS menu-bar clock, and the browser
positioned below that strip so it can never cover the timestamp. `x11vnc` and noVNC
expose the same display over http, which is how the SSO sign-in happens.

`docs/ARCHITECTURE.md` goes through this in more detail.

## Running the capture without the website

```bash
# a specific month
docker compose run --rm runner capture --month "August 2026"

# one project, in a named workspace
docker compose run --rm runner capture --project "2. CPQ" --workspace "CW Agentic"

# what projects are there?
docker compose run --rm runner capture --list-projects

# the user access review
docker compose run --rm runner users --period "Q3 FY26" --workspace Production

# NetSuite
docker compose run --rm runner ns-users   --period "Q3 FY26"
docker compose run --rm runner ns-changes --period "Q3 FY26" --area scripts,workflows
docker compose run --rm runner ns-changes --list-areas

# screenshots and manifest only, no workbook
docker compose run --rm runner capture --capture-only

# rebuild the workbook from a finished run
docker compose run --rm runner capture --build-only --out /data/runs/<job>/out
```

The script also runs directly on a Mac or a Windows machine, against the real
desktop, with no container at all:

```bash
pip3 install playwright openpyxl pillow mss
python3 -m playwright install chromium
python3 runner/workato_sox_capture.py
```

On macOS it uses `screencapture` and the menu-bar clock, and asks once for Screen
Recording permission. On Windows it grabs the desktop and the taskbar clock, so
leave the taskbar visible. In the container it grabs the X screen. Same output
either way.

## Configuration

Copy `.env.example` to `.env` and edit. Everything has a working default.

| Variable | Default | What it does |
|---|---|---|
| `WEB_PORT` | `8080` | host port for the site; set `80` to use the bare hostname |
| `TZ` | `America/New_York` | the timezone the captured clock reads in |
| `CAPTURE_WORKSPACE` | `Production` | fallback workspace when the page sends none |
| `CAPTURE_SETTLE` | `4.0` | seconds to wait after each page load before shooting |
| `SCREEN_W` / `SCREEN_H` | `1680` / `1050` | virtual screen size |

`CAPTURE_WORKSPACE` is a safety check, not a preference: the capture confirms the
named workspace is visible on the page before it shoots, and aborts rather than
quietly collecting evidence from the wrong one.

## API

The page drives these; they are also usable directly.

| Endpoint | Purpose |
|---|---|
| `GET /auth/config` | whether sign-in is on (no secrets) |
| `GET /auth/login` → `/auth/callback` | the Okta round trip |
| `GET /auth/me` | who is signed in, and which systems they may work on |
| `GET /auth/verify` | what nginx asks on every request |
| `GET /auth/logout` | drop the session |
| `GET /api/health` | runner state, whether an SSO session is stored |
| `GET /api/capabilities` | which system/control pairs are backed by a real capture |
| `GET /api/scope` | the workspaces and projects the selectors offer |
| `POST /api/prepare` | start a run: `{system, controlId, period, workspace, project}` |
| `GET /api/jobs/<id>` | status, phase, log tail, artifacts, warnings |
| `GET /api/jobs/<id>/log` | the full log |
| `GET /api/jobs/<id>/artifacts/<name>` | download the workbook, manifest or screenshot bundle |
| `POST /api/jobs/<id>/cancel` | stop a run |
| `POST /api/session/reset` | forget the stored SSO session |

One run at a time. The capture drives a single display and a single browser profile,
so two at once would photograph each other's windows and mix up the evidence.

## Limits worth knowing

- **The runner holds a live Workato session.** With sign-in configured this is
  behind Okta; with no `config/auth.yaml` it is not behind anything, and anyone who
  can open `/session` is inside a signed-in Workato window. Configure Okta before
  the site is reachable by anyone else.
- **Sessions are cookies over whatever scheme you serve.** The cookie is marked
  Secure only when the request arrives as https. On plain http it is not, so put TLS
  in front before this leaves a trusted network.
- **The review decisions in the page are still simulated** and reset on reload. The
  capture is real; the sign-off workflow around it is a prototype.
- **The workbook is labelled with the current month.** The page's period selector is
  quarterly and CM-02 is a monthly control; mapping one onto the other would mean
  guessing a fiscal calendar. Use `--month` on the CLI to label a different period.
- **The scope is hard-coded** in `SECTIONS` in `workato_sox_capture.py` - folder ids
  and expected recipe counts. When the population changes, that list changes with it.
