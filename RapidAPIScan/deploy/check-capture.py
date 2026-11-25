"""Check RapidPulse request capture inside an instrumented Python container.

    docker cp deploy/check-capture.py <container>:/tmp/check-capture.py
    docker exec <container> python /tmp/check-capture.py [<app source folder, default /app>]

It changes nothing. It prints:
  1. the OpenTelemetry and capture-module versions and whether the capture hooks are in place;
  2. a self-test: one POST with a JSON payload through a throw-away FastAPI app, instrumented the
     same way as the real one, showing which span received the payload;
  3. places in the application code that read the request or wrap the ASGI app themselves
     (the usual reason a payload is read outside the instrumented path).
"""
import asyncio
import importlib.metadata as md
import json
import os
import re
import sys


def version(pkg):
    try:
        return md.version(pkg)
    except Exception:
        return "-"


print("== 1. Versions and hooks")
print("python", sys.version.split()[0], sys.executable)
for p in ("opentelemetry-api", "opentelemetry-sdk", "opentelemetry-instrumentation", "opentelemetry-instrumentation-asgi",
          "opentelemetry-instrumentation-fastapi", "opentelemetry-instrumentation-starlette", "fastapi", "starlette", "uvicorn"):
    print(f"  {p:42} {version(p)}")
try:
    import rapidpulse_capture as cap
    print("  capture module", cap.__file__, "max bytes", cap.MAX,
          "| span fallback hook:", "yes" if "_start_internal_or_server_span" in open(cap.__file__).read() else "NO (old file)")
except Exception as e:
    print("  capture module NOT importable:", e)
    sys.exit(1)
try:
    import opentelemetry.instrumentation.asgi as asgi
    print("  ASGI middleware patched:", getattr(asgi.OpenTelemetryMiddleware, "_rapidpulse", False),
          "| span-start patched:", getattr(getattr(asgi, "_start_internal_or_server_span", None), "_rapidpulse", "n/a"))
except Exception as e:
    print("  opentelemetry-instrumentation-asgi not importable:", e)

print("\n== 2. Self-test")
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from fastapi import FastAPI, Request
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()

    @app.post("/check")
    async def check(req: Request):
        return {"n": len(await req.body())}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    body = json.dumps({"content": "capture self-test", "password": "x"}).encode()

    async def run():
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        sent = []

        async def send(m):
            sent.append(m)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/check", "raw_path": b"/check", "query_string": b"", "root_path": "", "server": ("t", 80),
                 "client": ("c", 1), "headers": [(b"content-type", b"application/json"), (b"host", b"t")]}
        await app(scope, receive, send)
        return sent[0].get("status")

    print("  response status", asyncio.run(run()))
    found = False
    for s in exporter.get_finished_spans():
        got = s.attributes.get("http.request.body")
        print(f"  span {s.name!r:40} kind={s.kind.name:8} payload={got!r}")
        found = found or (got is not None and s.kind.name == "SERVER")
    print("  RESULT:", "OK - payload on the server span" if found else "PROBLEM - payload not on the server span")
except Exception as e:
    print("  self-test could not run:", type(e).__name__, e)

print("\n== 3. Application code that reads the request or wraps the app")
root = sys.argv[1] if len(sys.argv) > 1 else "/app"
pat = re.compile(r"instrument_app|OpenTelemetryMiddleware|FastAPIInstrumentor|BaseHTTPMiddleware|add_middleware|"
                 r"def asgi\b|await request\.(body|json|stream)|receive\(\)|\.receive\b|set_tracer_provider")
hits = 0
for dirpath, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in ("site-packages", "node_modules", ".venv", "__pycache__", "tests")]
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(dirpath, f)
            try:
                for i, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
                    if pat.search(line):
                        print(f"  {path}:{i}: {line.strip()[:150]}")
                        hits += 1
                        if hits >= 60:
                            raise StopIteration
            except StopIteration:
                break
    if hits >= 60:
        break
print(f"  ({hits} lines)" if hits else f"  nothing found under {root}")
