# Cube API with OpenTelemetry auto-instrumentation (HTTP server spans + SQL Server
# query spans via the `tedious` driver instrumentation). Drop-in for cubejs/cube.
#   docker build -t cube-otel:1 --build-arg CUBE_VERSION=v1.3.0 .
ARG CUBE_VERSION=latest
FROM cubejs/cube:${CUBE_VERSION}

USER root
RUN npm install --no-save --prefix /otel \
      @opentelemetry/api@1.9.0 \
      @opentelemetry/auto-instrumentations-node@0.56.0 \
    && chown -R node:node /otel 2>/dev/null || true
USER node

ENV NODE_PATH=/otel/node_modules \
    NODE_OPTIONS="--require /otel/node_modules/@opentelemetry/auto-instrumentations-node/build/src/register.js" \
    OTEL_SERVICE_NAME=cube-api \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_NODE_ENABLED_INSTRUMENTATIONS="http,express,tedious,net,dns" \
    OTEL_RESOURCE_ATTRIBUTES="service.namespace=default"
# Set at run time:  OTEL_EXPORTER_OTLP_ENDPOINT=http://<pulse-or-collector>:4400
#                   OTEL_EXPORTER_OTLP_HEADERS=x-pulse-key=<key>
#                   OTEL_RESOURCE_ATTRIBUTES=service.namespace=<application>,container.name=<container>
