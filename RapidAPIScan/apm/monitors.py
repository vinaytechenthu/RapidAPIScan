"""Central synthetic monitors: HTTP health endpoints and TCP connectivity.

Run from the APM server itself, so "the service is down" is detected even when
the box running it is down (its own agent would be silent). Configured under
`monitors:` in pulse.yaml:

  - {name: "Orders API health", type: http, target: "http://10.0.1.20:8080/health",
     service: orders-api, expect_status: 200, timeout: 5}
  - {name: "Database port", type: tcp, target: "10.0.1.30:1433", service: "SQL Server (shopdb)"}
"""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request


def run_check(m: dict) -> dict:
    t0 = time.perf_counter()
    ok, msg = False, ""
    timeout = float(m.get("timeout", 5))
    try:
        if m.get("type", "http") == "tcp":
            host, _, port = m["target"].rpartition(":")
            with socket.create_connection((host, int(port)), timeout=timeout):
                ok, msg = True, "connected"
        else:
            req = urllib.request.Request(m["target"], method=m.get("method", "GET"),
                                         headers={"User-Agent": "rapidpulse-monitor"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    code = r.status
                    body = r.read(4096).decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                code, body = e.code, ""
            want = int(m.get("expect_status", 200))
            ok = code == want and (not m.get("expect_text") or m["expect_text"] in body)
            msg = f"HTTP {code}"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:300]
    return {"name": m["name"], "type": m.get("type", "http"), "target": m["target"], "ok": ok,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "message": msg,
            "app": m.get("app"), "service": m.get("service")}


def run_all(monitors: list[dict]) -> list[tuple]:
    ts = int(time.time())
    out = []
    for m in monitors or []:
        r = run_check(m)
        out.append((ts, r["name"], r["type"], r["target"], 1 if r["ok"] else 0, r["latency_ms"], r["message"],
                    r["app"], r["service"], "pulse-server"))
    return out
