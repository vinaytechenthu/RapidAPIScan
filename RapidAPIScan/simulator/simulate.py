"""Topology-driven traffic simulator for RapidPulse.

Describe ANY application — its hosts, services, request flows (call trees across
HTTP, databases, LLMs and MCP tools) and incident scenarios — in a YAML file, and
this emits *real* OTLP/HTTP traces + logs and RapidPulse agent infrastructure payloads
for it. Use it to demo, to validate dashboards and alert rules before the real
services are instrumented, or as a reference for what good instrumentation emits.

    python simulator/simulate.py                                     # default demo topology
    python simulator/simulate.py --topology examples/<example>/topology.yaml --backfill-minutes 120
    python simulator/simulate.py --topology my-app.yaml --scenario db_slow --rate 2

Topology format: see examples/demo-shop/topology.yaml (fully commented).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOPOLOGY = ROOT / "examples" / "demo-shop" / "topology.yaml"

DEFAULT_ERRORS = {
    "http": ("HTTPError", "upstream returned 500"),
    "db": ("DatabaseError", "query failed: timeout expired"),
    "llm": ("RateLimitError", "model rate limit exceeded"),
    "mcp": ("TimeoutError", "tool call exceeded its time budget"),
    "work": ("RuntimeError", "processing failed"),
    "entry": ("RuntimeError", "request failed"),
}


def rid(n=16):
    return os.urandom(n).hex()


def av(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def kv(d):
    return [{"key": k, "value": av(v)} for k, v in d.items() if v is not None and v != ""]


def load_topology(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        topo = yaml.safe_load(fh)
    for key in ("application", "services", "flows"):
        if key not in topo:
            raise ValueError(f"topology {path} is missing '{key}'")
    topo.setdefault("environment", "demo")
    topo.setdefault("hosts", {})
    topo.setdefault("incidents", [])
    topo.setdefault("checks", [])
    topo.setdefault("users", [f"user{i:02d}@example.com" for i in range(1, 25)])
    return topo


class Trace:
    def __init__(self, t0_ns: int):
        self.trace_id = rid(16)
        self.t0 = t0_ns
        self.spans: dict[str, list] = {}
        self.logs: dict[str, list] = {}

    def span(self, svc, name, start_ms, dur_ms, parent=None, kind=1, attrs=None, error=None, sid=None):
        sid = sid or rid(8)
        st = self.t0 + int(start_ms * 1e6)
        en = st + int(max(0.01, dur_ms) * 1e6)
        sp = {"traceId": self.trace_id, "spanId": sid, "parentSpanId": parent or "", "name": name, "kind": kind,
              "startTimeUnixNano": str(st), "endTimeUnixNano": str(en), "attributes": kv(attrs or {}),
              "status": {"code": 2, "message": error[1]} if error else {"code": 1}}
        if error:
            sp["events"] = [{"name": "exception", "timeUnixNano": str(en),
                             "attributes": kv({"exception.type": error[0], "exception.message": error[1]})}]
        self.spans.setdefault(svc, []).append(sp)
        return sid

    def log(self, svc, span_id, at_ms, sev, body):
        self.logs.setdefault(svc, []).append({
            "timeUnixNano": str(self.t0 + int(at_ms * 1e6)), "severityNumber": {"INFO": 9, "WARN": 13, "ERROR": 17}[sev],
            "severityText": sev, "body": {"stringValue": body}, "traceId": self.trace_id, "spanId": span_id})


class Incident:
    def __init__(self, spec: dict | None):
        spec = spec or {}
        self.name = spec.get("name")
        self.latency = spec.get("latency", {}) or {}        # node id -> multiplier
        self.errors = spec.get("errors", {}) or {}          # node id -> error probability
        self.down = set(spec.get("service_down", []) if isinstance(spec.get("service_down"), list)
                        else [spec["service_down"]] if spec.get("service_down") else [])
        self.host_cpu = spec.get("host_cpu", {}) or {}      # host -> cpu %


class Sim:
    def __init__(self, topo: dict, url: str = "http://127.0.0.1:4400", key: str | None = None):
        self.t = topo
        self.url = url.rstrip("/")
        self.key = key
        self.incident = Incident(None)
        self.restarts = {s.get("container"): 0 for s in topo["services"].values() if s.get("container")}
        self.weights = [f.get("weight", 1.0) for f in topo["flows"]]

    # ------------------------------------------------------------- incidents
    def set_incident(self, name: str | None):
        spec = next((i for i in self.t["incidents"] if i["name"] == name), None) if name else None
        self.incident = Incident(spec)

    @property
    def incident_names(self) -> list[str]:
        return [i["name"] for i in self.t["incidents"]]

    # ---------------------------------------------------------------- traces
    def resource(self, svc: str) -> dict:
        s = self.t["services"][svc]
        host = s.get("host", "")
        return {"attributes": kv({
            "service.name": svc, "service.namespace": self.t["application"], "service.version": s.get("version", "1.0.0"),
            "deployment.environment.name": self.t["environment"], "host.name": host, "container.name": s.get("container"),
            "telemetry.sdk.language": s.get("language"), "os.type": (self.t["hosts"].get(host) or {}).get("os")})}

    @staticmethod
    def dur(median_ms: float) -> float:
        return random.lognormvariate(math.log(max(0.05, median_ms)), 0.35)

    def trace(self, t0_ns: int, flow: dict | None = None) -> Trace:
        flow = flow or random.choices(self.t["flows"], weights=self.weights)[0]
        tr = Trace(t0_ns)
        root = flow["root"]
        user = random.choice(self.t["users"])
        ctx = {"enduser.id": user, "session.id": rid(6)}
        self._serve(tr, root, root["service"], "", 0.0, ctx)
        return tr

    def _node_params(self, n: dict) -> tuple[float, bool]:
        nid = n.get("id")
        own = self.dur(n.get("ms", 5)) * float(self.incident.latency.get(nid, 1.0) if nid else 1.0)
        p = self.incident.errors.get(nid, n.get("error_rate", 0.002)) if nid else n.get("error_rate", 0.002)
        return own, random.random() < float(p)

    def _err(self, n: dict) -> tuple[str, str]:
        e = n.get("error") or {}
        d = DEFAULT_ERRORS.get(n.get("type", "work"), DEFAULT_ERRORS["work"])
        return e.get("type", d[0]), e.get("message", d[1])

    def _body(self, tr, n, svc, sid, start, own, own_fail) -> tuple[float, tuple | None]:
        """Run a node's own time around its child calls (sequential)."""
        cur = start + own * 0.5
        for ch in n.get("calls", []) or []:
            reps = random.randint(*ch["repeat"]) if ch.get("repeat") else 1
            for _ in range(reps):
                end, f = self._build(tr, ch, svc, sid, cur)
                cur = end + random.uniform(0.2, 2.0)
                if f and ch.get("on_error", "fail") == "fail":
                    return cur + 2, f
        return cur + own * 0.5, (self._err(n) if own_fail else None)

    def _serve(self, tr, n, svc, parent, start, ctx=None) -> tuple[float, tuple | None]:
        """Server side of an entry / HTTP / MCP call, executed inside service `svc`."""
        own, own_fail = self._node_params(n)
        sid = rid(8)
        end, err = self._body(tr, n, svc, sid, start, own, own_fail)
        typ = n.get("type", "entry")
        if typ == "mcp":
            name = f"tools/call {n['tool']}"
            attrs = {"mcp.method.name": "tools/call", "gen_ai.tool.name": n["tool"]}
        else:
            method, route = n.get("method", "GET"), n.get("route", "/")
            name = f"{method} {route}"
            attrs = {"http.request.method": method, "http.route": route, "url.path": route,
                     "http.response.status_code": (n.get("error_status", 500) if err else 200)}
        attrs.update(ctx or {})
        attrs.update(n.get("attrs") or {})
        tr.span(svc, name, start, end - start, parent=parent, kind=2, attrs=attrs, error=err, sid=sid)
        if err:
            tr.log(svc, sid, end, "ERROR", f"{name} failed: {err[0]}: {err[1]}")
        elif not parent:
            tr.log(svc, sid, 0.5, "INFO", f"{name} received")
        return end, err

    def _build(self, tr, n, svc, parent, start) -> tuple[float, tuple | None]:
        typ = n.get("type", "work")
        if typ in ("http", "mcp"):
            to = n["to"]
            cid = rid(8)
            net = random.uniform(0.5, 4)
            if to in self.incident.down:
                end = start + random.uniform(15, 60)
                err = ("ConnectError", f"connection refused: {to}")
                tr.span(svc, self._client_name(n), start, end - start, parent=parent, kind=3,
                        attrs=self._client_attrs(n, 503), error=err, sid=cid)
                return end, err
            if to in self.t["services"]:
                send, serr = self._serve(tr, n, to, cid, start + net)
            else:  # an external dependency we can't see inside
                own, own_fail = self._node_params(n)
                send, serr = start + net + own, (self._err(n) if own_fail else None)
            end = send + net
            tr.span(svc, self._client_name(n), start, end - start, parent=parent, kind=3,
                    attrs=self._client_attrs(n, 500 if serr else 200), error=serr, sid=cid)
            return end, serr

        own, own_fail = self._node_params(n)
        if typ == "db":
            stmt = random.choice(n.get("statements") or [n.get("statement", "SELECT 1")])
            err = self._err(n) if own_fail else None
            sid = tr.span(svc, f"{stmt.split()[0].upper()} {n.get('database', '')}".strip(), start, own, parent=parent, kind=3,
                          attrs={"db.system": n.get("system", "mssql"), "db.namespace": n.get("database"),
                                 "server.address": n.get("server"), "db.query.text": stmt,
                                 "db.operation.name": stmt.split()[0].upper()}, error=err)
            if own > n.get("slow_log_ms", 2000):
                tr.log(svc, sid, start + own, "WARN", f"slow query {own:.0f}ms on {n.get('database', '')}")
            return start + own, err
        if typ == "llm":
            model = random.choice(n["model"]) if isinstance(n.get("model"), list) else n.get("model", "model")
            tin, tout = n.get("tokens_in", [500, 2000]), n.get("tokens_out", [100, 500])
            err = self._err(n) if own_fail else None
            tr.span(svc, f"{n.get('operation', 'chat')} {model}", start, own, parent=parent, kind=3, attrs={
                "gen_ai.system": n.get("provider", "openai"), "gen_ai.operation.name": n.get("operation", "chat"),
                "gen_ai.request.model": model, "gen_ai.usage.input_tokens": 0 if err else random.randint(*tin),
                "gen_ai.usage.output_tokens": 0 if err else random.randint(*tout)}, error=err)
            return start + own, err
        # work: an internal step that may make its own calls
        sid = rid(8)
        end, err = self._body(tr, n, svc, sid, start, own, own_fail)
        tr.span(svc, n.get("name", "work"), start, end - start, parent=parent, kind=1, attrs=n.get("attrs"), error=err, sid=sid)
        return end, err

    def _client_name(self, n):
        return f"tools/call {n['tool']}" if n.get("type") == "mcp" else n.get("method", "GET")

    def _client_attrs(self, n, status):
        if n.get("type") == "mcp":
            return {"mcp.method.name": "tools/call", "gen_ai.tool.name": n["tool"], "server.address": n["to"]}
        a = {"http.request.method": n.get("method", "GET"), "server.address": n["to"],
             "url.full": f"http://{n['to']}{n.get('route', '/')}", "http.response.status_code": status}
        if n["to"] not in self.t["services"]:
            a["peer.service"] = n.get("peer", n["to"])
        return a

    # ----------------------------------------------------------------- infra
    def infra_payloads(self, ts: int) -> list[dict]:
        inc = self.incident
        hour = (ts % 86400) / 3600
        diurnal = 0.6 + 0.4 * max(0, 1 - abs(hour - 13) / 9)
        out = []
        for host, h in self.t["hosts"].items():
            cpu = inc.host_cpu.get(host, h.get("cpu_base", 30) * diurnal + random.gauss(0, 4))
            if host in inc.host_cpu:
                cpu = inc.host_cpu[host] + random.uniform(-3, 3)
            mem = h.get("mem_base", 60) + random.gauss(0, 1.5)
            disk = h.get("disk_base", 50) + (ts % 86400) / 86400
            conts = []
            for svc, s in self.t["services"].items():
                if s.get("host") != host or not s.get("container"):
                    continue
                down = svc in inc.down
                if down and random.random() < 0.15:
                    self.restarts[s["container"]] += 1
                conts.append({"name": s["container"], "image": f"{s['container']}:{s.get('version', '1.0.0')}",
                              "status": "exited" if down else "running", "health": "unhealthy" if down else "healthy",
                              "cpu": 0 if down else max(0.5, cpu * random.uniform(0.2, 0.5)),
                              "mem_pct": 0 if down else random.uniform(15, 45), "mem_used": random.uniform(3e8, 1.8e9),
                              "restarts": self.restarts[s["container"]], "net_in": random.uniform(1e4, 2e5),
                              "net_out": random.uniform(1e4, 2e5)})
            for name in h.get("extra_containers", []) or []:
                conts.append({"name": name, "image": name, "status": "running", "health": "healthy",
                              "cpu": random.uniform(0.5, 6), "mem_pct": random.uniform(5, 25), "mem_used": 2e8,
                              "restarts": 0, "net_in": 1e4, "net_out": 1e4})
            checks = []
            for svc, s in self.t["services"].items():
                if s.get("host") == host and s.get("health"):
                    ok = svc not in inc.down
                    checks.append({"name": f"{svc} health", "type": "http", "target": s["health"], "service": svc,
                                   "ok": ok, "latency_ms": random.uniform(3, 30), "message": "HTTP 200" if ok else "connection refused"})
            for c in self.t["checks"]:
                if c.get("host") == host:
                    ok = c.get("service") not in inc.down
                    checks.append({**{k: v for k, v in c.items() if k != "host"}, "ok": ok,
                                   "latency_ms": random.uniform(0.8, 5), "message": "connected" if ok else "unreachable"})
            custom = [{"name": m["name"], "unit": m.get("unit", ""), "service": m.get("service", ""),
                       "value": random.uniform(*m.get("range", [0, 100]))} for m in h.get("metrics", []) or []]
            out.append({"ts": ts, "host": {"name": host, "app": self.t["application"], "agent_version": "sim",
                                           **{k: v for k, v in h.items() if k in ("os", "os_version", "role", "ip", "instance_id",
                                                                                  "instance_type", "region", "cpus", "mem_total")}},
                        "metrics": {"cpu": round(min(100, max(1, cpu)), 1), "mem_pct": round(mem, 1),
                                    "mem_used": h.get("mem_total", 16) * mem / 100 * 1e9, "disk_pct": round(disk, 1),
                                    "disks": [{"mount": "C:\\" if h.get("os") == "windows" else "/", "pct": round(disk, 1)}],
                                    "net_in": random.uniform(2e5, 4e6) * diurnal, "net_out": random.uniform(2e5, 4e6) * diurnal,
                                    "load1": cpu / 25, "procs": random.randint(120, 260), "tcp_conns": random.randint(40, 300)},
                        "containers": conts, "checks": checks, "custom": custom})
        return out

    # ------------------------------------------------------------------ send
    def post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"X-Pulse-Key": self.key} if self.key else {})})
        urllib.request.urlopen(req, timeout=30).read()

    def otlp_traces(self, traces: list[Trace]) -> dict:
        by_svc: dict[str, list] = {}
        for tr in traces:
            for svc, sps in tr.spans.items():
                by_svc.setdefault(svc, []).extend(sps)
        return {"resourceSpans": [{"resource": self.resource(svc), "scopeSpans": [{"scope": {"name": "pulse-simulator"}, "spans": sps}]}
                                  for svc, sps in by_svc.items()]}

    def send_traces(self, traces: list[Trace]):
        self.post("/v1/traces", self.otlp_traces(traces))
        logs: dict[str, list] = {}
        for tr in traces:
            for svc, lg in tr.logs.items():
                logs.setdefault(svc, []).extend(lg)
        if logs:
            self.post("/v1/logs", {"resourceLogs": [
                {"resource": self.resource(svc), "scopeLogs": [{"scope": {"name": "pulse-simulator"}, "logRecords": lg}]}
                for svc, lg in logs.items()]})

    def send_infra(self, ts: int):
        for p in self.infra_payloads(ts):
            self.post("/api/ingest/infra", p)


def main():
    ap = argparse.ArgumentParser(description="RapidPulse topology-driven traffic simulator")
    ap.add_argument("--topology", default=os.environ.get("PULSE_TOPOLOGY", str(DEFAULT_TOPOLOGY)))
    ap.add_argument("--url", default=os.environ.get("PULSE_URL", "http://127.0.0.1:4400"))
    ap.add_argument("--key", default=os.environ.get("PULSE_KEY"))
    ap.add_argument("--rate", type=float, default=1.0, help="requests per second (live)")
    ap.add_argument("--backfill-minutes", type=int, default=0, help="generate this much history first")
    ap.add_argument("--backfill-rate", type=float, default=0.5)
    ap.add_argument("--incidents", choices=["on", "off"], default="on", help="rotate the topology's incident scenarios")
    ap.add_argument("--scenario", help="pin one incident scenario by name")
    ap.add_argument("--duration", type=int, default=0, help="live seconds to run (0 = forever)")
    a = ap.parse_args()

    sim = Sim(load_topology(a.topology), a.url, a.key)
    if a.scenario and a.scenario not in sim.incident_names:
        sys.exit(f"unknown scenario {a.scenario!r}; topology defines: {', '.join(sim.incident_names) or 'none'}")

    def incident_at(ts):
        if a.scenario:
            return a.scenario
        names = sim.incident_names
        if a.incidents == "off" or not names:
            return None
        slot = int(ts // 300)  # 5-minute slots: an incident in one of every three
        return names[(slot // 3) % len(names)] if slot % 3 == 2 else None

    print(f"application '{sim.t['application']}' from {a.topology}", flush=True)
    if a.backfill_minutes:
        start = int((time.time() - a.backfill_minutes * 60) // 60 * 60)
        print(f"backfilling {a.backfill_minutes} minutes ...", flush=True)
        for i in range(a.backfill_minutes):
            m = start + i * 60
            sim.set_incident(incident_at(m))
            n = max(1, int(random.gauss(a.backfill_rate * 60, a.backfill_rate * 6)))
            batch = [sim.trace(int((m + random.random() * 60) * 1e9)) for _ in range(n)]
            for j in range(0, len(batch), 50):
                sim.send_traces(batch[j:j + 50])
            for s in range(0, 60, 15):
                sim.send_infra(m + s)
            if i % 10 == 0:
                print(f"  {i}/{a.backfill_minutes} minutes (incident={sim.incident.name})", flush=True)
        print("backfill done", flush=True)

    t_end = time.time() + a.duration if a.duration else float("inf")
    last_infra = 0.0
    print(f"live traffic at {a.rate}/s -> {a.url} (Ctrl+C to stop)", flush=True)
    try:
        while time.time() < t_end:
            tick = time.time()
            prev = sim.incident.name
            sim.set_incident(incident_at(tick))
            if sim.incident.name != prev:
                print(f"{time.strftime('%H:%M:%S')} incident -> {sim.incident.name}", flush=True)
            n = max(1, int(random.gauss(a.rate * 5, max(0.5, a.rate))))
            batch = [sim.trace(int((tick - 25 + random.random() * 5) * 1e9)) for _ in range(n)]
            try:
                sim.send_traces(batch)
                if tick - last_infra >= 15:
                    sim.send_infra(int(tick)); last_infra = tick
            except Exception as e:
                print("send failed:", e, file=sys.stderr, flush=True)
            time.sleep(max(0.0, 5 - (time.time() - tick)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
