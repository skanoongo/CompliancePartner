# Runbook

## Monthly Workato change-management review

1. `docker compose up -d` and open the site.
2. **Workato** → **Change management**. Pick the **Workspace** the recipes live in
   and the **Project** to cover (or *All projects*), then **Prepare**.
3. If it asks for sign-in, click **Open the browser session**, complete SSO in that
   tab, and switch to the workspace you selected if it is not already there. Then
   leave the tab alone - the capture is driving that browser.
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

The capture continues once the browser is on a Workato URL that is not a login page
and shows no password box. If you have signed in and it is still waiting, open the
browser session and check you are not sitting on an SSO interstitial or a consent
screen. It re-navigates to the app every 20s to re-check, so a sign-in that completed
through a redirect chain is picked up on the next probe rather than missed.

To start over from a clean session: `curl -X POST http://localhost:8080/api/session/reset`.

### The workbook has fewer tabs than expected

A scoped run only builds tabs for the projects it captured. An out-of-scope project
gets no sheet at all, deliberately - a blank sheet saying "no changes" would be a
claim the run never checked. `manifest.json` records the scope under `projects`.

### It aborted on the workspace check

```
Workspace 'Production' was never visible after 20 checks.
```

The **Workspace** chosen on the page is not the one the browser is showing. Either
switch workspace in the browser session, or choose the right one in the Workspace box
and run it again. The list in that box comes from `workspaces:` in
`config/environments.yaml`. It aborts rather than continuing
because evidence captured from the wrong workspace is evidence for the wrong
population - it would look fine and be wrong.

### Warnings in the finished run

Every warning is listed on the finished panel and in `manifest.json`. None of them
stop a run - they mark evidence a person has to look at before signing off.

```
'Production' not visible on page for: <page> - CHECK THIS SCREENSHOT
```
The shot was taken but the workspace could not be confirmed on that page. Open it in
the workbook and check it by eye.

```
could not stay on folder 27517768 (CPQ > Customer); the assets screenshot is of
whatever Workato redirected to - check it
```
The folder id does not resolve for the signed-in account, so Workato bounced to
somewhere else and the screenshot is of that instead. Usually the wrong workspace:
folder ids are per-workspace, so ids recorded against Production will not resolve in
another one. Either select the workspace those folders live in, or update `fid` in
`SECTIONS`.

```
CPQ > Customer: expected 2 recipes, found 0 after a reload
```
The folder held fewer recipes than the scope says it should, and a reload did not
recover it. Either the population genuinely changed - which is a change to write up -
or the page never loaded fully. Raise `CAPTURE_SETTLE` and re-run to tell them apart.

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

`SECTIONS` at the top of `runner/workato_sox_capture.py`. Each section's `tab` is
what the **Project** box offers, so adding one there adds it to the page. Each folder
carries:

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
