# Compliance Partner

A website and a capture runner, in Docker. The site is the Compliance Partner
prototype; behind one of its buttons is a real SOX evidence run.

```
Workato  →  Change management (CM-02)  →  Prepare
```

Pressing that signs in to Workato, walks every in-scope recipe folder and version
history, takes a whole-screen screenshot of each one, and builds an Excel workbook
from what it saw. The page shows the run as it happens and hands back the workbook,
the screenshots and a manifest.

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

Select **Workato** → **Change management** → **Prepare**. The first run needs a
one-time SSO sign-in: the page shows an **Open the browser session** button, which
opens the runner's own browser in a tab. Sign in there and the capture carries on by
itself. The session is remembered, so later runs start straight away.

A full run takes roughly 20-40 minutes and produces ~30 screenshots.

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
| `runner/workato_sox_capture.py` | the capture itself - runs on macOS, Linux and Windows |
| `runner/app.py` | the job API the page talks to |
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
| `CAPTURE_WORKSPACE` | `Production` | workspace that must be visible on every captured page |
| `CAPTURE_SETTLE` | `4.0` | seconds to wait after each page load before shooting |
| `SCREEN_W` / `SCREEN_H` | `1680` / `1050` | virtual screen size |

`CAPTURE_WORKSPACE` is a safety check, not a preference: the capture confirms the
named workspace is visible on the page before it shoots, and aborts rather than
quietly collecting evidence from the wrong one.

## API

The page drives these; they are also usable directly.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | runner state, whether an SSO session is stored |
| `GET /api/capabilities` | which system/control pairs are backed by a real capture |
| `POST /api/prepare` | start a run: `{system, controlId, period}` |
| `GET /api/jobs/<id>` | status, phase, log tail, artifacts, warnings |
| `GET /api/jobs/<id>/log` | the full log |
| `GET /api/jobs/<id>/artifacts/<name>` | download the workbook, manifest or screenshot bundle |
| `POST /api/jobs/<id>/cancel` | stop a run |
| `POST /api/session/reset` | forget the stored SSO session |

One run at a time. The capture drives a single display and a single browser profile,
so two at once would photograph each other's windows and mix up the evidence.

## Limits worth knowing

- **The runner holds a live Workato session.** Anyone who can reach the site can
  start a capture, and anyone who can open the browser session tab is inside a
  signed-in Workato window. There is no authentication in front of any of it. Keep
  it on a trusted network, or put an authenticating proxy in front before it goes
  anywhere shared.
- **The review decisions in the page are still simulated** and reset on reload. The
  capture is real; the sign-off workflow around it is a prototype.
- **The workbook is labelled with the current month.** The page's period selector is
  quarterly and CM-02 is a monthly control; mapping one onto the other would mean
  guessing a fiscal calendar. Use `--month` on the CLI to label a different period.
- **The scope is hard-coded** in `SECTIONS` in `workato_sox_capture.py` - folder ids
  and expected recipe counts. When the population changes, that list changes with it.
