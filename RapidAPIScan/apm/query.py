"""Read side: every dashboard, map, trace and report query."""
from __future__ import annotations

import json
import time
from collections import defaultdict

from .semantics import REQ_BODY, REQ_HEADER, bucket_upper, model_label
from .store import Store


# ----------------------------------------------------------------- helpers
def request_of(span: dict) -> dict | None:
    """The HTTP request a span received or sent: method, URL, headers and payload, from the
    attributes the instrumentation captured (http.request.header.*, http.request.body)."""
    a = span.get("attributes") or {}
    headers = {}
    for k, v in a.items():
        if k.lower().startswith(REQ_HEADER):
            # OpenTelemetry Python stores `content-type` as `content_type`; show the real header name
            name = k[len(REQ_HEADER):].replace("_", "-")
            headers[name] = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    body = a.get(REQ_BODY)
    if not headers and body in (None, ""):
        return None
    url = a.get("url.full") or a.get("http.url")
    if not url:
        path = a.get("url.path") or a.get("http.target") or span.get("http_route") or ""
        query = a.get("url.query")
        # The address the client called (Host / X-Forwarded-* headers) rather than the
        # container's internal address, which is what server.address holds behind Docker NAT.
        fwd_host = headers.get("x-forwarded-host", "").split(",")[0].strip()
        client_host = fwd_host or headers.get("host", "")
        if client_host:
            scheme = (headers.get("x-forwarded-proto", "").split(",")[0].strip()
                      or a.get("url.scheme") or a.get("http.scheme") or "http")
            hostport = client_host
        else:
            host = a.get("server.address") or a.get("http.host") or ""
            port = a.get("server.port")
            scheme = a.get("url.scheme") or a.get("http.scheme") or "http"
            hostport = f"{host}:{port}" if host and port and f":{port}" not in str(host) else str(host)
        if str(path).startswith(("http://", "https://")):
            hostport = ""
        url = (f"{scheme}://{hostport}" if hostport else "") + str(path) + (f"?{query}" if query else "")
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return {"method": span.get("http_method") or a.get("http.request.method") or a.get("http.method") or "",
            "url": url, "headers": dict(sorted(headers.items())), "content_type": ctype,
            "body": body if body not in (None, "") else None,
            "body_size": a.get("http.request.body.size") or (len(body) if isinstance(body, str) else None),
            "status": span.get("http_status")}


def merge_buckets(rows) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for r in rows:
        b = r["buckets"] if isinstance(r, dict) else r
        for k, v in (json.loads(b) if isinstance(b, str) else b).items():
            out[int(k)] += v
    return out


def percentile(b: dict[int, int], q: float) -> float | None:
    total = sum(b.values())
    if not total:
        return None
    target, seen = q * total, 0
    for i in sorted(b):
        n = b[i]
        if seen + n >= target:
            lo = bucket_upper(i - 1) if i > 0 else 0.0
            hi = bucket_upper(i)
            frac = (target - seen) / n if n else 1
            return round(lo + (hi - lo) * frac, 1)
        seen += n
    return round(bucket_upper(max(b)), 1)


def apdex(b: dict[int, int], t_ms: float) -> float | None:
    total = sum(b.values())
    if not total:
        return None
    sat = sum(v for i, v in b.items() if bucket_upper(i) <= t_ms)
    tol = sum(v for i, v in b.items() if t_ms < bucket_upper(i) <= 4 * t_ms)
    return round((sat + tol / 2) / total, 3)


def pick_step(frm: int, to: int) -> int:
    span = max(60, to - frm)
    for step in (60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400):
        if span / step <= 120:
            return step
    return 86400


def _where(app: str | None, frm: int, to: int, col: str = "minute", **eq) -> tuple[str, list]:
    clauses, params = [f"{col} >= ?", f"{col} < ?"], [frm, to]
    if app and app != "all":
        clauses.append("app = ?"); params.append(app)
    for k, v in eq.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)):
            clauses.append(f"{k} IN ({','.join('?' * len(v))})"); params.extend(v)
        else:
            clauses.append(f"{k} = ?"); params.append(v)
    return " AND ".join(clauses), params


def stats_from(rows: list[dict], minutes: float, t_ms: float) -> dict:
    cnt = sum(r["cnt"] for r in rows)
    err = sum(r["errors"] for r in rows)
    s = sum(r["sum_ms"] for r in rows)
    b = merge_buckets(rows)
    codes: dict[str, int] = defaultdict(int)
    for r in rows:
        for k, v in json.loads(r.get("codes") or "{}").items():
            codes[k] += v
    return {
        "calls": cnt, "errors": err,
        "error_rate": round(100.0 * err / cnt, 2) if cnt else 0.0,
        "rpm": round(cnt / max(minutes, 1), 2),
        "avg_ms": round(s / cnt, 1) if cnt else None,
        "p50_ms": percentile(b, 0.50), "p90_ms": percentile(b, 0.90),
        "p95_ms": percentile(b, 0.95), "p99_ms": percentile(b, 0.99),
        "max_ms": round(max((r["max_ms"] for r in rows), default=0), 1) if rows else None,
        "apdex": apdex(b, t_ms), "codes": dict(codes),
        "tokens_in": sum(r.get("tokens_in") or 0 for r in rows),
        "tokens_out": sum(r.get("tokens_out") or 0 for r in rows),
        "cost": round(sum(r.get("cost") or 0 for r in rows), 4),
    }


def health(stats: dict, cfg: dict, down: bool = False, service: str | None = None) -> str:
    if down:
        return "critical"
    if not stats.get("calls"):
        return "unknown"
    # An LLM-backed endpoint is legitimately slower than a CRUD one: per-service override.
    slow = (cfg.get("services", {}).get(service or "", {}) or {}).get(
        "slow_request_ms", cfg["thresholds"]["slow_request_ms"])
    if stats["error_rate"] >= 10 or (stats["p95_ms"] or 0) >= 2 * slow:
        return "critical"
    if stats["error_rate"] >= 2 or (stats["p95_ms"] or 0) >= slow:
        return "warning"
    return "healthy"


class Queries:
    def __init__(self, store: Store, cfg: dict):
        self.s, self.cfg = store, cfg

    @property
    def t_ms(self) -> float:
        return float(self.cfg["thresholds"]["apdex_t_ms"])

    # ----------------------------------------------------------- metadata
    def meta(self) -> dict:
        since = int(time.time()) - 7 * 86400
        apps = [r["app"] for r in self.s.q("SELECT DISTINCT app FROM rollups WHERE minute >= ? ORDER BY app", (since,))]
        services = self.s.q("SELECT DISTINCT app, service FROM rollups WHERE minute >= ? AND role='entry' ORDER BY service", (since,))
        return {"apps": apps, "services": services, "config_services": self.cfg.get("services", {}),
                "thresholds": self.cfg["thresholds"]}

    # --------------------------------------------------------- time series
    def series(self, frm: int, to: int, step: int | None = None, *, app=None, metrics=("calls",),
               group_by: str | None = None, top: int = 8, **filters) -> dict:
        step = step or pick_step(frm, to)
        where, params = _where(app, frm, to, **filters)
        gcol = f", {group_by} AS g" if group_by else ", '' AS g"
        rows = self.s.q(f"SELECT (minute / {step}) * {step} AS t {gcol}, cnt, errors, sum_ms, max_ms, buckets, "
                        f"tokens_in, tokens_out, cost FROM rollups WHERE {where}", params)
        groups: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        totals: dict[str, int] = defaultdict(int)
        for r in rows:
            groups[r["g"]][r["t"]].append(r)
            totals[r["g"]] += r["cnt"]
        keep = sorted(totals, key=lambda g: -totals[g])[:top]
        # Drop the still-filling last bucket: a half-full minute reads as a traffic drop.
        times = list(range((frm // step) * step, max((frm // step) * step + step, (to // step) * step), step))
        out = {"step": step, "times": times, "series": []}
        for g in keep:
            for m in metrics:
                pts = []
                for t in times:
                    bucket = groups[g].get(t, [])
                    pts.append(self._metric(bucket, m, step))
                out["series"].append({"group": g, "metric": m, "points": pts})
        return out

    def _metric(self, rows: list[dict], m: str, step: int):
        cnt = sum(r["cnt"] for r in rows)
        if m == "calls":
            return cnt
        if m == "rpm":
            return round(cnt / (step / 60), 2)
        if m == "errors":
            return sum(r["errors"] for r in rows)
        if m == "error_rate":
            return round(100 * sum(r["errors"] for r in rows) / cnt, 2) if cnt else None
        if m == "avg_ms":
            return round(sum(r["sum_ms"] for r in rows) / cnt, 1) if cnt else None
        if m in ("p50_ms", "p90_ms", "p95_ms", "p99_ms"):
            return percentile(merge_buckets(rows), int(m[1:3]) / 100) if cnt else None
        if m == "max_ms":
            return round(max((r["max_ms"] for r in rows), default=0), 1) if cnt else None
        if m == "tokens":
            return sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows)
        if m == "tokens_in":
            return sum(r["tokens_in"] or 0 for r in rows)
        if m == "tokens_out":
            return sum(r["tokens_out"] or 0 for r in rows)
        if m == "cost":
            return round(sum(r["cost"] or 0 for r in rows), 5)
        return None

    # ------------------------------------------------------------- tables
    def grouped(self, frm: int, to: int, by: list[str], *, app=None, **filters) -> list[dict]:
        where, params = _where(app, frm, to, **filters)
        rows = self.s.q(f"SELECT {', '.join(by)}, cnt, errors, sum_ms, max_ms, buckets, codes, tokens_in, tokens_out, cost "
                        f"FROM rollups WHERE {where}", params)
        groups: dict[tuple, list] = defaultdict(list)
        for r in rows:
            groups[tuple(r[k] for k in by)].append(r)
        minutes = (to - frm) / 60
        out = []
        for key, rs in groups.items():
            d = dict(zip(by, key))
            d.update(stats_from(rs, minutes, self.t_ms))
            out.append(d)
        out.sort(key=lambda d: -d["calls"])
        return out

    def down_services(self) -> set[str]:
        """Services whose most recent synthetic check failed."""
        rows = self.s.q("""SELECT c.service, c.ok FROM checks c JOIN
                           (SELECT name, MAX(ts) AS ts FROM checks WHERE ts > ? GROUP BY name) m
                           ON c.name = m.name AND c.ts = m.ts""", (int(time.time()) - 600,))
        return {r["service"] for r in rows if r["service"] and not r["ok"]}

    def services(self, frm, to, app=None) -> list[dict]:
        rows = self.grouped(frm, to, ["app", "service"], app=app, role="entry")
        down = self.down_services()
        svc_cfg = self.cfg.get("services", {})
        hosts = {r["service"]: r for r in self.s.q(
            "SELECT service, GROUP_CONCAT(DISTINCT host) AS hosts, GROUP_CONCAT(DISTINCT container) AS containers "
            "FROM spans WHERE start_ns >= ? GROUP BY service", (int((to - 3600) * 1e9),))}
        seen = set()
        for r in rows:
            seen.add(r["service"])
            r["health"] = health(r, self.cfg, r["service"] in down, r["service"])
            r["display"] = svc_cfg.get(r["service"], {}).get("display", r["service"])
            r["tier"] = svc_cfg.get(r["service"], {}).get("tier", "")
            r["hosts"] = (hosts.get(r["service"]) or {}).get("hosts") or ""
            r["containers"] = (hosts.get(r["service"]) or {}).get("containers") or ""
        for svc in down - seen:  # a down service with no traffic still shows up
            rows.append({"service": svc, "display": svc_cfg.get(svc, {}).get("display", svc), "app": app or "",
                         "calls": 0, "errors": 0, "error_rate": 0, "rpm": 0, "health": "critical"})
        return rows

    # ------------------------------------------------------------ overview
    def overview(self, frm, to, app=None) -> dict:
        tw, tp = _where(app, int(frm * 1e9), int(to * 1e9), col="start_ns")
        tr = self.s.q1(f"""SELECT COUNT(*) AS traces, SUM(error_count > 0) AS failed,
                              AVG((end_ns - start_ns) / 1e6) AS avg_ms FROM traces WHERE {tw}""", tp) or {}
        durs = [r["d"] for r in self.s.q(
            f"SELECT (end_ns - start_ns) / 1e6 AS d FROM traces WHERE {tw} ORDER BY d LIMIT 200000", tp)]
        p95 = durs[int(0.95 * (len(durs) - 1))] if durs else None
        services = self.services(frm, to, app)
        ai = self.grouped(frm, to, ["category"], app=app, category=["llm", "mcp"], role="exit")
        ai_by = {r["category"]: r for r in ai}
        db = self.grouped(frm, to, ["category"], app=app, category="db", role="exit")
        infra = self.infra_summary()
        alerts = self.s.q1("SELECT COUNT(*) AS n, SUM(severity='critical') AS crit FROM alert_events WHERE state='firing'") or {}
        series = self.series(frm, to, app=app, role="entry", metrics=("rpm", "error_rate", "p95_ms"))
        top_slow = sorted(self.grouped(frm, to, ["service", "operation"], app=app, role="entry"),
                          key=lambda r: -(r["p95_ms"] or 0))[:8]
        top_err = [r for r in sorted(self.grouped(frm, to, ["service", "operation"], app=app),
                                     key=lambda r: -r["errors"]) if r["errors"]][:8]
        return {
            "kpi": {
                "traces": tr.get("traces") or 0, "failed_traces": tr.get("failed") or 0,
                "trace_error_rate": round(100 * (tr.get("failed") or 0) / tr["traces"], 2) if tr.get("traces") else 0,
                "trace_avg_ms": round(tr["avg_ms"], 1) if tr.get("avg_ms") else None,
                "trace_p95_ms": round(p95, 1) if p95 else None,
                "llm_calls": (ai_by.get("llm") or {}).get("calls", 0),
                "llm_cost": (ai_by.get("llm") or {}).get("cost", 0),
                "llm_tokens": ((ai_by.get("llm") or {}).get("tokens_in", 0) + (ai_by.get("llm") or {}).get("tokens_out", 0)),
                "mcp_calls": (ai_by.get("mcp") or {}).get("calls", 0),
                "db_calls": db[0]["calls"] if db else 0, "db_p95_ms": db[0]["p95_ms"] if db else None,
                "alerts_firing": alerts.get("n") or 0, "alerts_critical": alerts.get("crit") or 0,
                "hosts": infra["hosts"], "hosts_down": infra["hosts_down"],
                "containers": infra["containers"], "containers_unhealthy": infra["containers_unhealthy"],
            },
            "services": services, "series": series, "top_slow": top_slow, "top_errors": top_err,
            "current_model": self.current_model(app),
        }

    # --------------------------------------------------------- service map
    def service_map(self, frm, to, app=None) -> dict:
        where, params = _where(app, frm, to)
        raw_edges = self.s.q(f"""SELECT src, dst, dst_type, SUM(cnt) AS calls, SUM(errors) AS errors, SUM(sum_ms) AS sum_ms
                                 FROM edges WHERE {where} GROUP BY src, dst, dst_type""", params)
        services = {r["service"]: r for r in self.services(frm, to, app)}
        # A call to an instrumented service that never reached it (connection refused, so no
        # server span) is recorded as an "http" exit; fold it into the service edge.
        merged: dict[tuple, dict] = {}
        for e in raw_edges:
            typ = "service" if e["dst"] in services else e["dst_type"]
            m = merged.setdefault((e["src"], e["dst"]), {"src": e["src"], "dst": e["dst"], "dst_type": typ,
                                                         "calls": 0, "errors": 0, "sum_ms": 0.0})
            m["calls"] += e["calls"]; m["errors"] += e["errors"]; m["sum_ms"] += e["sum_ms"]
            if typ == "service":
                m["dst_type"] = "service"
        edges = list(merged.values())
        exits = {(r["service"], r["peer"]): r for r in self.grouped(frm, to, ["service", "peer"], app=app, role="exit")}
        minutes = max(1, (to - frm) / 60)
        nodes: dict[str, dict] = {}
        svc_cfg = self.cfg.get("services", {})
        host_os = {h["host"]: h for h in self.s.q("SELECT host, os, role FROM hosts")}
        for name, r in services.items():
            hosts = [h for h in (r.get("hosts") or "").split(",") if h]
            nodes[name] = {"id": name, "label": r.get("display", name), "type": svc_cfg.get(name, {}).get("type", "service"),
                           "tier": r.get("tier", ""), "health": r["health"], "rpm": r.get("rpm", 0),
                           "error_rate": r.get("error_rate", 0), "avg_ms": r.get("avg_ms"), "p95_ms": r.get("p95_ms"),
                           "hosts": hosts, "os": sorted({(host_os.get(h) or {}).get("os") or "" for h in hosts} - {""}),
                           "containers": [c for c in (r.get("containers") or "").split(",") if c]}
        out_edges = []
        for e in edges:
            for end, typ in ((e["src"], "service"), (e["dst"], e["dst_type"])):
                if end not in nodes:
                    nodes[end] = {"id": end, "label": svc_cfg.get(end, {}).get("display", end),
                                  "type": typ if typ != "http" else "external", "health": "unknown",
                                  "rpm": 0, "error_rate": 0, "avg_ms": None, "p95_ms": None, "hosts": [], "os": []}
            ex = exits.get((e["src"], e["dst"]))
            out_edges.append({"source": e["src"], "target": e["dst"], "type": e["dst_type"],
                              "calls": e["calls"], "rpm": round(e["calls"] / minutes, 2),
                              "errors": e["errors"],
                              "error_rate": round(100 * e["errors"] / e["calls"], 2) if e["calls"] else 0,
                              "avg_ms": round(e["sum_ms"] / e["calls"], 1) if e["calls"] else None,
                              "p95_ms": ex["p95_ms"] if ex else None})
        # Virtual dependency nodes get their health from the calls into them.
        for n in nodes.values():
            if n["type"] in ("database", "llm", "mcp", "external"):
                inc = [e for e in out_edges if e["target"] == n["id"]]
                calls = sum(e["calls"] for e in inc)
                errs = sum(e["errors"] for e in inc)
                if calls:
                    n["rpm"] = round(calls / minutes, 2)
                    n["error_rate"] = round(100 * errs / calls, 2)
                    n["avg_ms"] = round(sum((e["avg_ms"] or 0) * e["calls"] for e in inc) / calls, 1)
                    n["health"] = health({"calls": calls, "error_rate": n["error_rate"], "p95_ms": n["avg_ms"]},
                                         self.cfg, service=n["id"])
        return {"nodes": list(nodes.values()), "edges": out_edges}

    # ------------------------------------------------------------- traces
    def traces(self, frm, to, app=None, service=None, operation=None, status=None, min_ms=None, max_ms=None,
               q=None, attr=None, sort="recent", limit=100) -> list[dict]:
        where, params = _where(app, int(frm * 1e9), int(to * 1e9), col="start_ns")
        if service:
            where += " AND instr(services, ?) > 0"; params.append(f",{service},")
        if operation:
            where += " AND root_name LIKE ?"; params.append(f"%{operation}%")
        if status == "error":
            where += " AND error_count > 0"
        elif status == "ok":
            where += " AND error_count = 0"
        if min_ms:
            where += " AND (end_ns - start_ns) >= ?"; params.append(int(float(min_ms) * 1e6))
        if max_ms:
            where += " AND (end_ns - start_ns) <= ?"; params.append(int(float(max_ms) * 1e6))
        if q:
            where += " AND (trace_id = ? OR root_name LIKE ?)"; params += [q.strip().lower(), f"%{q}%"]
        if attr:
            where += " AND trace_id IN (SELECT trace_id FROM spans WHERE start_ns >= ? AND attributes LIKE ?)"
            params += [int(frm * 1e9), f"%{attr}%"]
        order = "(end_ns - start_ns) DESC" if sort == "slowest" else "start_ns DESC"
        rows = self.s.q(f"""SELECT trace_id, app, start_ns, (end_ns - start_ns) / 1e6 AS duration_ms, span_count,
                                   error_count, services, COALESCE(root_service, first_service) AS root_service,
                                   COALESCE(root_name, '(root span pending)') AS root_name, root_status, http_status
                            FROM traces WHERE {where} ORDER BY {order} LIMIT ?""", params + [int(limit)])
        for r in rows:
            r["services"] = [s for s in r["services"].split(",") if s]
            r["duration_ms"] = round(r["duration_ms"], 1)
        return rows

    def trace(self, trace_id: str) -> dict | None:
        spans = self.s.q("SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ns", (trace_id.lower(),))
        if not spans:
            return None
        by_id = {s["span_id"]: s for s in spans}
        children: dict[str, list] = defaultdict(list)
        roots = []
        for s in spans:
            s["attributes"] = json.loads(s["attributes"] or "{}")
            s["resource"] = json.loads(s["resource"] or "{}")
            s["events"] = json.loads(s["events"] or "[]")
            if s["parent_id"] and s["parent_id"] in by_id:
                children[s["parent_id"]].append(s)
            else:
                roots.append(s)
        t0 = min(s["start_ns"] for s in spans)
        t1 = max(s["end_ns"] for s in spans)

        # Self time = own duration minus the union of child intervals (concurrency-aware).
        def self_time(s) -> float:
            iv = sorted((max(c["start_ns"], s["start_ns"]), min(c["end_ns"], s["end_ns"])) for c in children[s["span_id"]])
            covered, end = 0, s["start_ns"]
            for a, b in iv:
                if b <= end:
                    continue
                covered += b - max(a, end)
                end = b
            return max(0.0, (s["end_ns"] - s["start_ns"] - covered) / 1e6)

        ordered: list[dict] = []

        def walk(s, depth):
            s["depth"] = depth
            s["offset_ms"] = round((s["start_ns"] - t0) / 1e6, 3)
            s["self_ms"] = round(self_time(s), 3)
            s["child_count"] = len(children[s["span_id"]])
            ordered.append(s)
            for c in sorted(children[s["span_id"]], key=lambda x: x["start_ns"]):
                walk(c, depth + 1)
        for r in sorted(roots, key=lambda x: x["start_ns"]):
            walk(r, 0)

        # Where did the time go? per service and per component (dependency peers count separately)
        by_comp: dict[str, dict] = defaultdict(lambda: {"self_ms": 0.0, "spans": 0, "errors": 0, "type": "service"})
        for s in ordered:
            comp = s["peer"] if (s["peer"] and s["peer_type"] in ("database", "llm", "mcp") and not children[s["span_id"]]) else s["service"]
            c = by_comp[comp]
            c["self_ms"] += s["self_ms"]; c["spans"] += 1; c["errors"] += s["status"] == "error"
            if comp != s["service"]:
                c["type"] = s["peer_type"]
        total_ms = (t1 - t0) / 1e6
        breakdown = sorted(({"component": k, **v, "self_ms": round(v["self_ms"], 1),
                             "pct": round(100 * v["self_ms"] / total_ms, 1) if total_ms else 0}
                            for k, v in by_comp.items()), key=lambda x: -x["self_ms"])

        # Critical path: from the root, follow the child that finishes last.
        critical = []
        if roots:
            cur = max(roots, key=lambda r: r["end_ns"] - r["start_ns"])
            while cur:
                critical.append(cur["span_id"])
                kids = children[cur["span_id"]]
                cur = max(kids, key=lambda k: k["end_ns"]) if kids else None
        for s in ordered:
            s["critical"] = s["span_id"] in critical

        # Root cause: deepest error span (errors bubble up, the deepest is the origin).
        errs = [s for s in ordered if s["status"] == "error"]
        root_cause = max(errs, key=lambda s: s["depth"]) if errs else None
        bottleneck = max(ordered, key=lambda s: s["self_ms"])

        logs = self.s.q("SELECT ts_ns, service, severity, body, span_id FROM logs WHERE trace_id = ? ORDER BY ts_ns LIMIT 500",
                        (trace_id.lower(),))
        hosts = sorted({s["host"] for s in spans if s["host"]})
        t_s = int(t0 / 1e9)
        infra = []
        for h in hosts:
            m = self.s.q1("SELECT * FROM host_metrics WHERE host = ? AND ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
                          (h, t_s - 300, t_s + 300, t_s))
            meta = self.s.q1("SELECT os, role FROM hosts WHERE host = ?", (h,)) or {}
            if m:
                infra.append({"host": h, **meta, "cpu": m["cpu"], "mem_pct": m["mem_pct"], "disk_pct": m["disk_pct"], "ts": m["ts"]})
        conts = sorted({(s["host"], s["container"]) for s in spans if s["container"]})
        for h, cname in conts:
            m = self.s.q1("SELECT * FROM container_metrics WHERE container = ? AND ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
                          (cname, t_s - 300, t_s + 300, t_s))
            if m:
                infra.append({"host": f"{m['host']} / {cname}", "container": cname, "cpu": m["cpu"], "mem_pct": m["mem_pct"],
                              "status": m["status"], "restarts": m["restarts"], "ts": m["ts"]})
        llm = [s for s in ordered if s["category"] == "llm"]
        for s in ordered:
            s["request"] = request_of(s)
        # Some OpenTelemetry versions record the payload on the internal "http receive" child span
        # rather than on the request span: show it with the request it belongs to.
        for s in ordered:
            r = s["request"]
            if r and r["body"] is None and s["kind"] == "server":
                kid = next((c for c in children[s["span_id"]]
                            if (c.get("attributes") or {}).get(REQ_BODY) not in (None, "")), None)
                if kid:
                    r["body"] = kid["attributes"][REQ_BODY]
                    r["body_size"] = kid["attributes"].get("http.request.body.size") or len(r["body"])
        # The request that started the trace: the earliest entry (server) span that has one.
        entry = next((s for s in ordered if s["is_entry"] and s["request"]), None)             or next((s for s in ordered if s["request"]), None)
        return {
            "request": ({**entry["request"], "service": entry["service"], "span_id": entry["span_id"]} if entry else None),
            "trace_id": trace_id.lower(), "start_ns": t0, "duration_ms": round(total_ms, 3),
            "span_count": len(spans), "error_count": len(errs),
            "services": sorted({s["service"] for s in spans}),
            "spans": ordered, "breakdown": breakdown,
            "bottleneck": {"span_id": bottleneck["span_id"], "service": bottleneck["service"],
                           "name": bottleneck["name"], "self_ms": bottleneck["self_ms"]},
            "root_cause": ({"span_id": root_cause["span_id"], "service": root_cause["service"], "name": root_cause["name"],
                            "error_type": root_cause["error_type"], "error_message": root_cause["error_message"]}
                           if root_cause else None),
            "ai": {"llm_calls": len(llm), "tokens_in": sum(s["tokens_in"] or 0 for s in llm),
                   "tokens_out": sum(s["tokens_out"] or 0 for s in llm),
                   "cost": round(sum(s["cost_usd"] or 0 for s in llm), 5),
                   "mcp_calls": sum(1 for s in ordered if s["category"] == "mcp")},
            "logs": logs, "infra": infra,
        }

    # --------------------------------------------------------- AI monitoring
    def current_model(self, app=None, recent_s: int = 3600) -> dict | None:
        """The model of the most recent LLM call (whatever the selected range), plus the other models
        called in the last hour. Read from the spans, so it follows a model change on its own."""
        a, ap = ("AND app = ? ", [app]) if app and app != "all" else ("", [])
        last = self.s.q1(f"""SELECT llm_model, llm_provider, service, start_ns FROM spans
                             WHERE category='llm' AND llm_model IS NOT NULL AND llm_model != '' {a}
                             ORDER BY start_ns DESC LIMIT 1""", ap)
        if not last:
            return None
        since = int((time.time() - recent_s) * 1e9)
        recent = self.s.q(f"""SELECT llm_model, COUNT(*) AS calls FROM spans
                              WHERE category='llm' AND start_ns >= ? AND llm_model IS NOT NULL AND llm_model != '' {a}
                              GROUP BY llm_model ORDER BY calls DESC""", [since] + ap)
        labels: dict[str, int] = {}
        for r in recent:
            lbl = model_label(r["llm_model"])
            labels[lbl] = labels.get(lbl, 0) + r["calls"]
        cur = model_label(last["llm_model"])
        return {"model": last["llm_model"], "label": cur, "provider": last["llm_provider"], "service": last["service"],
                "last_seen": last["start_ns"] / 1e9,
                "others": [{"label": k, "calls": v} for k, v in sorted(labels.items(), key=lambda x: -x[1]) if k != cur]}

    def ai(self, frm, to, app=None) -> dict:
        models = self.grouped(frm, to, ["operation", "peer"], app=app, category="llm", role="exit")
        callers = self.grouped(frm, to, ["service"], app=app, category="llm", role="exit")
        tools = self.grouped(frm, to, ["service", "operation"], app=app, category="mcp")
        totals = self.grouped(frm, to, ["category"], app=app, category="llm", role="exit")
        mcp_tot = self.grouped(frm, to, ["category"], app=app, category="mcp")
        series = self.series(frm, to, app=app, category="llm", role="exit",
                             metrics=("calls", "tokens_in", "tokens_out", "cost", "p95_ms", "error_rate"))
        by_model = self.series(frm, to, app=app, category="llm", role="exit", metrics=("cost",), group_by="operation")
        failures = self.s.q("""SELECT trace_id, span_id, start_ns, service, category, operation, llm_model, duration_ms,
                                      error_type, error_message FROM spans
                               WHERE category IN ('llm','mcp') AND status='error' AND start_ns >= ? AND start_ns < ?
                               ORDER BY start_ns DESC LIMIT 50""", (int(frm * 1e9), int(to * 1e9)))
        slow = self.s.q("""SELECT trace_id, span_id, start_ns, service, operation, duration_ms, tokens_in, tokens_out, cost_usd
                           FROM spans WHERE category='llm' AND start_ns >= ? AND start_ns < ?
                           ORDER BY duration_ms DESC LIMIT 15""", (int(frm * 1e9), int(to * 1e9)))
        return {"totals": totals[0] if totals else {}, "mcp_totals": mcp_tot[0] if mcp_tot else {},
                "models": models, "callers": callers, "tools": tools, "series": series, "cost_by_model": by_model,
                "failures": failures, "slowest": slow, "current_model": self.current_model(app)}

    # ------------------------------------------------------------ database
    def database(self, frm, to, app=None) -> dict:
        thr = self.cfg["thresholds"]["slow_query_ms"]
        dbs = self.grouped(frm, to, ["peer"], app=app, category="db", role="exit")
        queries = self.grouped(frm, to, ["peer", "service", "operation"], app=app, category="db", role="exit")
        queries.sort(key=lambda r: -(r["avg_ms"] or 0) * r["calls"])
        series = self.series(frm, to, app=app, category="db", role="exit", metrics=("calls", "avg_ms", "p95_ms", "error_rate"))
        slow = self.s.q("""SELECT trace_id, span_id, start_ns, service, peer, db_statement, duration_ms, status, error_message
                           FROM spans WHERE category='db' AND duration_ms >= ? AND start_ns >= ? AND start_ns < ?
                           ORDER BY duration_ms DESC LIMIT 50""", (thr, int(frm * 1e9), int(to * 1e9)))
        errors = self.s.q("""SELECT trace_id, span_id, start_ns, service, peer, db_statement, error_type, error_message
                             FROM spans WHERE category='db' AND status='error' AND start_ns >= ? AND start_ns < ?
                             ORDER BY start_ns DESC LIMIT 30""", (int(frm * 1e9), int(to * 1e9)))
        # The services that front the databases (data-access APIs): callers of db exits
        callers = self.grouped(frm, to, ["service"], app=app, category="db", role="exit")
        fronts = [r for r in self.services(frm, to, app) if r["service"] in {c["service"] for c in callers}]
        checks = [c for c in self.checks(frm, to) if c["type"] in ("tcp", "sql", "db")]
        return {"databases": dbs, "queries": queries[:100], "series": series, "slow": slow, "errors": errors,
                "callers": callers, "front_services": fronts, "connectivity": checks, "slow_threshold_ms": thr}

    # -------------------------------------------------------------- infra
    def infra_summary(self) -> dict:
        now = time.time()
        hosts = self.s.q("SELECT host, last_seen FROM hosts")
        down = sum(1 for h in hosts if now - (h["last_seen"] or 0) > 120)
        latest = self._latest_containers()
        bad = sum(1 for c in latest if (c["status"] or "") != "running" or (c["health"] or "") == "unhealthy")
        return {"hosts": len(hosts), "hosts_down": down, "containers": len(latest), "containers_unhealthy": bad}

    def _latest_containers(self, host=None) -> list[dict]:
        extra, p = ("AND c.host = ?", [host]) if host else ("", [])
        return self.s.q(f"""SELECT c.* FROM container_metrics c JOIN
                            (SELECT host, container, MAX(ts) AS ts FROM container_metrics WHERE ts > ? GROUP BY host, container) m
                            ON c.host = m.host AND c.container = m.container AND c.ts = m.ts {extra}
                            ORDER BY c.host, c.container""", [int(time.time()) - 3600] + p)

    def hosts(self) -> list[dict]:
        now = time.time()
        rows = self.s.q("SELECT * FROM hosts ORDER BY role, host")
        for h in rows:
            m = self.s.q1("SELECT * FROM host_metrics WHERE host = ? ORDER BY ts DESC LIMIT 1", (h["host"],)) or {}
            h["metrics"] = m
            h["status"] = "down" if now - (h["last_seen"] or 0) > 120 else (
                "critical" if (m.get("cpu") or 0) > 90 or (m.get("mem_pct") or 0) > 92 or (m.get("disk_pct") or 0) > 92
                else "warning" if (m.get("cpu") or 0) > 75 or (m.get("mem_pct") or 0) > 85 or (m.get("disk_pct") or 0) > 85
                else "healthy")
            h["containers"] = self._latest_containers(h["host"])
            h["services"] = [r["service"] for r in self.s.q(
                "SELECT DISTINCT service FROM spans WHERE host = ? AND start_ns > ?", (h["host"], int((now - 3600) * 1e9)))]
        return rows

    def host_series(self, host: str, frm: int, to: int) -> dict:
        step = pick_step(frm, to)
        rows = self.s.q(f"""SELECT (ts / {step}) * {step} AS t, AVG(cpu) AS cpu, AVG(mem_pct) AS mem, AVG(disk_pct) AS disk,
                                   AVG(net_in) AS net_in, AVG(net_out) AS net_out, AVG(tcp_conns) AS conns
                            FROM host_metrics WHERE host = ? AND ts >= ? AND ts < ? GROUP BY t ORDER BY t""", (host, frm, to))
        cont = self.s.q(f"""SELECT container, (ts / {step}) * {step} AS t, AVG(cpu) AS cpu, AVG(mem_pct) AS mem
                            FROM container_metrics WHERE host = ? AND ts >= ? AND ts < ? GROUP BY container, t ORDER BY t""",
                        (host, frm, to))
        return {"step": step, "host": rows, "containers": cont}

    # -------------------------------------------------------------- checks
    def checks(self, frm, to) -> list[dict]:
        rows = self.s.q("""SELECT name, type, target, app, service, source, COUNT(*) AS n, SUM(ok) AS ok_n,
                                  AVG(latency_ms) AS avg_ms, MAX(ts) AS last_ts FROM checks
                           WHERE ts >= ? AND ts <= ? GROUP BY name ORDER BY name""", (frm, to))
        for r in rows:
            last = self.s.q1("SELECT ok, message, latency_ms FROM checks WHERE name = ? ORDER BY ts DESC LIMIT 1", (r["name"],))
            r["uptime"] = round(100 * r["ok_n"] / r["n"], 2) if r["n"] else None
            r["last_ok"] = bool(last["ok"]) if last else None
            r["last_message"] = last["message"] if last else ""
            r["avg_ms"] = round(r["avg_ms"] or 0, 1)
        return rows

    # ---------------------------------------------------------------- logs
    def logs(self, frm, to, app=None, service=None, severity=None, q=None, limit=200) -> list[dict]:
        where, params = _where(app, int(frm * 1e9), int(to * 1e9), col="ts_ns", service=service)
        if severity:
            where += " AND severity = ?"; params.append(severity.upper())
        if q:
            where += " AND body LIKE ?"; params.append(f"%{q}%")
        return self.s.q(f"SELECT * FROM logs WHERE {where} ORDER BY ts_ns DESC LIMIT ?", params + [int(limit)])

    # ------------------------------------------------------------ analytics
    def report(self, frm, to, app=None, group_by="service,operation", **filters) -> dict:
        allowed = {"service", "operation", "category", "role", "peer", "app"}
        by = [g for g in group_by.split(",") if g in allowed] or ["service"]
        cur = self.grouped(frm, to, by, app=app, **filters)
        prev = {tuple(r[k] for k in by): r for r in self.grouped(frm - (to - frm), frm, by, app=app, **filters)}
        for r in cur:
            p = prev.get(tuple(r[k] for k in by))
            r["prev_calls"] = p["calls"] if p else None
            r["prev_p95_ms"] = p["p95_ms"] if p else None
            r["prev_error_rate"] = p["error_rate"] if p else None
        return {"group_by": by, "rows": cur}

    def errors(self, frm, to, app=None, service=None, limit=100) -> dict:
        where, params = _where(app, int(frm * 1e9), int(to * 1e9), col="start_ns", service=service)
        groups = self.s.q(f"""SELECT service, category, operation, COALESCE(error_type, '') AS error_type,
                                     http_status, COUNT(*) AS n, MAX(start_ns) AS last_ns, MAX(trace_id) AS sample_trace,
                                     MAX(error_message) AS message
                              FROM spans WHERE {where} AND status = 'error'
                              GROUP BY service, category, operation, error_type, http_status ORDER BY n DESC LIMIT ?""",
                           params + [int(limit)])
        return {"groups": groups}
