"""Alert rules, evaluation, and notification.

A rule = metric + optional scope + comparison + window. Unscoped rules fan out
(one alert per service / host / model / check), so "any service with >5% errors"
is a single rule. Events have a lifecycle: firing -> (re-notified every
cooldown) -> resolved.
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
import urllib.request
from email.message import EmailMessage

from .query import Queries
from .store import Store

log = logging.getLogger("pulse.alerts")

# metric -> (description, unit, scope keys)
METRICS = {
    "service.error_rate": ("API failure rate of a service's endpoints", "%", ["app", "service"]),
    "service.p95_ms": ("p95 response time of a service", "ms", ["app", "service"]),
    "service.avg_ms": ("Average response time of a service", "ms", ["app", "service"]),
    "service.rpm": ("Requests per minute (use < for traffic drop)", "rpm", ["app", "service"]),
    "endpoint.error_rate": ("Failure rate of an endpoint", "%", ["app", "service", "operation"]),
    "endpoint.p95_ms": ("p95 response time of an endpoint", "ms", ["app", "service", "operation"]),
    "llm.error_rate": ("LLM call failure rate", "%", ["app", "operation"]),
    "llm.p95_ms": ("LLM p95 latency", "ms", ["app", "operation"]),
    "llm.cost": ("LLM spend in the window", "USD", ["app", "operation"]),
    "llm.tokens": ("LLM tokens in the window", "tokens", ["app", "operation"]),
    "mcp.error_rate": ("MCP tool failure rate", "%", ["app", "operation"]),
    "mcp.p95_ms": ("MCP tool p95 latency", "ms", ["app", "operation"]),
    "db.error_rate": ("Database call failure rate", "%", ["app", "peer"]),
    "db.p95_ms": ("Database query p95", "ms", ["app", "peer"]),
    "db.slow_queries": ("Queries slower than the slow-query threshold", "count", ["app", "peer"]),
    "host.cpu": ("Host CPU utilisation (window average)", "%", ["host"]),
    "host.mem_pct": ("Host memory utilisation", "%", ["host"]),
    "host.disk_pct": ("Host disk utilisation (fullest volume)", "%", ["host"]),
    "host.unreachable_min": ("Minutes since the host agent last reported", "min", ["host"]),
    "container.down": ("Container not running / unhealthy (1 = down)", "bool", ["host", "container"]),
    "container.restarts": ("Container restarts within the window", "count", ["host", "container"]),
    "check.failed": ("Synthetic availability / connectivity check failing (1 = failing)", "bool", ["name"]),
}

DEFAULT_RULES = [
    ("API failure rate high", "service.error_rate", {}, ">", 5, 5, "critical", "Any service with > 5% failed requests"),
    ("API latency high", "service.p95_ms", {}, ">", 3000, 5, "warning",
     "Any service with p95 > 3 s (or above its own slow_request_ms from pulse.yaml)"),
    ("Service down (health check)", "check.failed", {}, ">=", 1, 2, "critical", "Synthetic check failing"),
    ("Host CPU high", "host.cpu", {}, ">", 85, 5, "warning", "CPU > 85% for 5 minutes"),
    ("Host memory high", "host.mem_pct", {}, ">", 90, 5, "warning", "Memory > 90%"),
    ("Host disk nearly full", "host.disk_pct", {}, ">", 90, 10, "critical", "A volume above 90%"),
    ("Host agent not reporting", "host.unreachable_min", {}, ">", 3, 10, "critical", "Server down or agent stopped"),
    ("Container down", "container.down", {}, ">=", 1, 2, "critical", "Container exited or unhealthy"),
    ("Container restarting", "container.restarts", {}, ">=", 3, 15, "warning", "Crash loop"),
    ("LLM failures", "llm.error_rate", {}, ">", 5, 5, "critical", "LLM call failure rate > 5%"),
    ("LLM latency high", "llm.p95_ms", {}, ">", 20000, 10, "warning", "LLM p95 > 20 s"),
    ("LLM spend high", "llm.cost", {}, ">", 50, 60, "warning", "More than $50 per hour on one model"),
    ("MCP tool failures", "mcp.error_rate", {}, ">", 5, 5, "critical", "MCP tool failure rate > 5%"),
    ("Slow SQL queries", "db.slow_queries", {}, ">", 10, 5, "warning", "More than 10 slow queries in 5 minutes"),
    ("Database errors", "db.error_rate", {}, ">", 2, 5, "critical", "Database call failure rate > 2%"),
]

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}


class AlertEngine:
    def __init__(self, store: Store, q: Queries, cfg: dict):
        self.s, self.q, self.cfg = store, q, cfg

    # ---------------------------------------------------------------- rules
    def seed_defaults(self) -> None:
        if not self.cfg.get("default_alert_rules", True):
            return
        if (self.s.q1("SELECT COUNT(*) AS n FROM alert_rules") or {}).get("n"):
            return
        self.s.write(lambda c: c.executemany(
            "INSERT INTO alert_rules (name, enabled, metric, scope, op, threshold, window_min, severity, cooldown_min, "
            "channels, description, created) VALUES (?,1,?,?,?,?,?,?,30,'[]',?,?)",
            [(n, m, json.dumps(sc), op, th, w, sev, d, time.time()) for n, m, sc, op, th, w, sev, d in DEFAULT_RULES]))

    def rules(self) -> list[dict]:
        rows = self.s.q("SELECT * FROM alert_rules ORDER BY id")
        for r in rows:
            r["scope"] = json.loads(r["scope"] or "{}")
            r["channels"] = json.loads(r["channels"] or "[]")
            r["enabled"] = bool(r["enabled"])
        return rows

    def upsert_rule(self, rule: dict) -> int:
        if rule.get("metric") not in METRICS:
            raise ValueError(f"unknown metric {rule.get('metric')!r}")
        if rule.get("op", ">") not in OPS:
            raise ValueError("op must be one of " + ", ".join(OPS))
        vals = (rule.get("name") or rule["metric"], 1 if rule.get("enabled", True) else 0, rule["metric"],
                json.dumps({k: v for k, v in (rule.get("scope") or {}).items() if v}), rule.get("op", ">"),
                float(rule.get("threshold", 0)), int(rule.get("window_min", 5)), rule.get("severity", "warning"),
                int(rule.get("cooldown_min", 30)), json.dumps(rule.get("channels") or []), rule.get("description", ""))
        if rule.get("id"):
            self.s.write(lambda c: c.execute(
                "UPDATE alert_rules SET name=?, enabled=?, metric=?, scope=?, op=?, threshold=?, window_min=?, severity=?, "
                "cooldown_min=?, channels=?, description=? WHERE id=?", vals + (int(rule["id"]),)))
            return int(rule["id"])
        return self.s.write(lambda c: c.execute(
            "INSERT INTO alert_rules (name, enabled, metric, scope, op, threshold, window_min, severity, cooldown_min, "
            "channels, description, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", vals + (time.time(),)).lastrowid)

    def delete_rule(self, rid: int) -> None:
        self.s.write(lambda c: (c.execute("DELETE FROM alert_rules WHERE id=?", (rid,)),
                                c.execute("UPDATE alert_events SET state='resolved', resolved=? WHERE rule_id=? AND state='firing'",
                                          (time.time(), rid))))

    # ----------------------------------------------------------- evaluation
    def measure(self, rule: dict, now: int | None = None) -> list[tuple[str, float]]:
        """Return [(scope_key, value)] for the rule's metric over its window."""
        now = now or int(time.time())
        frm = now - int(rule["window_min"]) * 60
        m, sc = rule["metric"], rule["scope"] or {}
        fam, _, stat = m.partition(".")
        app = sc.get("app")

        if fam in ("service", "endpoint", "llm", "mcp", "db") and stat != "slow_queries":
            filt = {"role": "entry"} if fam in ("service", "endpoint") else {"category": fam, "role": "exit"}
            if fam == "mcp":
                filt = {"category": "mcp"}
            by = {"service": ["service"], "endpoint": ["service", "operation"], "llm": ["operation"],
                  "mcp": ["operation"], "db": ["peer"]}[fam]
            for k in ("service", "operation", "peer"):
                if sc.get(k):
                    filt[k] = sc[k]
            rows = self.q.grouped(frm, now, by, app=app, **filt)
            out = []
            for r in rows:
                key = " / ".join(str(r[k]) for k in by)
                if stat == "cost":
                    out.append((key, r["cost"]))
                elif stat == "tokens":
                    out.append((key, r["tokens_in"] + r["tokens_out"]))
                elif r["calls"] >= (1 if stat == "rpm" else 5):  # avoid alerting on 1 failed call of 1
                    out.append((key, r.get(stat) or 0))
            if stat == "rpm" and sc.get("service") and not rows:
                out.append((sc["service"], 0.0))
            return out

        if m == "db.slow_queries":
            p = [self.cfg["thresholds"]["slow_query_ms"], int(frm * 1e9)]
            extra = ""
            if sc.get("peer"):
                extra = " AND peer = ?"; p.append(sc["peer"])
            rows = self.s.q(f"SELECT peer, COUNT(*) AS n FROM spans WHERE category='db' AND duration_ms >= ? "
                            f"AND start_ns >= ? {extra} GROUP BY peer", p)
            return [(r["peer"], float(r["n"])) for r in rows]

        if fam == "host":
            if stat == "unreachable_min":
                rows = self.s.q("SELECT host, last_seen FROM hosts")
                return [(r["host"], round((now - (r["last_seen"] or 0)) / 60, 1)) for r in rows
                        if not sc.get("host") or r["host"] == sc["host"]]
            rows = self.s.q(f"SELECT host, AVG({stat}) AS v FROM host_metrics WHERE ts >= ? GROUP BY host", (frm,))
            return [(r["host"], round(r["v"] or 0, 1)) for r in rows if not sc.get("host") or r["host"] == sc["host"]]

        if fam == "container":
            if stat == "down":
                rows = self.q._latest_containers()
                return [(f"{r['host']} / {r['container']}",
                         1.0 if (r["status"] != "running" or r["health"] == "unhealthy") else 0.0)
                        for r in rows if (not sc.get("container") or r["container"] == sc["container"])
                        and (not sc.get("host") or r["host"] == sc["host"])]
            rows = self.s.q("SELECT host, container, MAX(restarts) - MIN(restarts) AS v FROM container_metrics "
                            "WHERE ts >= ? GROUP BY host, container", (frm,))
            return [(f"{r['host']} / {r['container']}", float(r["v"] or 0)) for r in rows
                    if not sc.get("container") or r["container"] == sc["container"]]

        if m == "check.failed":
            # Failing = every result in the window failed (debounces one blip).
            rows = self.s.q("SELECT name, SUM(ok) AS ok_n, COUNT(*) AS n FROM checks WHERE ts >= ? GROUP BY name", (frm,))
            return [(r["name"], 1.0 if r["ok_n"] == 0 else 0.0) for r in rows if not sc.get("name") or r["name"] == sc["name"]]
        return []

    def threshold_for(self, rule: dict, scope_key: str) -> float:
        """An unscoped latency rule honours a service's own `slow_request_ms`
        (an LLM-backed API is legitimately slower than a CRUD one)."""
        if rule["metric"] in ("service.p95_ms", "service.avg_ms") and not (rule["scope"] or {}).get("service"):
            svc = (self.cfg.get("services") or {}).get(scope_key) or {}
            if svc.get("slow_request_ms"):
                return max(float(rule["threshold"]), float(svc["slow_request_ms"]))
        return float(rule["threshold"])

    def evaluate(self) -> dict:
        now = time.time()
        fired = resolved = 0
        for rule in self.rules():
            if not rule["enabled"]:
                continue
            try:
                values = self.measure(rule)
            except Exception:
                log.exception("rule %s failed to evaluate", rule["name"])
                continue
            unit = METRICS[rule["metric"]][1]
            breaching = {k: v for k, v in values if OPS[rule["op"]](v, self.threshold_for(rule, k))}
            open_ev = {e["scope_key"]: e for e in self.s.q(
                "SELECT * FROM alert_events WHERE rule_id = ? AND state = 'firing'", (rule["id"],))}
            for key, val in breaching.items():
                msg = (f"[{rule['severity'].upper()}] {rule['name']}: {key} — {rule['metric']} = {val:g}{unit if unit != 'bool' else ''} "
                       f"({rule['op']} {rule['threshold']:g}, {rule['window_min']}m window)")
                ev = open_ev.get(key)
                if ev is None:
                    self.s.write(lambda c: c.execute(
                        "INSERT INTO alert_events (rule_id, rule_name, severity, state, scope_key, value, threshold, message, "
                        "started, last_notified) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rule["id"], rule["name"], rule["severity"], "firing", key, val, rule["threshold"], msg, now, now)))
                    self.notify(rule, "FIRING", msg)
                    fired += 1
                else:
                    renotify = now - (ev["last_notified"] or 0) > rule["cooldown_min"] * 60 and not ev["acknowledged"]
                    self.s.write(lambda c: c.execute(
                        "UPDATE alert_events SET value=?, message=?, last_notified=? WHERE id=?",
                        (val, msg, now if renotify else ev["last_notified"], ev["id"])))
                    if renotify:
                        self.notify(rule, "STILL FIRING", msg)
            for key, ev in open_ev.items():
                if key not in breaching:
                    self.s.write(lambda c: c.execute(
                        "UPDATE alert_events SET state='resolved', resolved=? WHERE id=?", (now, ev["id"])))
                    self.notify(rule, "RESOLVED", f"{rule['name']}: {key} back to normal")
                    resolved += 1
        return {"fired": fired, "resolved": resolved}

    def events(self, state: str | None = None, limit: int = 200) -> list[dict]:
        if state:
            return self.s.q("SELECT * FROM alert_events WHERE state = ? ORDER BY started DESC LIMIT ?", (state, limit))
        return self.s.q("SELECT * FROM alert_events ORDER BY started DESC LIMIT ?", (limit,))

    def acknowledge(self, eid: int) -> None:
        self.s.write(lambda c: c.execute("UPDATE alert_events SET acknowledged=1 WHERE id=?", (eid,)))

    # -------------------------------------------------------- notifications
    def notify(self, rule: dict, state: str, text: str) -> None:
        log.warning("ALERT %s %s", state, text)
        n = self.cfg.get("notifications") or {}
        targets = list(rule.get("channels") or []) + list(n.get("webhooks") or [])
        payload = {"text": f"{state}: {text}", "state": state, "rule": rule["name"], "severity": rule["severity"],
                   "metric": rule["metric"], "source": "rapidpulse"}
        for url in targets:
            if url == "email" or not str(url).startswith("http"):
                continue
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:  # notification failure never breaks evaluation
                log.error("webhook %s failed: %s", url, e)
        smtp = n.get("smtp")
        if smtp and smtp.get("host") and smtp.get("to") and ("email" in (rule.get("channels") or []) or smtp.get("all_rules")):
            try:
                msg = EmailMessage()
                msg["Subject"] = f"[RapidPulse] {state}: {rule['name']}"
                msg["From"] = smtp.get("from", "rapidpulse@localhost")
                msg["To"] = ", ".join(smtp["to"])
                msg.set_content(text)
                with smtplib.SMTP(smtp["host"], int(smtp.get("port", 25)), timeout=10) as s:
                    if smtp.get("starttls"):
                        s.starttls()
                    if smtp.get("user"):
                        s.login(smtp["user"], smtp.get("password", ""))
                    s.send_message(msg)
            except Exception as e:
                log.error("smtp notification failed: %s", e)
