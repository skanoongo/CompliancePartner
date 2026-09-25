#!/usr/bin/env bash
#
# Builds the virtual desktop the capture photographs, then starts the job API.
#
# The capture script takes WHOLE-SCREEN shots on purpose: the evidence has to show
# the browser URL bar and a clock, not just page content. So the container needs a
# real screen, not a headless browser:
#
#   Xvfb       an X display with nothing else on it
#   xclock     a clock in a reserved strip at the top - the stand-in for the macOS
#              menu-bar clock. The browser is positioned BELOW that strip
#              (CAPTURE_WINDOW_OFFSET_Y) so it can never cover the timestamp.
#   x11vnc     exposes that display, so a person can complete the SSO login
#   noVNC      serves the VNC session over http, so the login happens in a normal
#              browser tab instead of needing a VNC client
#
set -euo pipefail

: "${DISPLAY:=:99}"
: "${SCREEN_W:=1680}"
: "${SCREEN_H:=1050}"
: "${SCREEN_D:=24}"
: "${CLOCK_H:=28}"
: "${PORT:=8000}"
: "${NOVNC_PORT:=6080}"
: "${CAPTURE_DATA_DIR:=/data}"

export DISPLAY SCREEN_W SCREEN_H CAPTURE_DATA_DIR
export CAPTURE_WINDOW_OFFSET_Y="$CLOCK_H"

mkdir -p "$CAPTURE_DATA_DIR/runs"

log() { printf '[entrypoint] %s\n' "$1"; }

wait_for_x() {
    local xvfb_pid="$1"
    for _ in $(seq 1 100); do
        xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && return 0
        # If Xvfb died there is no point waiting out the rest of the timeout.
        kill -0 "$xvfb_pid" 2>/dev/null || { echo "Xvfb exited while starting" >&2; exit 1; }
        sleep 0.2
    done
    echo "X display $DISPLAY never came up after 20s" >&2
    exit 1
}

start_desktop() {
    # A container that is restarted after a crash inherits its own stale lock, and
    # Xvfb refuses to start on a display that still looks taken. Nothing else is
    # using this display inside the container, so clearing it is safe.
    rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}" 2>/dev/null || true

    log "Xvfb on $DISPLAY at ${SCREEN_W}x${SCREEN_H}x${SCREEN_D}"
    Xvfb "$DISPLAY" -screen 0 "${SCREEN_W}x${SCREEN_H}x${SCREEN_D}" -nolisten tcp &
    wait_for_x "$!"

    # Plain background so the screenshots have a predictable, non-distracting ground.
    xsetroot -solid '#1d2433' || true

    # The clock. Full width so it reads as a bar rather than a floating window, and
    # -update 1 so the seconds move - a frozen clock would be worthless as evidence.
    local geom="${SCREEN_W}x${CLOCK_H}+0+0"
    log "clock at $geom"
    xclock -digital -update 1 \
           -strftime '   %Y-%m-%d  %H:%M:%S %Z    [ Compliance Partner capture session ]' \
           -geometry "$geom" -bg '#11172a' -fg '#e8edf7' -face 'DejaVu Sans Mono:size=11' &

    if [ "${ENABLE_VNC:-1}" = "1" ]; then
        log "x11vnc + noVNC on :$NOVNC_PORT"
        x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 &
        websockify --web /usr/share/novnc "$NOVNC_PORT" localhost:5900 &
    fi
}

case "${1:-serve}" in
    serve)
        start_desktop
        log "capture runner starting on :$PORT"
        exec python3 app.py
        ;;
    capture)
        # Run the capture directly, no API. Everything after "capture" is passed
        # through, e.g. docker compose run --rm runner capture --month "May 2026"
        start_desktop
        shift
        exec python3 -m workato.sox_capture \
            --profile "$CAPTURE_DATA_DIR/browser-profile" "$@"
        ;;
    users)
        # The user access review, straight from the CLI:
        #   docker compose run --rm runner users --period "Q3 FY26"
        start_desktop
        shift
        exec python3 -m workato.uar_capture \
            --profile "$CAPTURE_DATA_DIR/browser-profile" "$@"
        ;;
    ns-users)
        #   docker compose run --rm runner ns-users --period "Q3 FY26"
        start_desktop
        shift
        exec python3 -m netsuite.uar_capture \
            --profile "$CAPTURE_DATA_DIR/netsuite-profile" "$@"
        ;;
    ns-changes)
        #   docker compose run --rm runner ns-changes --area scripts,workflows
        start_desktop
        shift
        exec python3 -m netsuite.sox_capture \
            --profile "$CAPTURE_DATA_DIR/netsuite-profile" "$@"
        ;;
    shell)
        start_desktop
        exec bash
        ;;
    *)
        exec "$@"
        ;;
esac
