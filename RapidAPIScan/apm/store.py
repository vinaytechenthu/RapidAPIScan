"""SQLite storage (WAL). One writer connection behind a lock, one reader per thread.

Three tiers, like commercial APMs:
  * raw      — every span / log line, short retention (default 72 h): trace search & waterfall
  * rollups  — 1-minute aggregates with latency histograms, long retention (90 d): dashboards,
               percentiles, history, alerting
  * edges    — 1-minute service-to-service call aggregates: the service map

SQLite keeps the platform zero-dependency for a single EC2 box. The schema is
plain SQL so it moves to PostgreSQL/Timescale unchanged when volume demands it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

from .semantics import latency_bucket

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
  trace_id TEXT, span_id TEXT, parent_id TEXT, app TEXT, env TEXT, service TEXT, service_version TEXT,
  host TEXT, container TEXT, name TEXT, kind TEXT, start_ns INTEGER, end_ns INTEGER, duration_ms REAL,
  status TEXT, category TEXT, operation TEXT, peer TEXT, peer_type TEXT, is_entry INTEGER,
  http_method TEXT, http_route TEXT, http_status INTEGER, db_system TEXT, db_statement TEXT,
  llm_model TEXT, llm_provider TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
  error_type TEXT, error_message TEXT, attributes TEXT, resource TEXT, events TEXT, scope TEXT,
  ingested_at REAL
);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id, span_id);
CREATE INDEX IF NOT EXISTS ix_spans_start ON spans(start_ns);
CREATE INDEX IF NOT EXISTS ix_spans_cat ON spans(category, start_ns);
CREATE INDEX IF NOT EXISTS ix_spans_svc ON spans(service, start_ns);

CREATE TABLE IF NOT EXISTS traces (
  trace_id TEXT PRIMARY KEY, app TEXT, env TEXT, start_ns INTEGER, end_ns INTEGER,
  span_count INTEGER, error_count INTEGER, services TEXT,
  root_service TEXT, root_name TEXT, root_status TEXT, http_status INTEGER, first_service TEXT
);
CREATE INDEX IF NOT EXISTS ix_traces_start ON traces(start_ns);

CREATE TABLE IF NOT EXISTS rollups (
  minute INTEGER, app TEXT, service TEXT, category TEXT, role TEXT, operation TEXT, peer TEXT,
  cnt INTEGER, errors INTEGER, sum_ms REAL, max_ms REAL, buckets TEXT, codes TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost REAL,
  PRIMARY KEY (minute, app, service, category, role, operation, peer)
);
CREATE INDEX IF NOT EXISTS ix_rollups_min ON rollups(minute);

CREATE TABLE IF NOT EXISTS edges (
  minute INTEGER, app TEXT, src TEXT, dst TEXT, dst_type TEXT,
  cnt INTEGER, errors INTEGER, sum_ms REAL,
  PRIMARY KEY (minute, app, src, dst, dst_type)
);

CREATE TABLE IF NOT EXISTS logs (
  ts_ns INTEGER, trace_id TEXT, span_id TEXT, app TEXT, service TEXT, host TEXT,
  severity TEXT, body TEXT, attributes TEXT
);
CREATE INDEX IF NOT EXISTS ix_logs_ts ON logs(ts_ns);
CREATE INDEX IF NOT EXISTS ix_logs_trace ON logs(trace_id);

CREATE TABLE IF NOT EXISTS metric_points (
  ts INTEGER, app TEXT, service TEXT, host TEXT, name TEXT, unit TEXT, value REAL, attrs TEXT
);
CREATE INDEX IF NOT EXISTS ix_metric_points ON metric_points(name, ts);

CREATE TABLE IF NOT EXISTS hosts (
  host TEXT PRIMARY KEY, os TEXT, os_version TEXT, role TEXT, ip TEXT, instance_id TEXT, instance_type TEXT,
  region TEXT, cpus INTEGER, mem_total REAL, agent_version TEXT, app TEXT, last_seen REAL, meta TEXT
);
CREATE TABLE IF NOT EXISTS host_metrics (
  ts INTEGER, host TEXT, cpu REAL, mem_pct REAL, mem_used REAL, disk_pct REAL, disks TEXT,
  net_in REAL, net_out REAL, load1 REAL, procs INTEGER, tcp_conns INTEGER
);
CREATE INDEX IF NOT EXISTS ix_host_metrics ON host_metrics(host, ts);
CREATE TABLE IF NOT EXISTS container_metrics (
  ts INTEGER, host TEXT, container TEXT, image TEXT, status TEXT, health TEXT,
  cpu REAL, mem_pct REAL, mem_used REAL, restarts INTEGER, net_in REAL, net_out REAL
);
CREATE INDEX IF NOT EXISTS ix_container_metrics ON container_metrics(host, container, ts);

CREATE TABLE IF NOT EXISTS checks (
  ts INTEGER, name TEXT, type TEXT, target TEXT, ok INTEGER, latency_ms REAL, message TEXT,
  app TEXT, service TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks ON checks(name, ts);

CREATE TABLE IF NOT EXISTS alert_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, enabled INTEGER, metric TEXT, scope TEXT,
  op TEXT, threshold REAL, window_min INTEGER, severity TEXT, cooldown_min INTEGER,
  channels TEXT, description TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, rule_name TEXT, severity TEXT, state TEXT,
  scope_key TEXT, value REAL, threshold REAL, message TEXT, started REAL, resolved REAL,
  last_notified REAL, acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alert_events ON alert_events(state, started);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

SPAN_COLS = ["trace_id", "span_id", "parent_id", "app", "env", "service", "service_version", "host",
             "container", "name", "kind", "start_ns", "end_ns", "duration_ms", "status", "category",
             "operation", "peer", "peer_type", "is_entry", "http_method", "http_route", "http_status",
             "db_system", "db_statement", "llm_model", "llm_provider", "tokens_in", "tokens_out",
             "cost_usd", "error_type", "error_message", "attributes", "resource", "events", "scope"]


def rollup_role(rec: dict) -> str | None:
    """Which spans become time-series: entry (server/root) spans per service, and
    exit spans to databases, LLMs, MCP tools and remote HTTP APIs."""
    if rec["is_entry"]:
        return "entry"
    if rec["category"] in ("db", "llm", "mcp"):
        return "exit"
    if rec["category"] == "http" and rec["kind"] == "client":
        return "exit"
    return None


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.Lock()
        self._w = self._connect()
        self._w.executescript(SCHEMA)
        self._local = threading.local()
        self._agg: dict[tuple, dict] = {}
        self._agg_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        return c

    # ------------------------------------------------------------------ reads
    def r(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def q(self, sql: str, params=()) -> list[dict]:
        return [dict(row) for row in self.r().execute(sql, params).fetchall()]

    def q1(self, sql: str, params=()) -> dict | None:
        row = self.r().execute(sql, params).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- writes
    def write(self, fn):
        with self._wlock:
            self._w.execute("BEGIN")
            try:
                out = fn(self._w)
                self._w.execute("COMMIT")
                return out
            except BaseException:
                self._w.execute("ROLLBACK")
                raise

    def insert_spans(self, recs: list[dict]) -> None:
        if not recs:
            return
        now = time.time()
        rows = [tuple(r.get(c) for c in SPAN_COLS) + (now,) for r in recs]
        trace_rows = []
        for r in recs:
            root = not r["parent_id"]
            trace_rows.append((
                r["trace_id"], r["app"], r["env"], r["start_ns"], r["end_ns"], 1,
                1 if r["status"] == "error" else 0, f",{r['service']},",
                r["service"] if root else None, (r["operation"] or r["name"]) if root else None,
                r["status"] if root else None, r["http_status"] if root else None, r["service"]))

        def tx(c):
            c.executemany(f"INSERT INTO spans ({','.join(SPAN_COLS)}, ingested_at) VALUES "
                          f"({','.join('?' * (len(SPAN_COLS) + 1))})", rows)
            c.executemany("""
              INSERT INTO traces (trace_id, app, env, start_ns, end_ns, span_count, error_count, services,
                                  root_service, root_name, root_status, http_status, first_service)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(trace_id) DO UPDATE SET
                first_service = CASE WHEN excluded.start_ns < traces.start_ns THEN excluded.first_service ELSE traces.first_service END,
                start_ns = MIN(traces.start_ns, excluded.start_ns),
                end_ns = MAX(traces.end_ns, excluded.end_ns),
                span_count = traces.span_count + 1,
                error_count = traces.error_count + excluded.error_count,
                services = CASE WHEN instr(traces.services, excluded.services) > 0 THEN traces.services
                                ELSE traces.services || substr(excluded.services, 2) END,
                root_service = COALESCE(excluded.root_service, traces.root_service),
                root_name = COALESCE(excluded.root_name, traces.root_name),
                root_status = COALESCE(excluded.root_status, traces.root_status),
                http_status = COALESCE(excluded.http_status, traces.http_status)
            """, trace_rows)
        self.write(tx)

        with self._agg_lock:
            for r in recs:
                role = rollup_role(r)
                if not role:
                    continue
                minute = int(r["start_ns"] // 60_000_000_000) * 60
                key = (minute, r["app"], r["service"], r["category"], role,
                       (r["operation"] or r["name"])[:300], r["peer"] or "")
                a = self._agg.get(key)
                if a is None:
                    a = self._agg[key] = {"cnt": 0, "errors": 0, "sum": 0.0, "max": 0.0,
                                          "b": defaultdict(int), "codes": defaultdict(int),
                                          "tin": 0, "tout": 0, "cost": 0.0}
                a["cnt"] += 1
                a["errors"] += r["status"] == "error"
                a["sum"] += r["duration_ms"]
                a["max"] = max(a["max"], r["duration_ms"])
                a["b"][latency_bucket(r["duration_ms"])] += 1
                if r["http_status"]:
                    a["codes"][str(r["http_status"])] += 1
                a["tin"] += r["tokens_in"] or 0
                a["tout"] += r["tokens_out"] or 0
                a["cost"] += r["cost_usd"] or 0.0

    def flush_rollups(self) -> int:
        with self._agg_lock:
            pending, self._agg = self._agg, {}
        if not pending:
            return 0

        def tx(c):
            for key, a in pending.items():
                row = c.execute("SELECT cnt, errors, sum_ms, max_ms, buckets, codes, tokens_in, tokens_out, cost "
                                "FROM rollups WHERE minute=? AND app=? AND service=? AND category=? AND role=? "
                                "AND operation=? AND peer=?", key).fetchone()
                b, codes = dict(a["b"]), dict(a["codes"])
                cnt, err, s, mx, tin, tout, cost = a["cnt"], a["errors"], a["sum"], a["max"], a["tin"], a["tout"], a["cost"]
                if row:
                    for k, v in json.loads(row["buckets"]).items():
                        b[int(k)] = b.get(int(k), 0) + v
                    for k, v in json.loads(row["codes"]).items():
                        codes[k] = codes.get(k, 0) + v
                    cnt += row["cnt"]; err += row["errors"]; s += row["sum_ms"]; mx = max(mx, row["max_ms"])
                    tin += row["tokens_in"]; tout += row["tokens_out"]; cost += row["cost"]
                c.execute("INSERT OR REPLACE INTO rollups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          key + (cnt, err, s, mx, json.dumps(b), json.dumps(codes), tin, tout, cost))
        self.write(tx)
        return len(pending)

    # ------------------------------------------------------- service-map linker
    def link_edges(self, settle_seconds: float = 20.0) -> int:
        """Derive service-to-service and service-to-dependency edges.

        Children usually export *before* their parents (they finish first), so
        edges are computed after a settle delay rather than at ingest time.
        """
        last = int((self.q1("SELECT v FROM kv WHERE k='linker_rowid'") or {"v": 0})["v"])
        hi = self.q1("SELECT MAX(rowid) AS m FROM spans WHERE ingested_at < ?", (time.time() - settle_seconds,))
        hi_id = int((hi or {}).get("m") or 0)
        if hi_id <= last:
            return 0
        # (1) cross-service parent->child: the child's entry span is the call
        cross = self.q("""
          SELECT c.start_ns, c.app, p.service AS src, c.service AS dst, c.status, c.duration_ms
          FROM spans c JOIN spans p ON p.trace_id = c.trace_id AND p.span_id = c.parent_id
          WHERE c.rowid > ? AND c.rowid <= ? AND c.service <> p.service""", (last, hi_id))
        # (2) exit spans to an uninstrumented dependency (no child in another service)
        exits = self.q("""
          SELECT s.start_ns, s.app, s.service AS src, s.peer AS dst, s.peer_type AS dst_type,
                 s.status, s.duration_ms
          FROM spans s
          WHERE s.rowid > ? AND s.rowid <= ? AND s.peer IS NOT NULL AND s.peer <> ''
            AND s.peer_type IN ('database','llm','mcp','http') AND s.is_entry = 0
            AND NOT EXISTS (SELECT 1 FROM spans ch WHERE ch.trace_id = s.trace_id
                            AND ch.parent_id = s.span_id AND ch.service <> s.service)""", (last, hi_id))
        agg: dict[tuple, list] = defaultdict(lambda: [0, 0, 0.0])
        for e in cross:
            k = (int(e["start_ns"] // 60_000_000_000) * 60, e["app"], e["src"], e["dst"], "service")
            agg[k][0] += 1; agg[k][1] += e["status"] == "error"; agg[k][2] += e["duration_ms"]
        for e in exits:
            k = (int(e["start_ns"] // 60_000_000_000) * 60, e["app"], e["src"], e["dst"], e["dst_type"])
            agg[k][0] += 1; agg[k][1] += e["status"] == "error"; agg[k][2] += e["duration_ms"]

        def tx(c):
            for k, (cnt, err, s) in agg.items():
                c.execute("""INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)
                             ON CONFLICT(minute, app, src, dst, dst_type) DO UPDATE SET
                               cnt = cnt + excluded.cnt, errors = errors + excluded.errors,
                               sum_ms = sum_ms + excluded.sum_ms""", k + (cnt, err, s))
            c.execute("INSERT OR REPLACE INTO kv VALUES ('linker_rowid', ?)", (str(hi_id),))
        self.write(tx)
        return len(agg)

    # ------------------------------------------------------------------ other
    def insert_logs(self, rows: list[tuple]) -> None:
        if rows:
            self.write(lambda c: c.executemany("INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?)", rows))

    def insert_metric_points(self, rows: list[tuple]) -> None:
        if rows:
            self.write(lambda c: c.executemany("INSERT INTO metric_points VALUES (?,?,?,?,?,?,?,?)", rows))

    def insert_checks(self, rows: list[tuple]) -> None:
        if rows:
            self.write(lambda c: c.executemany("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)", rows))

    def purge(self, cfg: dict) -> None:
        st = cfg["storage"]
        now = time.time()
        raw_ns = int((now - st["raw_retention_hours"] * 3600) * 1e9)
        roll_s = int(now - st["rollup_retention_days"] * 86400)
        infra_s = int(now - st["infra_retention_days"] * 86400)

        def tx(c):
            c.execute("DELETE FROM spans WHERE start_ns < ?", (raw_ns,))
            c.execute("DELETE FROM traces WHERE start_ns < ?", (raw_ns,))
            c.execute("DELETE FROM logs WHERE ts_ns < ?", (raw_ns,))
            c.execute("DELETE FROM rollups WHERE minute < ?", (roll_s,))
            c.execute("DELETE FROM edges WHERE minute < ?", (roll_s,))
            c.execute("DELETE FROM metric_points WHERE ts < ?", (infra_s,))
            c.execute("DELETE FROM host_metrics WHERE ts < ?", (infra_s,))
            c.execute("DELETE FROM container_metrics WHERE ts < ?", (infra_s,))
            c.execute("DELETE FROM checks WHERE ts < ?", (infra_s,))
        self.write(tx)

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                total += os.path.getsize(p)
        return total
