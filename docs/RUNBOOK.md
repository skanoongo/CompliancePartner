# Runbook

## Monthly Workato change-management review

1. `docker compose up -d` and open the site.
2. **Workato** → **Change management** → **Prepare**.
3. If it asks for sign-in, click **Open the browser session**, complete SSO in that
   tab, and switch the workspace to **CoreWeave → Production** if it is not already.
   Then leave the tab alone - the capture is driving that browser.
4. Wait. A run is roughly 20-40 minutes and ~30 screenshots. The panel shows the
   folder and recipe it is on.
5. When it finishes, download the workbook. **Read the warnings** if there are any.
6. Work through the three validation checks, submit, and sign off.

The workbook is labelled with the current month. For a different month, run it from
the CLI instead: `docker compose run --rm runner capture --month "August 2026"`.

## Getting the workbook into Google Sheets

Upload the `.xlsx` to Drive, open with Google Sheets, then **File → Save as Google
Sheets**. Two sheet names are truncated to fit Excel's 31-character limit and can be
renamed after upload:

- `1. Workday Payroll to NS GL` → `1. Workday Payroll to NetSuite GL`
- `6. CoStar to NS Fx Rates update` → `6. CoStar to NetSuite Fx Rates update`

## When something goes wrong

### "Waiting for login" never clears

The capture only continues once the page looks signed in - a Workato URL, and at
least two of *projects / recipes / connections / workspace* in the page text. If you
have signed in and it is still waiting, open the browser session and check you are
on the Workato dashboard rather than an SSO interstitial or a workspace picker.

To start over from a clean session: `curl -X POST http://localhost:8080/api/session/reset`.

### It aborted on the workspace check

```
Workspace 'Production' was never visible after 20 checks.
```

The browser is in the wrong workspace. Open the browser session, switch to
**CoreWeave → Production**, and run it again. It aborts rather than continuing
because evidence captured from the wrong workspace is evidence for the wrong
population - it would look fine and be wrong.

### Warnings in the finished run

```
!! 'Production' not visible on page for: <page> - CHECK THIS SCREENSHOT
```

The capture took the shot but could not confirm the workspace on that page. Open
that screenshot in the workbook and confirm it by eye before signing off. These are
listed in the panel and in `manifest.json`.

### A folder captured fewer recipes than expected

```
! expected 8 recipes, page shows 6 - reloading once
```

It reloads and re-counts. If the second count is still short, either the page was
slow (raise `CAPTURE_SETTLE`) or the population genuinely changed - in which case
the `expected` count and `known` ids in `SECTIONS` need updating, and the change
itself is worth noting in the workpaper.

### "A capture is already running"

One run at a time, by design. Wait, or stop the running one with **Stop**.

### The runner will not start

```bash
docker compose logs runner
```

`X display :99 never came up` means Xvfb failed - check the compose file has not
been given a screen size the server rejects. The entrypoint clears a stale
`/tmp/.X99-lock` on start, so a crash loop should not wedge on its own lock.

### Chromium crashes mid-run

Usually shared memory. `shm_size: "1gb"` is set in `docker-compose.yml`; if it has
been lowered, put it back.

## Changing what gets captured

`SECTIONS` at the top of `runner/workato_sox_capture.py`. Each folder carries:

- `fid` - the Workato folder id, or `None` to resolve it from a known recipe
- `known` - recipe id → name, used as a fallback when discovery comes up empty
- `expected` - how many recipes should be there, which drives the warning above

Recipe ids are discovered from the folder's assets page at run time; `known` exists
so a discovery failure degrades to a stale-but-labelled capture instead of an empty
one. After changing this, rebuild: `docker compose up -d --build`.

## Before this goes anywhere shared

There is no authentication in front of the site, the API or the browser session, and
the runner holds a signed-in Workato session. Anyone who can reach it can start a
capture or open a signed-in Workato window. Put an authenticating proxy in front of
it, or keep it on a trusted network.
