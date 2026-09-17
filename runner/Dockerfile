# Capture runner: Workato SOX change-management evidence.
#
# Carries a whole virtual desktop, not just a browser, because the capture takes
# whole-screen shots that must show the URL bar and a clock. See entrypoint.sh.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_W=1680 \
    SCREEN_H=1050 \
    SCREEN_D=24 \
    CLOCK_H=28 \
    PORT=8000 \
    NOVNC_PORT=6080 \
    CAPTURE_DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# xvfb/x11-apps/x11-xserver-utils: the virtual screen and its clock
# x11-utils: xdpyinfo, which the entrypoint uses to know the display is really up
# x11vnc + novnc + websockify: the SSO login session, over http
# wmctrl: best-effort window raise
# tzdata: so the captured clock shows a real, configurable timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11-apps \
        x11-utils \
        x11-xserver-utils \
        x11vnc \
        novnc \
        websockify \
        wmctrl \
        fonts-dejavu-core \
        fonts-liberation \
        tzdata \
        ca-certificates \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache

COPY workato_sox_capture.py workato_uar_capture.py app.py environments.py entrypoint.sh ./
RUN chmod +x entrypoint.sh workato_sox_capture.py workato_uar_capture.py

# Credentials are MOUNTED at /config, never copied in: an image layer is forever,
# and it travels to whatever registry the image is pushed to.
ENV CAPTURE_CONFIG=/config/environments.yaml

# Browser profile and run output live here. Mount a volume so an SSO login and the
# evidence it produces both survive a container restart.
VOLUME ["/data"]

EXPOSE 8000 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
