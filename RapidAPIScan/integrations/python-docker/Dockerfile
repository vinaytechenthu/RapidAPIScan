# Add OpenTelemetry zero-code instrumentation to ANY existing Python container image,
# without changing its code.
#
# Easiest: let deploy/instrument-container.py build this for a running container. It copies the
# container's settings, adds the OTEL_* variables and rolls back if the new container is unhealthy.
#
# By hand:
#   docker build -t my-api-otel:1 --build-arg BASE=my-api:1 --build-arg RUN_AS=appuser .
#   docker run ... --entrypoint opentelemetry-instrument my-api-otel:1 <the image's original command>
# Setting ENTRYPOINT in a derived image clears the base image's CMD, so give the command explicitly
# (deploy/instrument-container.py appends ENTRYPOINT/CMD lines for you).
#
# BASE                  the image you run today
# OTEL_CONTRIB_VERSION  "auto" (default) matches the OpenTelemetry already in the image
#                       (e.g. SDK 1.41.1 <-> instrumentation 0.62b1), so nothing the app pinned is upgraded.
#                       If the image has no OpenTelemetry, the latest release is installed.
# RUN_AS                the image's user:  docker inspect -f '{{.Config.User}}' <BASE>   (empty = root)
# PIP_INDEX_URL         optional private mirror / proxy for PyPI
#
# Request capture: every server span also gets the request headers (OpenTelemetry's own
# OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_* settings) and the request payload
# (rapidpulse_capture.py, next to this file). Secrets are masked before anything leaves the
# container. Turn off with  -e RAPIDPULSE_CAPTURE_BODY_BYTES=0  and/or
# -e OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST=
ARG BASE
FROM ${BASE}
ARG OTEL_CONTRIB_VERSION=auto
ARG RUN_AS=root
ARG PIP_INDEX_URL=
USER root
COPY rapidpulse_capture.py /opt/rapidpulse/rapidpulse_capture.py
RUN set -e; \
    [ -n "$PIP_INDEX_URL" ] || unset PIP_INDEX_URL; \
    PY="$(command -v python3 || command -v python)"; \
    [ -n "$PY" ] || { echo "No python in this image: use the .NET/Java/Node agent instead." >&2; exit 1; }; \
    "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip; \
    ver() { "$PY" -c "import importlib.metadata as m; print(m.version('$1'))" 2>/dev/null || true; }; \
    C="$OTEL_CONTRIB_VERSION"; [ "$C" != auto ] || C="$(ver opentelemetry-semantic-conventions)"; \
    A="$(ver opentelemetry-api)"; \
    if [ -n "$C" ] && [ -n "$A" ]; then \
        echo "Matching the image's OpenTelemetry: api $A, instrumentation $C"; \
        "$PY" -m pip install --no-cache-dir "opentelemetry-distro==$C" "opentelemetry-instrumentation==$C" \
            "opentelemetry-exporter-otlp-proto-http==$A"; \
    else \
        echo "No OpenTelemetry in the image: installing the latest release"; \
        "$PY" -m pip install --no-cache-dir opentelemetry-distro opentelemetry-exporter-otlp-proto-http; \
    fi; \
    opentelemetry-bootstrap -a install; \
    SP="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    chmod 0644 /opt/rapidpulse/rapidpulse_capture.py; \
    for d in "$SP" $(find / -xdev -type d \( -name site-packages -o -name dist-packages \) \
                       -not -path '/proc/*' 2>/dev/null); do \
        [ -w "$d" ] || continue; \
        cp /opt/rapidpulse/rapidpulse_capture.py "$d/"; \
        printf '%s\n' 'import sys; exec("try:\n import rapidpulse_capture\nexcept Exception:\n pass")' > "$d/rapidpulse_capture.pth"; \
        chmod 0644 "$d/rapidpulse_capture.py" "$d/rapidpulse_capture.pth"; \
        echo "Request capture installed in $d"; \
    done; \
    "$PY" -c "import rapidpulse_capture"; \
    "$PY" -m pip list 2>/dev/null | grep -i '^opentelemetry-instrumentation-' || true
USER ${RUN_AS}
ENV OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_SEMCONV_STABILITY_OPT_IN=http \
    OTEL_PYTHON_FASTAPI_EXCLUDE_SPANS=receive,send \
    OTEL_PYTHON_EXCLUDED_URLS="health,healthz,readyz,livez,metrics" \
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST=".*" \
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS=".*auth.*,.*cookie.*,.*key.*,.*token.*,.*secret.*,.*session.*,.*signature.*,.*password.*,.*credential.*" \
    RAPIDPULSE_CAPTURE_BODY_BYTES=8192
ENTRYPOINT ["opentelemetry-instrument"]
