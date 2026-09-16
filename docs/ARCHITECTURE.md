# Architecture

Two containers. One serves a page, one runs a browser and photographs it.

```
                    ┌─────────────────────────────────────────────┐
  browser  ────────▶│ web   nginx :8080                           │
                    │       cw_CompliancePartnercore.internal...  │
                    │                                             │
                    │  /              index.html + live-capture.js│
                    │  /api/     ──┐                              │
                    │  /session/ ──┤                              │
                    └──────────────┼──────────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────────────┐
                    │ runner                                      │
                    │                                             │
                    │  :8000  app.py .............. job API       │
                    │  :6080  noVNC ............... the SSO login │
                    │  :5900  x11vnc                              │
                    │   :99   Xvfb 1680x1050                      │
                    │           ├── xclock  (top 28px strip)      │
                    │           └── Chromium (below the strip)    │
                    │                                             │
                    │  workato_sox_capture.py                     │
                    │  /data  profile + runs (volume)             │
                    └─────────────────────────────────────────────┘
```

## Why a whole desktop and not a headless browser

The evidence requirement drives everything else here.

A screenshot of page *content* proves only what some markup rendered to. Evidence for
a change-management control has to show **where the page came from and when**: the
browser URL bar, and a clock. That is why the original script takes whole-screen
captures - the macOS `Cmd+Shift+3` framing, including the menu-bar clock - rather
than Playwright's `page.screenshot()`.

Carrying that requirement into a container means the container needs a screen:

| Piece | Stands in for |
|---|---|
| `Xvfb` on `:99` | the physical display |
| `xclock` in a 28px strip at the top | the macOS menu-bar clock |
| Chromium at `y=28`, sized `H-28` | the browser window, kept clear of the clock |
| `x11vnc` + noVNC on `:6080` | sitting at the machine to complete SSO |

The browser is positioned *below* the clock strip rather than layered under it.
Xvfb runs with no window manager, so stacking order is just mapping order and a
full-screen browser would simply cover the clock. Reserving the strip makes it
impossible for the timestamp to be missing from a capture.

`CAPTURE_WINDOW_OFFSET_Y` carries the strip height from `entrypoint.sh` into the
capture script. It is `0` on a real desktop, where the OS keeps its own bar on top.

## The capture script runs on three platforms

`workato_sox_capture.py` has one platform layer; everything above it is
platform-blind.

| | screen size | raise window | grab | clock in the shot |
|---|---|---|---|---|
| macOS | AppleScript | AppleScript | `screencapture -x -D` | menu bar |
| Linux | `SCREEN_W/H`, `xdpyinfo` | `wmctrl` | `mss` | `xclock` |
| Windows | `GetSystemMetrics` | `SetForegroundWindow` | `mss` | taskbar |

macOS keeps its original path untouched, so a run on someone's Mac produces exactly
what it did before this repo existed.

### The non-interactive changes

The script was written to be run by a person at a terminal. In the container there
is no terminal, and three things had to change to make that safe rather than merely
quiet:

- **`enter_pressed()` now returns `False` when stdin is not a TTY.** It polls stdin
  with `select()`, and a redirected stdin reports readable immediately. Unguarded,
  the login wait would read that as the operator pressing Enter and start capturing
  a signed-out browser.
- **`ensure_workspace()` no longer blocks on `input()`.** With no terminal it
  re-checks on a timer and then aborts. Capturing the wrong workspace produces
  evidence for the wrong population, which is worse than producing none.
- **The confirm-before-capture prompt is skipped** when there is nothing to prompt on.

It also emits `PHASE::<name>::<detail>` lines. The job API reads those instead of
pattern-matching log prose, so the progress the page shows cannot drift out of sync
with wording changes.

## The job API

`app.py` is a small Flask app run under waitress. It starts the capture as a
subprocess with `stdin=DEVNULL`, streams the output into a job record and a log
file, watches for phase markers, and indexes what the run produced when it exits.

**One job at a time**, enforced with a lock. The capture drives a single X display
and a single browser profile; two concurrent runs would photograph each other's
windows. The second request gets a `409` explaining why.

Artifact downloads resolve the requested name and confirm the result is still inside
the run's output directory, so a crafted name cannot climb out of it.

`/data` is a volume holding the browser profile and every run's output, so an SSO
session and the evidence it produced both survive a restart.

## The page

`index.html` is the prototype, unmodified except for three disclaimer strings that
would otherwise have been false, and two tags loading the additions.

`live-capture.js` loads after it and overrides three functions by reassignment:

- `startPrepare()` - POSTs to `/api/prepare` and polls, but only for a system and
  control that `/api/capabilities` says is real. Otherwise it calls straight through
  to the original simulation.
- `workflow(r)` - renders the live panel (phase, log tail, sign-in prompt, then the
  real artifacts) when a record has a job attached, and the original otherwise.
- `render()` - badges the control rows that are backed by a real capture.

The human workflow after the capture - the three validation checks, submission,
management review, sign-off - is the prototype's, reached through the same handlers.
The capture replaces where the evidence comes from, not what a person then does
with it.

If the API is unreachable, `/api/capabilities` fails, `LIVE` stays empty, and the
page is exactly the prototype it was before.
