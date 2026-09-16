# The Compliance Partner site, published as cw_CompliancePartnercore.internal.coreweave.com
FROM nginx:1.27-alpine

# Unprivileged port, so the container does not need to run as root to bind it.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY html/ /usr/share/nginx/html/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1
