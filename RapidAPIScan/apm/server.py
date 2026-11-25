"""RapidPulse server: OTLP/HTTP receiver + query API + web UI.

    python -m apm.server                 # uses config/pulse.yaml
    OTEL_EXPORTER_OTLP_ENDPOINT=http://<pulse-host>:4400   # in any instrumented service
"""
from __future__ import annotations

import base64
import csv
import io
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, monitors, otlp, reports
from .alerts import METRICS, AlertEngine
from .config import load_config
from .ingest import Ingestor
from .query import Queries, _where, pick_step
from .store import Store

log = logging.getLogger("pulse")
STATIC = Path(__file__).parent / "static"

RANGES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200,
          "24h": 86400, "2d": 172800, "7d": 604800, "30d": 2592000}


def time_window(request: Request) -> tuple[int, int]:
    p = request.query_params
    now = int(time.time())
    if p.get("from"):
        frm = int(float(p["from"]))
        to = int(float(p.get("to") or now))
    else:
        to = now
        frm = to - RANGES.get(p.get("range", "1h"), 3600)
    return frm, to


def create_app(cfg: dict | None = None, *, background: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    store = Store(cfg["storage"]["path"])
    q = Queries(store, cfg)
    ing = Ingestor(store, cfg)
    alerts = AlertEngine(store, q, cfg)
    alerts.seed_defaults()
    stop = threading.Event()

    def loop(name, interval, fn):
        def run():
            while not stop.wait(interval):
                try:
                    fn()
                except Exception:
                    log.exception("background task %s failed", name)
        threading.Thread(target=run, name=f"pulse-{name}", daemon=True).start()

    @asynccontextmanager
    async def lifespan(_app):
        if background:
            loop("rollup", 5, store.flush_rollups)
            loop("linker", 10, store.link_edges)
            loop("alerts", 30, lambda: (store.flush_rollups(), alerts.evaluate()))
            loop("purge", 600, lambda: store.purge(cfg))
            if cfg.get("monitors"):
                loop("monitors", int(cfg.get("monitor_interval_seconds", 30)),
                     lambda: store.insert_checks(monitors.run_all(cfg["monitors"])))
        yield
        stop.set()
        store.flush_rollups()

    app = FastAPI(title="RapidPulse", version=__version__, lifespan=lifespan,
                  description="Vendor-neutral APM & API observability on OpenTelemetry")
    app.state.store, app.state.q, app.state.ing, app.state.alerts, app.state.cfg = store, q, ing, alerts, cfg

    # ------------------------------------------------------------- security
    sec = cfg["security"]

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") or path.startswith("/api/ingest"):
            keys = sec.get("ingest_api_keys") or []
            if keys:
                given = request.headers.get("x-pulse-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                if not any(secrets.compare_digest(given, k) for k in keys):
                    return JSONResponse({"error": "invalid ingest key"}, status_code=401)
        elif sec.get("ui_users") and path != "/api/health":
            hdr = request.headers.get("authorization", "")
            ok = False
            if hdr.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    ok = user in sec["ui_users"] and secrets.compare_digest(pw, str(sec["ui_users"][user]))
                except Exception:
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="RapidPulse"'})
        return await call_next(request)

    # ---------------------------------------------------------- OTLP ingest
    async def _otlp(signal: str, request: Request, handler):
        body = await request.body()
        try:
            payload = otlp.decode(signal, body, request.headers.get("content-type"),
                                  request.headers.get("content-encoding"))
        except otlp.UnsupportedPayload as e:
            raise HTTPException(415, str(e))
        except Exception as e:
            raise HTTPException(400, f"cannot decode OTLP {signal}: {e}")
        n = handler(payload)
        if "protobuf" in (request.headers.get("content-type") or ""):
            # Empty Export*ServiceResponse is a zero-length protobuf message.
            return Response(content=b"", media_type="application/x-protobuf")
        return {"accepted": n}

    @app.post("/v1/traces")
    async def otlp_traces(request: Request):
        return await _otlp("traces", request, ing.traces)

    @app.post("/v1/logs")
    async def otlp_logs(request: Request):
        return await _otlp("logs", request, ing.logs)

    @app.post("/v1/metrics")
    async def otlp_metrics(request: Request):
        return await _otlp("metrics", request, ing.metrics)

    @app.post("/api/ingest/infra")
    def ingest_infra(payload: dict = Body(...)):
        try:
            return ing.infra(payload)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ query API
    def app_param(request: Request):
        a = request.query_params.get("app")
        return None if a in (None, "", "all") else a

    @app.get("/api/health")
    def health_ep():
        return {"status": "ok", "version": __version__}

    @app.get("/api/system")
    def system():
        return {"version": __version__, "counters": ing.counters, "db_bytes": store.size_bytes(),
                "retention": cfg["storage"], "monitors": len(cfg.get("monitors") or [])}

    @app.get("/api/meta")
    def meta():
        return q.meta()

    @app.get("/api/overview")
    def overview(request: Request):
        frm, to = time_window(request)
        return q.overview(frm, to, app_param(request))

    @app.get("/api/services")
    def services(request: Request):
        frm, to = time_window(request)
        return q.services(frm, to, app_param(request))

    @app.get("/api/services/{service}")
    def service_detail(service: str, request: Request):
        frm, to = time_window(request)
        a = app_param(request)
        endpoints = q.grouped(frm, to, ["operation"], app=a, service=service, role="entry")
        exits = q.grouped(frm, to, ["category", "peer", "operation"], app=a, service=service, role="exit")
        where, params = _where(a, frm, to)
        deps_out = store.q(f"SELECT dst AS name, dst_type AS type, SUM(cnt) AS calls, SUM(errors) AS errors, "
                           f"SUM(sum_ms)/SUM(cnt) AS avg_ms FROM edges WHERE {where} AND src = ? GROUP BY dst, dst_type",
                           params + [service])
        deps_in = store.q(f"SELECT src AS name, SUM(cnt) AS calls, SUM(errors) AS errors, SUM(sum_ms)/SUM(cnt) AS avg_ms "
                          f"FROM edges WHERE {where} AND dst = ? GROUP BY src", params + [service])
        summary = q.grouped(frm, to, ["service"], app=a, service=service, role="entry")
        return {
            "service": service, "summary": summary[0] if summary else None,
            "series": q.series(frm, to, app=a, service=service, role="entry",
                               metrics=("rpm", "error_rate", "avg_ms", "p95_ms", "p99_ms")),
            "endpoints": endpoints, "exits": exits, "deps_out": deps_out, "deps_in": deps_in,
            "errors": q.errors(frm, to, a, service, limit=30)["groups"],
            "slow_traces": q.traces(frm, to, a, service=service, sort="slowest", limit=15),
            "hosts": store.q("SELECT DISTINCT host, container, service_version FROM spans WHERE service = ? AND start_ns >= ?",
                             (service, int(frm * 1e9))),
        }

    @app.get("/api/endpoints")
    def endpoints(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        return q.grouped(frm, to, ["service", "operation"], app=app_param(request), service=p.get("service"), role="entry")

    @app.get("/api/map")
    def service_map(request: Request):
        frm, to = time_window(request)
        return q.service_map(frm, to, app_param(request))

    @app.get("/api/traces")
    def traces(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        return q.traces(frm, to, app_param(request), service=p.get("service"), operation=p.get("operation"),
                        status=p.get("status"), min_ms=p.get("min_ms"), max_ms=p.get("max_ms"), q=p.get("q"),
                        attr=p.get("attr"), sort=p.get("sort", "recent"), limit=int(p.get("limit", 100)))

    def tz_param(request: Request) -> int | None:
        try:
            return int(request.query_params.get("tz", ""))
        except ValueError:
            return None

    def pdf(data: bytes, name: str) -> Response:
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/reports/pdf")
    def report_pdf(request: Request):
        """audience=developer (technical, everything) or stakeholder (short summary)."""
        frm, to = time_window(request)
        audience = "stakeholder" if request.query_params.get("audience") == "stakeholder" else "developer"
        a = app_param(request)
        data = reports.build_report(q, store, cfg, frm, to, a, audience, tz_param(request))
        stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(to))
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (a or "all-apps"))
        return pdf(data, f"rapidpulse-{audience}-report-{safe}-{stamp}.pdf")

    @app.get("/api/traces/{trace_id}/pdf")
    def trace_pdf(trace_id: str, request: Request):
        data = reports.build_trace_report(q, trace_id, tz_param(request))
        if not data:
            raise HTTPException(404, "trace not found (it may have expired from raw retention)")
        return pdf(data, f"rapidpulse-trace-{trace_id.lower()[:32]}.pdf")

    @app.get("/api/traces/{trace_id}")
    def trace(trace_id: str):
        t = q.trace(trace_id)
        if not t:
            raise HTTPException(404, "trace not found (it may have expired from raw retention)")
        return t

    @app.get("/api/ai")
    def ai(request: Request):
        frm, to = time_window(request)
        return q.ai(frm, to, app_param(request))

    @app.get("/api/database")
    def database(request: Request):
        frm, to = time_window(request)
        return q.database(frm, to, app_param(request))

    @app.get("/api/infra/hosts")
    def hosts():
        return q.hosts()

    @app.get("/api/infra/hosts/{host}")
    def host_series(host: str, request: Request):
        frm, to = time_window(request)
        return q.host_series(host, frm, to)

    @app.get("/api/checks")
    def checks(request: Request):
        frm, to = time_window(request)
        return q.checks(frm, to)

    @app.get("/api/logs")
    def logs(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        return q.logs(frm, to, app_param(request), p.get("service"), p.get("severity"), p.get("q"), int(p.get("limit", 200)))

    @app.get("/api/metrics/names")
    def metric_names():
        return store.q("SELECT name, MAX(unit) AS unit, COUNT(*) AS points FROM metric_points WHERE ts > ? "
                       "GROUP BY name ORDER BY name", (int(time.time()) - 86400,))

    @app.get("/api/metrics/series")
    def metric_series(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        step = pick_step(frm, to)
        extra, params = "", [p.get("name", ""), frm, to]
        for k in ("host", "service"):
            if p.get(k):
                extra += f" AND {k} = ?"; params.append(p[k])
        rows = store.q(f"SELECT (ts / {step}) * {step} AS t, AVG(value) AS v FROM metric_points "
                       f"WHERE name = ? AND ts >= ? AND ts < ? {extra} GROUP BY t ORDER BY t", params)
        return {"name": p.get("name"), "step": step, "points": [[r["t"], r["v"]] for r in rows]}

    @app.get("/api/errors")
    def errors(request: Request):
        frm, to = time_window(request)
        return q.errors(frm, to, app_param(request), request.query_params.get("service"))

    @app.get("/api/analytics/series")
    def analytics_series(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        metrics = tuple(m for m in (p.get("metrics") or "rpm").split(",") if m)
        filters = {k: p.get(k) for k in ("service", "operation", "category", "role", "peer") if p.get(k)}
        step = int(p["step"]) if p.get("step") else pick_step(frm, to)
        out = q.series(frm, to, step, app=app_param(request), metrics=metrics, group_by=p.get("group_by") or None, **filters)
        if p.get("compare") == "1":
            prev = q.series(frm - (to - frm), frm, step, app=app_param(request), metrics=metrics, **filters)
            out["previous"] = prev["series"]
        return out

    @app.get("/api/analytics/report")
    def analytics_report(request: Request):
        frm, to = time_window(request)
        p = request.query_params
        filters = {k: p.get(k) for k in ("service", "operation", "category", "role", "peer") if p.get(k)}
        rep = q.report(frm, to, app_param(request), p.get("group_by", "service,operation"), **filters)
        if p.get("format") == "csv":
            buf = io.StringIO()
            cols = rep["group_by"] + ["calls", "rpm", "errors", "error_rate", "avg_ms", "p50_ms", "p95_ms", "p99_ms",
                                      "max_ms", "apdex", "tokens_in", "tokens_out", "cost", "prev_calls", "prev_p95_ms",
                                      "prev_error_rate"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rep["rows"])
            return Response(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=pulse-report.csv"})
        return rep

    # ------------------------------------------------------------- alerts
    @app.get("/api/alerts/metrics")
    def alert_metrics():
        return [{"metric": k, "description": d, "unit": u, "scope": s} for k, (d, u, s) in METRICS.items()]

    @app.get("/api/alerts/rules")
    def alert_rules():
        return alerts.rules()

    @app.post("/api/alerts/rules")
    def save_rule(rule: dict = Body(...)):
        try:
            return {"id": alerts.upsert_rule(rule)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/alerts/rules/{rid}")
    def delete_rule(rid: int):
        alerts.delete_rule(rid)
        return {"deleted": rid}

    @app.post("/api/alerts/rules/{rid}/test")
    def test_rule(rid: int):
        rule = next((r for r in alerts.rules() if r["id"] == rid), None)
        if not rule:
            raise HTTPException(404, "no such rule")
        store.flush_rollups()
        return {"values": [{"scope": k, "value": v} for k, v in alerts.measure(rule)]}

    @app.get("/api/alerts/events")
    def alert_events(request: Request):
        return alerts.events(request.query_params.get("state"))

    @app.post("/api/alerts/events/{eid}/ack")
    def ack(eid: int):
        alerts.acknowledge(eid)
        return {"acknowledged": eid}

    @app.post("/api/alerts/evaluate")
    def evaluate_now():
        store.flush_rollups()
        return alerts.evaluate()

    # ----------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        # Stamp asset URLs with their mtime so an upgrade is never served from a stale browser cache.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for asset in ("styles.css", "app.js"):
            stamp = int((STATIC / asset).stat().st_mtime)
            html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={stamp}\"")
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg["server"]["host"], port=int(cfg["server"]["port"]), log_level="warning")


if __name__ == "__main__":
    main()
