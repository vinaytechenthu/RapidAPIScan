"""Ingestion: OTLP traces/logs/metrics, and the RapidPulse agent's infrastructure payload."""
from __future__ import annotations

import json
import time

from . import otlp
from .semantics import make_redactor, normalise_span
from .store import Store

SEVERITY = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]


def _severity(num) -> str:
    """OTLP severityNumber (1..24, or its enum name) -> TRACE..FATAL."""
    if isinstance(num, str):
        return num.replace("SEVERITY_NUMBER_", "").rstrip("234") or "INFO"
    n = int(num or 9)
    return SEVERITY[min(5, max(0, (n - 1) // 4))]


class Ingestor:
    def __init__(self, store: Store, cfg: dict):
        self.store, self.cfg = store, cfg
        self.redact = make_redactor(cfg["security"].get("redact_attribute_patterns", []), cfg["security"])
        self.counters = {"spans": 0, "logs": 0, "metrics": 0, "infra": 0, "rejected": 0}

    # ---------------------------------------------------------------- traces
    def traces(self, payload: dict) -> int:
        recs = []
        for res, scope, sp in otlp.iter_spans(payload):
            try:
                rec = normalise_span(res, scope, sp, cfg=self.cfg, redact=self.redact)
            except Exception:  # one malformed span must not drop the batch
                self.counters["rejected"] += 1
                continue
            if rec["trace_id"] and rec["span_id"]:
                recs.append(rec)
        self.count_llm_calls_once(recs)
        self.store.insert_spans(recs)
        self.counters["spans"] += len(recs)
        return len(recs)

    def count_llm_calls_once(self, recs: list[dict]) -> None:
        """One model call often produces two nested LLM spans: the application's own span (an
        agent framework, OpenInference) and the SDK/HTTP client span underneath (e.g. the cloud provider
        call). Counting both doubles calls, tokens and cost. Keep one per nested chain - the one
        with token usage, preferring the innermost - and turn the other into an internal span."""
        llm = [r for r in recs if r["category"] == "llm"]
        if not llm:
            return
        for tid in {r["trace_id"] for r in llm}:
            stored = self.store.q("SELECT span_id, parent_id, category, tokens_in, tokens_out, cost_usd FROM spans "
                                  "WHERE trace_id = ?", (tid,))
            parent = {s["span_id"]: s["parent_id"] for s in stored}
            parent.update({r["span_id"]: r["parent_id"] for r in recs if r["trace_id"] == tid})
            used = lambda s: bool(s["tokens_in"] or s["tokens_out"] or s["cost_usd"])
            kept = {s["span_id"]: used(s) for s in stored if s["category"] == "llm"}

            def ancestors(sid: str) -> list[str]:
                out, seen = [], set()
                while (sid := parent.get(sid)) and sid not in seen and len(out) < 64:
                    seen.add(sid)
                    out.append(sid)
                return out

            batch = [r for r in llm if r["trace_id"] == tid]
            # Spans with usage first, innermost (deepest) first.
            batch.sort(key=lambda r: (not used(r), -len(ancestors(r["span_id"]))))
            for r in batch:
                sid = r["span_id"]
                related = [k for k in kept if k in ancestors(sid) or sid in ancestors(k)]
                twin = next((k for k in related if kept[k] or not used(r)), None)
                if twin is None:
                    kept[sid] = used(r)
                    continue
                a = json.loads(r["attributes"] or "{}")
                a["rapidpulse.llm_counted_in"] = twin
                r.update(category="internal", peer=None, peer_type=None, tokens_in=0, tokens_out=0, cost_usd=0.0,
                         operation=r["name"], attributes=json.dumps(a, default=str))

    # ------------------------------------------------------------------ logs
    def logs(self, payload: dict) -> int:
        rows = []
        for res, rec in otlp.iter_logs(payload):
            a = self.redact(otlp.attrs(rec.get("attributes")))
            body = otlp.any_value(rec.get("body"))
            if not isinstance(body, str):
                body = json.dumps(body, default=str)
            sev = rec.get("severityText") or _severity(rec.get("severityNumber"))
            ts = int(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano") or time.time_ns())
            rows.append((ts, otlp.norm_id(rec.get("traceId")), otlp.norm_id(rec.get("spanId")),
                         str(res.get("service.namespace") or "default"), str(res.get("service.name") or ""),
                         str(res.get("host.name") or ""), str(sev).upper(), (body or "")[:8000],
                         json.dumps(a, default=str)[:8000]))
        self.store.insert_logs(rows)
        self.counters["logs"] += len(rows)
        return len(rows)

    # --------------------------------------------------------------- metrics
    def metrics(self, payload: dict) -> int:
        rows = []
        for res, name, unit, ts_ns, value, pa in otlp.iter_metric_points(payload):
            rows.append((int(ts_ns // 1e9) if ts_ns else int(time.time()),
                         str(res.get("service.namespace") or "default"), str(res.get("service.name") or ""),
                         str(res.get("host.name") or ""), name, unit, value, json.dumps(pa, default=str)[:2000]))
        self.store.insert_metric_points(rows)
        self.counters["metrics"] += len(rows)
        return len(rows)

    # ----------------------------------------------------------------- infra
    def infra(self, p: dict) -> dict:
        h = p.get("host") or {}
        name = h.get("name")
        if not name:
            raise ValueError("host.name is required")
        ts = int(p.get("ts") or time.time())
        m = p.get("metrics") or {}
        conts = p.get("containers") or []
        checks = p.get("checks") or []
        custom = p.get("custom") or []

        def tx(c):
            c.execute("""INSERT INTO hosts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(host) DO UPDATE SET os=excluded.os, os_version=excluded.os_version,
                           role=COALESCE(excluded.role, hosts.role), ip=excluded.ip, instance_id=excluded.instance_id,
                           instance_type=excluded.instance_type, region=excluded.region, cpus=excluded.cpus,
                           mem_total=excluded.mem_total, agent_version=excluded.agent_version,
                           app=COALESCE(excluded.app, hosts.app), last_seen=excluded.last_seen, meta=excluded.meta""",
                      (name, h.get("os"), h.get("os_version"), h.get("role"), h.get("ip"), h.get("instance_id"),
                       h.get("instance_type"), h.get("region"), h.get("cpus"), h.get("mem_total"),
                       h.get("agent_version"), h.get("app"), time.time(), json.dumps(h.get("meta") or {})))
            if m:
                c.execute("INSERT INTO host_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (ts, name, m.get("cpu"), m.get("mem_pct"), m.get("mem_used"), m.get("disk_pct"),
                           json.dumps(m.get("disks") or []), m.get("net_in"), m.get("net_out"),
                           m.get("load1"), m.get("procs"), m.get("tcp_conns")))
            c.executemany("INSERT INTO container_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(ts, name, k.get("name"), k.get("image"), k.get("status"), k.get("health"),
                            k.get("cpu"), k.get("mem_pct"), k.get("mem_used"), k.get("restarts", 0),
                            k.get("net_in"), k.get("net_out")) for k in conts])
            c.executemany("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [(ts, k.get("name"), k.get("type"), k.get("target"), 1 if k.get("ok") else 0,
                            k.get("latency_ms"), (k.get("message") or "")[:500], k.get("app") or h.get("app"),
                            k.get("service"), name) for k in checks])
            c.executemany("INSERT INTO metric_points VALUES (?,?,?,?,?,?,?,?)",
                          [(ts, h.get("app") or "default", k.get("service") or "", name, k.get("name"),
                            k.get("unit") or "", float(k.get("value", 0)), json.dumps(k.get("attrs") or {}))
                           for k in custom if k.get("name") is not None])
        self.store.write(tx)
        self.counters["infra"] += 1
        return {"host": name, "containers": len(conts), "checks": len(checks), "custom": len(custom)}
