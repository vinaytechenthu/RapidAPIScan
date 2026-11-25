"""End-to-end tests: OTLP in (JSON + protobuf) -> traces, rollups, map, alerts, privacy, auth.

They run against the neutral demo topology (examples/demo-shop); one test checks the
Lumina example topology still produces its full call path.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apm.config import DEFAULTS, _merge  # noqa: E402
from apm.semantics import make_redactor, sql_fingerprint  # noqa: E402
from apm.server import create_app  # noqa: E402

spec = importlib.util.spec_from_file_location("simulate", ROOT / "simulator" / "simulate.py")
sim_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim_mod)

DEMO = sim_mod.load_topology(ROOT / "examples" / "demo-shop" / "topology.yaml")
LUMINA = sim_mod.load_topology(ROOT / "examples" / "lumina" / "topology.yaml")


def make_client(tmp_path, **overrides):
    cfg = _merge(DEFAULTS, {"storage": {"path": str(tmp_path / "t.db")}, **overrides})
    app = create_app(cfg, background=False)
    return TestClient(app), app


def flow(topo, name):
    return next(f for f in topo["flows"] if f["name"] == name)


def make_traces(n, topo=DEMO, flow_name=None, incident=None, t0=None):
    s = sim_mod.Sim(topo)
    s.set_incident(incident)
    base = t0 or time.time() - 120
    f = flow(topo, flow_name) if flow_name else None
    return s, [s.trace(int((base + i * 0.5) * 1e9), f) for i in range(n)]


def test_json_ingest_and_trace_analysis(tmp_path):
    c, _ = make_client(tmp_path)
    sim, traces = make_traces(5, flow_name="assistant")
    r = c.post("/v1/traces", json=sim.otlp_traces(traces))
    assert r.status_code == 200 and r.json()["accepted"] >= 5 * 8
    t = c.get(f"/api/traces/{traces[0].trace_id}").json()
    assert t["spans"][0]["depth"] == 0 and t["spans"][0]["service"] == "storefront"
    assert {"storefront", "assistant-api", "tools-mcp", "catalog-api"} <= set(t["services"])
    comps = {b["component"] for b in t["breakdown"]}
    assert "LLM (openai)" in comps
    assert sum(b["self_ms"] for b in t["breakdown"]) <= t["duration_ms"] * 1.01
    assert t["ai"]["llm_calls"] == 2 and t["ai"]["tokens_in"] > 0 and t["ai"]["cost"] > 0
    assert any(s["critical"] for s in t["spans"])


def test_root_cause_is_deepest_error(tmp_path):
    c, _ = make_client(tmp_path)
    sim = sim_mod.Sim(DEMO)
    tr = sim_mod.Trace(time.time_ns())
    root = tr.span("storefront", "GET /x", 0, 100, kind=2, attrs={"http.request.method": "GET", "http.route": "/x",
                                                                  "http.response.status_code": 500}, error=("E", "bubbled"))
    mid = tr.span("catalog-api", "POST /load", 5, 90, parent=root, kind=2, attrs={"http.request.method": "POST",
                  "http.route": "/load", "http.response.status_code": 500}, error=("E", "bubbled"))
    tr.span("catalog-api", "mssql.query", 10, 80, parent=mid, kind=3,
            attrs={"db.system": "mssql", "db.namespace": "shopdb", "db.query.text": "SELECT * FROM t WHERE id = 42"},
            error=("SqlException", "deadlock victim"))
    c.post("/v1/traces", json=sim.otlp_traces([tr]))
    t = c.get(f"/api/traces/{tr.trace_id}").json()
    assert t["root_cause"]["error_type"] == "SqlException"
    assert t["bottleneck"]["name"] == "mssql.query"
    db = [s for s in t["spans"] if s["category"] == "db"][0]
    assert db["db_statement"] == "SELECT * FROM t WHERE id = ?"   # literal removed
    assert db["peer"] == "SQL Server (shopdb)"


def _proto_llm_span(attrs: dict, trace_hex: str):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import Span
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(KeyValue(key="service.name", value=AnyValue(string_value="ai-api")))
    rs.resource.attributes.append(KeyValue(key="service.namespace", value=AnyValue(string_value="proto-app")))
    sp = rs.scope_spans.add().spans.add()
    sp.trace_id, sp.span_id = bytes.fromhex(trace_hex), bytes.fromhex("b7ad6b7169203331")
    sp.name, sp.kind = "llm", Span.SPAN_KIND_INTERNAL
    sp.start_time_unix_nano = time.time_ns() - 2_000_000_000
    sp.end_time_unix_nano = sp.start_time_unix_nano + 1_500_000_000
    for k, v in attrs.items():
        val = AnyValue(int_value=v) if isinstance(v, int) else AnyValue(double_value=v) if isinstance(v, float) else AnyValue(string_value=v)
        sp.attributes.append(KeyValue(key=k, value=val))
    return req.SerializeToString()


def test_protobuf_ingest_openinference(tmp_path):
    c, _ = make_client(tmp_path)
    tid = "0af7651916cd43dd8448eb211c80319c"
    body = _proto_llm_span({"openinference.span.kind": "LLM", "llm.model_name": "anthropic.claude-sonnet-4-5",
                            "llm.token_count.prompt": 1000, "llm.token_count.completion": 200}, tid)
    assert c.post("/v1/traces", content=body, headers={"Content-Type": "application/x-protobuf"}).status_code == 200
    s = c.get(f"/api/traces/{tid}").json()["spans"][0]
    assert (s["category"], s["llm_model"], s["tokens_in"], s["tokens_out"]) == ("llm", "anthropic.claude-sonnet-4-5", 1000, 200)
    assert s["cost_usd"] == pytest.approx((1000 * 3 + 200 * 15) / 1e6)


def test_attribute_aliases_for_custom_names(tmp_path):
    c, _ = make_client(tmp_path, attribute_aliases={"llm_model": ["myapp.model"], "llm_input_tokens": ["myapp.tin"],
                                                    "llm_output_tokens": ["myapp.tout"], "llm_cost": ["myapp.cost"]})
    tid = "1af7651916cd43dd8448eb211c80319c"
    body = _proto_llm_span({"openinference.span.kind": "LLM", "myapp.model": "in-house-model",
                            "myapp.tin": 50, "myapp.tout": 7, "myapp.cost": 0.42}, tid)
    c.post("/v1/traces", content=body, headers={"Content-Type": "application/x-protobuf"})
    s = c.get(f"/api/traces/{tid}").json()["spans"][0]
    assert (s["llm_model"], s["tokens_in"], s["tokens_out"], s["cost_usd"]) == ("in-house-model", 50, 7, 0.42)


def test_rollups_percentiles_map_and_reports(tmp_path):
    c, app = make_client(tmp_path)
    sim, traces = make_traces(60)
    c.post("/v1/traces", json=sim.otlp_traces(traces))
    app.state.store.flush_rollups()
    app.state.store.link_edges(settle_seconds=-1)
    svcs = {s["service"]: s for s in c.get("/api/services?range=1h").json()}
    assert set(DEMO["services"]) <= set(svcs)
    w = svcs["storefront"]
    assert w["calls"] == 60 and w["p50_ms"] <= w["p95_ms"] <= w["p99_ms"] <= w["max_ms"] * 1.3
    assert {"demo-shop"} == set(c.get("/api/meta").json()["apps"])
    m = c.get("/api/map?range=1h").json()
    edges = {(e["source"], e["target"]) for e in m["edges"]}
    for e in [("storefront", "catalog-api"), ("storefront", "orders-api"), ("storefront", "assistant-api"),
              ("orders-api", "catalog-api"), ("orders-api", "Payment gateway"), ("catalog-api", "SQL Server (shopdb)"),
              ("assistant-api", "LLM (openai)"), ("assistant-api", "tools-mcp"), ("tools-mcp", "catalog-api")]:
        assert e in edges, e
    types = {n["id"]: n["type"] for n in m["nodes"]}
    assert types["SQL Server (shopdb)"] == "database" and types["LLM (openai)"] == "llm" and types["Payment gateway"] == "external"
    ai = c.get("/api/ai?range=1h").json()
    assert ai["totals"]["calls"] > 0 and ai["totals"]["cost"] > 0 and {r["operation"] for r in ai["models"]} == {"gpt-4o", "gpt-4o-mini"}
    db = c.get("/api/database?range=1h").json()
    assert db["databases"] and all("'" not in q["operation"] for q in db["queries"])   # no literal survives
    s = c.get("/api/analytics/series?range=1h&metrics=p95_ms,rpm&group_by=service&role=entry").json()
    assert s["series"] and len(s["series"][0]["points"]) == len(s["times"])
    assert c.get("/api/analytics/report?range=1h&format=csv").text.startswith("service,operation,calls")


def test_example_topology_lumina_end_to_end(tmp_path):
    """Applications are data: the Lumina example produces its full chain with no code of its own."""
    c, app = make_client(tmp_path)
    sim, traces = make_traces(10, topo=LUMINA, flow_name="chat")
    c.post("/v1/traces", json=sim.otlp_traces(traces))
    app.state.store.flush_rollups()
    app.state.store.link_edges(settle_seconds=-1)
    edges = {(e["source"], e["target"]) for e in c.get("/api/map?range=1h").json()["edges"]}
    for e in [("web-app", "lumina-api"), ("lumina-api", "fabric-api"), ("fabric-api", "cube-api"),
              ("cube-api", "SQL Server (EDM_DEMO)"), ("lumina-api", "LLM (aws.bedrock)"), ("fabric-api", "lumina-mcp")]:
        assert e in edges, e


def test_alert_fires_and_resolves(tmp_path):
    c, app = make_client(tmp_path)
    sim, traces = make_traces(40, flow_name="checkout", incident="payment_errors", t0=time.time() - 60)
    c.post("/v1/traces", json=sim.otlp_traces(traces))
    app.state.store.flush_rollups()
    rule = {"name": "orders errors", "metric": "service.error_rate", "scope": {"service": "orders-api"},
            "op": ">", "threshold": 1, "window_min": 5, "severity": "critical"}
    rid = c.post("/api/alerts/rules", json=rule).json()["id"]
    app.state.alerts.evaluate()
    firing = [e for e in c.get("/api/alerts/events?state=firing").json() if e["rule_id"] == rid]
    assert len(firing) == 1 and firing[0]["value"] > 1
    c.post("/api/alerts/rules", json={**rule, "id": rid, "threshold": 99})
    app.state.alerts.evaluate()
    assert not [e for e in c.get("/api/alerts/events?state=firing").json() if e["rule_id"] == rid]


def test_per_service_latency_threshold(tmp_path):
    _, app = make_client(tmp_path, services={"assistant-api": {"slow_request_ms": 10**9}})
    rule = {"metric": "service.p95_ms", "scope": {}, "threshold": 1}
    assert app.state.alerts.threshold_for(rule, "assistant-api") == 10**9
    assert app.state.alerts.threshold_for(rule, "catalog-api") == 1


def test_infra_and_checks(tmp_path):
    c, app = make_client(tmp_path)
    sim = sim_mod.Sim(DEMO)
    sim.set_incident("catalog_down")
    for p in sim.infra_payloads(int(time.time())):
        assert c.post("/api/ingest/infra", json=p).status_code == 200
    hosts = {h["host"]: h for h in c.get("/api/infra/hosts").json()}
    assert hosts["DB-01"]["os"] == "windows" and hosts["APP-01"]["containers"]
    cat = [k for k in hosts["APP-01"]["containers"] if k["container"] == "catalog-api"][0]
    assert cat["status"] == "exited"
    checks = {k["name"]: k for k in c.get("/api/checks?range=1h").json()}
    assert checks["catalog-api health"]["last_ok"] is False and checks["orders-api health"]["last_ok"] is True
    rule = [r for r in app.state.alerts.rules() if r["metric"] == "container.down"][0]
    assert ("APP-01 / catalog-api", 1.0) in app.state.alerts.measure(rule)


def test_service_down_breaks_calls_without_server_span(tmp_path):
    sim, traces = make_traces(5, flow_name="browse", incident="catalog_down")
    for tr in traces:
        assert "catalog-api" not in tr.spans             # nothing reached the service
        client = [s for s in tr.spans["storefront"] if s["kind"] == 3][0]
        assert client["status"]["code"] == 2


def test_privacy_redaction():
    red = make_redactor(["authorization", "token"])
    out = red({"http.request.header.authorization": "Bearer x", "url.full": "https://h/p?id=7&code=abc",
               "url.query": "a=1", "app.run_id": "r1"})
    assert out["http.request.header.authorization"] == "[redacted]"
    # query strings: only sensitive parameters are masked by default; "redact" hides the whole query
    assert out["url.full"] == "https://h/p?id=7&code=abc" and out["url.query"] == "a=1"
    assert red({"url.query": "q=1&access_token=zz"})["url.query"] == "q=1&access_token=[redacted]"
    strict = make_redactor([], {"query_strings": "redact"})
    assert strict({"url.full": "https://h/p?id=7"})["url.full"] == "https://h/p?[redacted]"
    assert out["app.run_id"] == "r1"
    # token *counts* are not secrets
    assert red({"gen_ai.usage.input_tokens": 12})["gen_ai.usage.input_tokens"] == 12
    assert sql_fingerprint("SELECT a FROM t WHERE n = N'Bob' AND x IN (1, 2, 3)") == "SELECT a FROM t WHERE n = ? AND x IN (?…)"


def test_ingest_key_enforced(tmp_path):
    c, _ = make_client(tmp_path, security={**copy.deepcopy(DEFAULTS["security"]), "ingest_api_keys": ["s3cret"]})
    sim, traces = make_traces(1)
    body = sim.otlp_traces(traces)
    assert c.post("/v1/traces", json=body).status_code == 401
    assert c.post("/v1/traces", json=body, headers={"x-pulse-key": "s3cret"}).status_code == 200
    assert c.post("/v1/traces", json=body, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/health").status_code == 200


def test_local_settings_override_main_config(tmp_path):
    from apm.config import load_config
    (tmp_path / "pulse.yaml").write_text("thresholds: {slow_query_ms: 500}\nsecurity: {sanitize_sql: true}\n", encoding="utf-8")
    (tmp_path / "pulse.local.yaml").write_text("security: {ingest_api_keys: [k1]}\nserver: {port: 4999}\n", encoding="utf-8")
    cfg = load_config(str(tmp_path / "pulse.yaml"))
    assert cfg["security"]["ingest_api_keys"] == ["k1"] and cfg["security"]["sanitize_sql"] is True
    assert cfg["server"]["port"] == 4999 and cfg["thresholds"]["slow_query_ms"] == 500


def test_ui_served_with_cache_busting(tmp_path):
    c, _ = make_client(tmp_path)
    html = c.get("/").text
    assert "/static/app.js?v=" in html and "RapidPulse" in html


def test_core_has_no_application_names():
    """The platform code must stay application-neutral; applications live in examples/."""
    banned = ("lumina", "bedrock", "fabric", "medm", "edm_", "cube-api")
    for folder in ("apm", "agent", "instrumentation", "deploy", "config", "integrations"):
        for p in (ROOT / folder).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".js", ".html", ".css", ".yaml", ".ps1", ".sh", ".ts") \
                    and "vendor" not in p.parts:
                text = p.read_text(encoding="utf-8", errors="ignore").lower()
                for word in banned:
                    assert word not in text, f"{p.relative_to(ROOT)} mentions {word!r}"


def test_pdf_reports_and_custom_window(tmp_path):
    c, app = make_client(tmp_path)
    sim, traces = make_traces(40, incident=None)
    _, bad = make_traces(10, incident="payment_outage" if any(i["name"] == "payment_outage" for i in DEMO.get("incidents", [])) else None)
    c.post("/v1/traces", json=sim.otlp_traces(traces + bad))
    now = int(time.time())
    for aud in ("developer", "stakeholder"):
        r = c.get(f"/api/reports/pdf?range=1h&audience={aud}&tz=-330")
        assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
        assert f"rapidpulse-{aud}-report" in r.headers["content-disposition"]
    dev = c.get("/api/reports/pdf?range=1h&audience=developer").content
    brief = c.get("/api/reports/pdf?range=1h&audience=stakeholder").content
    assert len(dev) > len(brief)                       # the developer report carries much more detail
    # a custom window (from/to) is honoured, including a 5-minute preset
    assert c.get(f"/api/reports/pdf?from={now - 600}&to={now}&audience=stakeholder").status_code == 200
    assert c.get("/api/services?range=5m").status_code == 200
    tid = traces[0].trace_id if hasattr(traces[0], "trace_id") else c.get("/api/traces?range=1h&limit=1").json()[0]["trace_id"]
    r = c.get(f"/api/traces/{tid}/pdf?tz=0")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and tid[:16] in r.headers["content-disposition"]
    assert c.get("/api/traces/0123456789abcdef0123456789abcdef/pdf").status_code == 404


# ----------------------------------------------------------------- LLM cost
def test_llm_pricing_current_models_and_cache():
    from apm.config import DEFAULTS, merge_pricing
    from apm.semantics import llm_cost, normalise_model
    P = DEFAULTS["llm_pricing"]
    assert normalise_model("us.anthropic.claude-opus-5-5-v1:0") == "claude-opus-5-5"
    assert normalise_model("arn:aws:x:us-east-1:1:inference-profile/global.anthropic.claude-opus-5-5") == "claude-opus-5-5"
    assert normalise_model("anthropic.claude-3-5-sonnet-20240620-v1:0") == "claude-sonnet-3-5"
    assert normalise_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    # Opus 5.5: $4 in / $20 out per 1M tokens
    cost, tin = llm_cost("us.anthropic.claude-opus-5-5-v1:0", 10_000, 2_000, 0, 0, P)
    assert cost == pytest.approx((10_000 * 4 + 2_000 * 20) / 1e6) and tin == 10_000
    # cache tokens reported separately (input < cached): reads $0.20, writes 1.25 x $4
    cost, tin = llm_cost("claude-opus-5-5", 1_000, 500, 50_000, 4_000, P)
    assert tin == 55_000
    assert cost == pytest.approx((1_000 * 4 + 50_000 * 0.20 + 4_000 * 5.0 + 500 * 20) / 1e6)
    # cache tokens included in the input count
    cost, tin = llm_cost("claude-opus-5-5", 60_000, 0, 50_000, 0, P, "true")
    assert tin == 60_000 and cost == pytest.approx((10_000 * 4 + 50_000 * 0.20) / 1e6)
    # most specific row wins regardless of order; older names and long-prompt tiers
    assert llm_cost("claude-opus-5", 1_000_000, 0, 0, 0, P)[0] == pytest.approx(5.0)
    assert llm_cost("claude-opus-4-1", 1_000_000, 0, 0, 0, P)[0] == pytest.approx(15.0)
    assert llm_cost("claude-3-haiku-20240307", 1_000_000, 0, 0, 0, P)[0] == pytest.approx(0.25)
    assert llm_cost("claude-haiku-5-5", 50_000, 0, 0, 0, P)[0] == pytest.approx(50_000 * 0.10 / 1e6)
    assert llm_cost("claude-haiku-5-5", 200_000, 0, 0, 0, P)[0] == pytest.approx(200_000 * 0.50 / 1e6)
    # unknown model: not priced (None); a legacy catch-all row from an old config is ignored
    merged = merge_pricing(P, [{"match": "", "input": 1, "output": 3}, {"match": "claude-opus-5-5", "input": 3.6, "output": 18}])
    assert llm_cost("some-new-model", 1000, 1000, 0, 0, merged)[0] is None
    assert llm_cost("claude-opus-5-5", 1_000_000, 0, 0, 0, merged)[0] == pytest.approx(3.6)


def _otlp(spans, service="svc"):
    def kv(k, v):
        if isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, list):
            val = {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
        else:
            val = {"stringValue": str(v)}
        return {"key": k, "value": val}
    now = time.time_ns()
    out = []
    for i, sp in enumerate(spans):
        out.append({"traceId": sp["trace"], "spanId": sp["id"], "parentSpanId": sp.get("parent", ""), "name": sp["name"],
                    "kind": sp.get("kind", 1), "startTimeUnixNano": str(now - 3_000_000_000 + i * 1000),
                    "endTimeUnixNano": str(now - 1_000_000_000 - i * 1000),
                    "attributes": [kv(k, v) for k, v in sp.get("attrs", {}).items()]})
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.name", service), kv("service.namespace", "app1")]},
                               "scopeSpans": [{"scope": {"name": "t"}, "spans": out}]}]}


def test_nested_llm_spans_counted_once(tmp_path):
    c, app = make_client(tmp_path)
    tid = "2af7651916cd43dd8448eb211c80319c"
    usage = {"gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100}
    outer = {"trace": tid, "id": "a000000000000001", "name": "llm", "parent": "b000000000000001",
             "attrs": {"openinference.span.kind": "LLM", "llm.model_name": "claude-opus-5-5",
                       "llm.token_count.prompt": 1000, "llm.token_count.completion": 100}}
    inner = {"trace": tid, "id": "a000000000000002", "parent": "a000000000000001", "name": "chat claude-opus-5-5", "kind": 3,
             "attrs": {"gen_ai.system": "aws", "gen_ai.request.model": "us.anthropic.claude-opus-5-5-v1:0", **usage}}
    other = {"trace": tid, "id": "a000000000000003", "parent": "b000000000000001", "name": "second call", "kind": 3,
             "attrs": {"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o", **usage}}
    # the inner span is exported first (it ends first); its parent arrives in a later batch
    c.post("/v1/traces", json=_otlp([inner, other]))
    c.post("/v1/traces", json=_otlp([outer]))
    t = c.get(f"/api/traces/{tid}").json()
    assert t["ai"]["llm_calls"] == 2 and t["ai"]["tokens_in"] == 2000
    assert t["ai"]["cost"] == pytest.approx((1000 * 4 + 100 * 20 + 1000 * 2.5 + 100 * 10) / 1e6)
    o = next(s for s in t["spans"] if s["span_id"] == "a000000000000001")
    assert o["category"] == "internal" and o["attributes"]["rapidpulse.llm_counted_in"] == "a000000000000002"
    # both in one batch: the inner span is kept
    tid2 = "5af7651916cd43dd8448eb211c80319c"
    c.post("/v1/traces", json=_otlp([{**outer, "trace": tid2}, {**inner, "trace": tid2}]))
    t2 = c.get(f"/api/traces/{tid2}").json()
    assert t2["ai"]["llm_calls"] == 1
    assert [s["span_id"] for s in t2["spans"] if s["category"] == "llm"] == ["a000000000000002"]


# ----------------------------------------------------------------- request capture
def test_request_headers_and_payload_masked_and_shown(tmp_path):
    c, app = make_client(tmp_path)
    tid = "3af7651916cd43dd8448eb211c80319c"
    body = '{"question": "sales by region", "password": "hunter2", "max_tokens": 5, "auth": {"api_key": "k1"}}'
    srv = {"trace": tid, "id": "c000000000000001", "name": "POST /api/chat", "kind": 2,
           "attrs": {"http.request.method": "POST", "http.route": "/api/chat", "url.path": "/api/chat",
                     "url.query": "lang=en&access_token=zz", "server.address": "api.local", "server.port": 8600,
                     "http.response.status_code": 200, "http.request.header.content-type": ["application/json"],
                     "http.request.header.authorization": ["Bearer abc"], "http.request.header.x-api-key": ["k2"],
                     "http.request.header.user-agent": ["curl/8"], "http.request.body": body,
                     "http.request.body.size": len(body)}}
    c.post("/v1/traces", json=_otlp([srv], service="web-api"))
    t = c.get(f"/api/traces/{tid}").json()
    r = t["request"]
    assert r["method"] == "POST" and r["url"] == "http://api.local:8600/api/chat?lang=en&access_token=[redacted]"
    assert r["headers"]["authorization"] == "[redacted]" and r["headers"]["x-api-key"] == "[redacted]"
    assert r["headers"]["user-agent"] == "curl/8" and r["content_type"] == "application/json"
    assert '"password": "[redacted]"' in r["body"] and '"api_key": "[redacted]"' in r["body"]
    assert '"question": "sales by region"' in r["body"] and '"max_tokens": 5' in r["body"]
    assert "hunter2" not in str(t) and "Bearer abc" not in str(t)
    assert t["spans"][0]["request"]["body"] == r["body"]
    # searchable from the Traces page, and printed in the trace PDF
    assert [x["trace_id"] for x in c.get("/api/traces?range=1h&attr=sales by region").json()] == [tid]
    pdf = c.get(f"/api/traces/{tid}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_request_body_limit_and_storage_cap(tmp_path):
    c, _ = make_client(tmp_path, security={**copy.deepcopy(DEFAULTS["security"]), "max_request_body_bytes": 100})
    tid = "4af7651916cd43dd8448eb211c80319c"
    srv = {"trace": tid, "id": "d000000000000001", "name": "POST /x", "kind": 2,
           "attrs": {"http.request.method": "POST", "http.route": "/x", "http.response.status_code": 200,
                     "http.request.body": "x" * 5000, "huge.attr": "y" * 100_000}}
    c.post("/v1/traces", json=_otlp([srv]))
    s = c.get(f"/api/traces/{tid}").json()["spans"][0]           # attributes are still valid JSON
    assert s["request"]["body"].startswith("x" * 100) and "truncated at 100" in s["request"]["body"]
    assert len(s["attributes"]["huge.attr"]) < 100_000


def _capture_module():
    spec = importlib.util.spec_from_file_location("rp_capture_test", ROOT / "integrations" / "python-docker" / "rapidpulse_capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.meta_path[:] = [f for f in sys.meta_path if type(f).__name__ != "_AfterImport"]   # keep the test process clean
    return mod


def test_capture_module_asgi_and_wsgi(monkeypatch):
    import asyncio
    import io
    cap = _capture_module()
    got = []
    monkeypatch.setattr(cap, "_record", lambda buf, total, ctype, span=None: got.append((cap.mask(bytes(buf).decode()), total, ctype)))
    assert cap.mask('{"user": "a", "password": "b"}') == '{"user": "a", "password": "[redacted]"}'
    assert cap.mask("grant_type=x&client_secret=y") == "grant_type=x&client_secret=[redacted]"

    class FakeMiddleware:            # stands in for opentelemetry.instrumentation.asgi.OpenTelemetryMiddleware
        async def __call__(self, scope, receive, send):
            while (await receive()).get("more_body"):
                pass
    cap._patch_asgi(type("m", (), {"OpenTelemetryMiddleware": FakeMiddleware}))
    msgs = iter([{"type": "http.request", "body": b'{"a": 1, ', "more_body": True},
                 {"type": "http.request", "body": b'"token": "t"}', "more_body": False}])

    async def receive():
        return next(msgs)
    scope = {"type": "http", "headers": [(b"content-type", b"application/json")]}
    asyncio.run(FakeMiddleware()(scope, receive, None))
    assert got[-1] == ('{"a": 1, "token": "[redacted]"}', 22, "application/json")

    class FakeWsgiApp:
        def __call__(self, environ, start_response):
            return [environ["wsgi.input"].read()]
    cap._patch_wsgi_call(FakeWsgiApp)
    data = b"name=x&pin=1234"
    out = FakeWsgiApp()({"wsgi.input": io.BytesIO(data), "CONTENT_LENGTH": str(len(data)),
                         "CONTENT_TYPE": "application/x-www-form-urlencoded"}, None)
    assert out == [data]                                     # the application gets the same bytes
    assert got[-1][0] == "name=x&pin=[redacted]" and got[-1][1] == len(data)

    class ReadIntoApp:                # werkzeug reads with readinto()
        def __call__(self, environ, start_response):
            b = bytearray(64)
            n = environ["wsgi.input"].readinto(b)
            return [bytes(b[:n])]
    cap._patch_wsgi_call(ReadIntoApp)
    out = ReadIntoApp()({"wsgi.input": io.BytesIO(b"secret=1&a=2"), "CONTENT_LENGTH": "12",
                         "CONTENT_TYPE": "application/x-www-form-urlencoded"}, None)
    assert out == [b"secret=1&a=2"] and got[-1][0] == "secret=[redacted]&a=2"


def test_request_url_from_host_header_and_payload_from_child_span(tmp_path):
    """Behind Docker NAT server.address is the container address: show the address the client called.
    Some OpenTelemetry versions put the payload on the internal `http receive` child span."""
    c, _ = make_client(tmp_path)
    tid = "6af7651916cd43dd8448eb211c80319c"
    srv = {"trace": tid, "id": "e000000000000001", "name": "POST /api/v1/chatbot/{id}/messages", "kind": 2,
           "attrs": {"http.request.method": "POST", "http.route": "/api/v1/chatbot/{id}/messages",
                     "url.path": "/api/v1/chatbot/cnv_1/messages", "server.address": "172.26.0.3", "server.port": 8000,
                     "http.response.status_code": 202, "http.request.header.host": ["10.0.0.9:8600"],
                     "http.request.header.content_type": ["application/json"]}}
    rcv = {"trace": tid, "id": "e000000000000002", "parent": "e000000000000001", "name": "POST /api/v1/chatbot/{id}/messages http receive",
           "attrs": {"asgi.event.type": "http.request", "http.request.body": '{"content": "give me 10 isin"}',
                     "http.request.body.size": 31}}
    c.post("/v1/traces", json=_otlp([srv, rcv], service="chat-api"))
    r = c.get(f"/api/traces/{tid}").json()["request"]
    assert r["url"] == "http://10.0.0.9:8600/api/v1/chatbot/cnv_1/messages"
    assert r["body"] == '{"content": "give me 10 isin"}' and r["body_size"] == 31 and r["status"] == 202


def test_current_model_follows_latest_llm_call(tmp_path):
    from apm.semantics import model_label
    assert model_label("us.anthropic.claude-opus-5-5-v1:0") == "Opus 5.5"
    assert model_label("arn:aws:x:eu-west-1:1:inference-profile/eu.anthropic.claude-sonnet-5-5-v1:0") == "Sonnet 5.5"
    assert model_label("claude-3-5-sonnet-20241022") == "Sonnet 3.5"
    assert model_label("claude-fable-5-2") == "Fable 5.2" and model_label("gpt-4o") == "gpt-4o"
    c, app = make_client(tmp_path)
    assert c.get("/api/overview").json()["current_model"] is None

    def call(tid, model):
        sp = {"trace": tid, "id": "d000000000000001", "name": "chat", "kind": 3,
              "attrs": {"gen_ai.system": "aws", "gen_ai.request.model": model,
                        "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5}}
        c.post("/v1/traces", json=_otlp([sp], service="chat-api"))
        time.sleep(0.01)
    call("6af7651916cd43dd8448eb211c80319c", "us.anthropic.claude-sonnet-5-5-v1:0")
    call("7af7651916cd43dd8448eb211c80319c", "us.anthropic.claude-opus-5-5-v1:0")
    cm = c.get("/api/overview").json()["current_model"]
    assert cm["label"] == "Opus 5.5" and cm["model"] == "us.anthropic.claude-opus-5-5-v1:0" and cm["service"] == "chat-api"
    assert cm["others"] == [{"label": "Sonnet 5.5", "calls": 1}]
    call("8af7651916cd43dd8448eb211c80319c", "claude-haiku-5-5")     # the model changes: the card follows
    assert c.get("/api/ai").json()["current_model"]["label"] == "Haiku 5.5"
    assert c.get("/api/ai?app=other-app").json()["current_model"] is None
