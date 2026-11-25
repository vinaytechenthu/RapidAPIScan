# Connecting the Lumina BI chatbot to RapidPulse

This guide is specific to `medm-lumina-api` as it is in the repository today
(commit `5d63f88`). Each step says what you get from it. Steps 1–2 alone already
give you Lumina runs, LLM calls, tokens and cost in RapidPulse. Steps 3–6 join the
other tiers into **one trace per chatbot question**.

## What the code does today (from the scan)

| Hop | Where in the code | Trace context today |
|---|---|---|
| Browser → **Web app / BFF** (Next.js, `gresham-edm-ui-next`, Windows EC2) | `src/app/api/v1/[...path]` streams to the API | not instrumented |
| BFF → **Lumina API** (FastAPI, container) | `app/main.py` `create_app()` | **no server span**. Lumina starts its own `lumina.run` root, so the web app's trace is not continued |
| Lumina → **LLM** (AWS Bedrock `converse`) | `app/agents/proxy.py:229` `lumina.llm` span, OpenInference attributes | ✅ RapidPulse reads `llm.model_name`, `llm.token_count.*`, `lumina.cost` as-is |
| Lumina → **MCP gateway** (`lumina-mcp`, streamable HTTP over `httpx2`) | `app/mcp/remote.py:185` `session.call_tool()` | **not propagated**. `httpx2` is not auto-instrumented, and the call runs inside the long-lived `_serve` task, not the caller's context |
| Gateway → **Data Fabric API** (`medm-datafabric-api`, Keycloak client credentials) | `lumina-mcp/gateway.py:412` `request()` (httpx) | not instrumented |
| Data Fabric → **Cube API** (`cubejs/cube`) → **SQL Server** | separate repos | not instrumented |

Traces currently go to Phoenix (`OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix:6006`)
and logs go to the compose `otel-collector`. The allow-list in
`app/observability/tracing.py` (`SAFE_ATTRIBUTES`) drops every attribute it
doesn't list. That is deliberate, so the steps below *extend* the list rather
than bypass it.

---

## 1. Fan out through the collector (no code change)

Replace `deploy/otel-collector.yaml` with [`otel-collector.yaml`](otel-collector.yaml)
from this folder, then in `docker-compose.yml` (service `api`):

```yaml
OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318   # was http://phoenix:6006
LUMINA_OTEL_METRICS: "true"                               # RapidPulse accepts OTLP metrics
```

and on `otel-collector`:

```yaml
environment:
  PULSE_OTLP_ENDPOINT: http://<pulse-host>:4400
  PULSE_KEY: ${PULSE_KEY}
  PULSE_HOST_NAME: ${HOSTNAME}
```

Phoenix still gets every trace. RapidPulse now gets traces, the ECS logs (correlated
to traces by `trace_id`), and metrics. Every record is stamped
`service.namespace=lumina-bi-chatbot` and the EC2 host.

## 2. Lumina API: server spans, resource, allow-list

`app/observability/tracing.py`, in `_install()`. Add the application, host and
container to the resource:

```python
resource = Resource.create({
    "service.name": self.config.service_name,
    "service.namespace": os.environ.get("PULSE_APP", "lumina-bi-chatbot"),
    "deployment.environment": self.config.deployment_id,
    "host.name": os.environ.get("HOST_NAME", socket.gethostname()),
    "container.name": os.environ.get("CONTAINER_NAME", "medm-lumina-api"),
    "lumina.product": self.config.product,
    "openinference.project.name": self.config.service_name,
})
```

Extend `SAFE_ATTRIBUTES`. These are names and counts, never values:

```python
    # RapidPulse / OTel semantic conventions
    "gen_ai.system", "gen_ai.request.model", "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens", "mcp.method.name", "gen_ai.tool.name",
    "server.address", "error.type",
```

`app/agents/proxy.py:229`, on the `llm` span. This names the provider on the service map:

```python
with tracer().span("llm", **{"openinference.span.kind": "LLM",
                             "gen_ai.system": "aws.bedrock",
                             "lumina.stage": stage}) as span:
```

`app/main.py`, after `application = FastAPI(...)` (line 104). This creates real
HTTP server spans, extracts `traceparent` from the BFF, and gives the API page
its status codes:

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(
    application, excluded_urls="health,ready,metrics",
    exclude_spans=["send", "receive"])        # no per-chunk SSE spans
```

Add `opentelemetry-instrumentation-fastapi` to `requirements.txt`, pinned to
match `opentelemetry-api==1.44.0` (instrumentation `0.65b0`). The FastAPI span
records the **route template** (`/api/v1/runs/{run_id}`), never the concrete
path, so no ids leak. RapidPulse also strips query strings at ingest.

Because the server span becomes the current context, the existing
`tracer().start_root("run", ...)` becomes its child automatically. Nothing else
in the run path changes.

## 3. Lumina → MCP gateway: carry the trace in `_meta`

The OpenTelemetry MCP convention is `params._meta.traceparent`. A header hook on
`httpx2` would be wrong here, because the session is opened once by `_serve` and
would stamp *its* context on every call.

`app/mcp/remote.py`, where a call is enqueued (line 209). This code runs in the
**caller's** task, so open the client span here and capture its context:

```python
from opentelemetry.propagate import inject
from app.observability.tracing import tracer

with tracer().span("mcp_call", **{"mcp.method.name": "tools/call",
                                  "gen_ai.tool.name": name,
                                  "server.address": "lumina-mcp"}):
    carrier: dict = {}
    inject(carrier)                     # {"traceparent": "00-<trace>-<this span>-01"}
    pending = {"name": name, "args": arguments, "fut": fut, "task": None, "meta": carrier}
    await self._requests.put(pending)
    ...existing wait on fut...          # keep the await inside the span so it times the call
```

In `_serve` (line 162), pass `item["meta"]` to `_answer`. In `_answer`
(line 185), forward it:

```python
res = await asyncio.wait_for(
    session.call_tool(name, arguments, meta=meta),   # check: mcp 2.2.0 ClientSession.call_tool(meta=…)
    timeout=self.CALL_TIMEOUT)
```

Add `"mcp_call"` to `SPANS`. If the pinned SDK's `call_tool` doesn't take
`meta`, send it as `_meta` in the request params. The gateway (step 4) reads
either one.

## 4. MCP gateway: continue the trace, propagate to Data Fabric

`lumina-mcp/gateway.py`, in `on_call_tool` (line 482):

```python
from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind
_tr = trace.get_tracer("lumina-mcp")

async def on_call_tool(ctx, params):
    meta = getattr(params, "meta", None) or {}
    meta = meta if isinstance(meta, dict) else getattr(meta, "model_extra", {}) or {}
    with _tr.start_as_current_span(f"tools/call {params.name}", context=extract(meta),
                                   kind=SpanKind.SERVER,
                                   attributes={"mcp.method.name": "tools/call",
                                               "gen_ai.tool.name": params.name}) as span:
        ...existing body...
        if is_err:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "tool error"))
```

In `main()` (line 3324), before serving:

```python
import sys; sys.path.insert(0, "/opt/pulse")        # or vendor pulse_otel.py next to gateway.py
from pulse_otel import init_telemetry
init_telemetry("lumina-mcp", app="lumina-bi-chatbot", logs=True)
```

`init_telemetry` turns on `HTTPXClientInstrumentor`, so every upstream call in
`request()` (Data Fabric, Keycloak token, EDM) gets a client span and a
`traceparent` header. The Data Fabric API continues the trace from there. The
gateway never records headers, so the bearer can't reach the APM.

Requirements: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`opentelemetry-instrumentation-httpx`. Env: `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`.

## 5. Data Fabric API and Cube API

**Data Fabric API** (`medm-datafabric-api`). If it is Python/FastAPI, use the
same one call:

```python
from pulse_otel import init_telemetry, llm_span, mcp_tool_span
init_telemetry("fabric-api", app="lumina-bi-chatbot", fastapi_app=app)
```

Wrap its Bedrock calls with `llm_span(...)` and its MCP calls with
`mcp_tool_span(...)`, which returns the `_meta` to send. For zero-code, run it
under `opentelemetry-instrument` with the `OTEL_*` variables shown in
[`integrations/services.env.example`](../../integrations/services.env.example). For a .NET or Node build,
use the OpenTelemetry auto-instrumentation for that runtime with the same
variables.

**Cube API.** Build [`integrations/cube/Dockerfile`](../../integrations/cube/Dockerfile), which is
`cubejs/cube` plus the OpenTelemetry Node auto-instrumentation. HTTP server spans
continue Fabric's trace. The `tedious` instrumentation emits one `db.system=mssql`
span per SQL Server query, with timing and statement. RapidPulse removes literals
before storing.

## 6. Web application (Windows EC2)

The BFF is Next.js. Add [`integrations/nextjs/instrumentation.ts`](../../integrations/nextjs/instrumentation.ts)
to `gresham-edm-ui-next/src/`. Its outbound `fetch` to `/api/v1/...` then carries
`traceparent`, and Lumina's FastAPI span (step 2) continues it. The trace starts
at the user's click.

## 7. Servers, containers, SQL Server

On each EC2 instance:

| Server | Command |
|---|---|
| Web (Windows) | `deploy/install-agent.ps1 -Server http://<pulse>:4400 -Role web -App lumina-bi-chatbot -Checks "Web login=https://localhost/lumina@web-app"` |
| Lumina/MCP (Linux) | `deploy/install-agent.sh http://<pulse>:4400 app lumina-bi-chatbot` (collects Docker containers) |
| Fabric/Cube (Linux) | same as above |
| SQL Server (Windows) | `deploy/install-agent.ps1 ... -Role database -Mssql "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;Trusted_Connection=yes;TrustServerCertificate=yes"` |

Also add the central monitors in [`pulse.yaml`](pulse.yaml) (`monitors:`). They detect
"service down" from outside the box, which still works when that box's own agent
has gone silent.

## Result

One chatbot question gives a single trace:

```
web-app  POST /api/chat
└ lumina-api  POST /api/v1/runs
  ├ lumina.run → lumina.plan → lumina.llm (Bedrock: model, tokens, cost)
  ├ tools/call query_cube  ──►  lumina-mcp  tools/call query_cube
  │                              └ POST datafabric  ──►  fabric-api  POST /api/v1/query
  │                                                       ├ chat claude-haiku (Bedrock)
  │                                                       └ POST cube  ──►  cube-api  POST /cubejs-api/v1/load
  │                                                                          └ mssql SELECT … (SQL Server)
  ├ lumina.execute (Polars)
  └ lumina.llm conclude
```

For any slow or failed question, RapidPulse shows which tier, which call, and how
much of the time each one used, plus the host and container state at that
moment.
