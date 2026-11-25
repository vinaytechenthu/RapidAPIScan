"""RapidPulse infrastructure agent — one per server (Windows or Linux EC2).

Collects every --interval seconds and POSTs to <server>/api/ingest/infra:
  * host: CPU, memory, every disk volume, network throughput, TCP connections, processes
  * EC2 identity (instance id/type/region via IMDSv2), OS and version
  * Docker containers (Linux container hosts): status, health, CPU, memory, restarts, network
  * local health checks (HTTP / TCP) — e.g. a container's /health, SQL Server's port
  * optional SQL Server collector (needs `pyodbc` + an ODBC driver): connections,
    batch requests/s, blocked processes, page life expectancy, top waits

    python pulse_agent.py --server http://pulse:4400 --role app --app my-application \
        --check "Orders API health=http://localhost:8080/health@orders-api"
    python pulse_agent.py --server http://pulse:4400 --role database \
        --mssql "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;Trusted_Connection=yes;TrustServerCertificate=yes"

    python pulse_agent.py --config /etc/rapidpulse-agent/agent.conf     # settings from a file (see below)

Settings file (what the installers write; KEY=VALUE, '#' comments, CHECK may repeat):
    SERVER=http://10.0.0.5:4400   KEY=...   APP=my-application   ROLE=app   HOST_NAME=   INTERVAL=15
    CHECK=Orders API health=http://localhost:8080/health@orders-api   DOCKER=auto|off   MSSQL=<odbc string>
Command-line options override the file.

Dependencies: psutil (required). Docker metrics use the docker CLI if present.
Install as a Windows service with NSSM / `sc.exe`, or as a systemd unit (see docs).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psutil

VERSION = "1.0.0"


# ----------------------------------------------------------------- identity
def ec2_identity(timeout=0.5) -> dict:
    """IMDSv2 lookup; returns {} off EC2 without slowing startup."""
    try:
        tok_req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                         headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        token = urllib.request.urlopen(tok_req, timeout=timeout).read().decode()
        doc_req = urllib.request.Request("http://169.254.169.254/latest/dynamic/instance-identity/document",
                                         headers={"X-aws-ec2-metadata-token": token})
        doc = json.loads(urllib.request.urlopen(doc_req, timeout=timeout).read())
        return {"instance_id": doc.get("instanceId"), "instance_type": doc.get("instanceType"),
                "region": doc.get("region"), "ip": doc.get("privateIp")}
    except Exception:
        return {}


def host_info(args) -> dict:
    ec2 = ec2_identity()
    os_name = "windows" if sys.platform.startswith("win") else "linux" if sys.platform.startswith("linux") else sys.platform
    try:
        ip = ec2.get("ip") or socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = ""
    return {"name": args.host_name or socket.gethostname(), "os": os_name,
            "os_version": f"{platform.system()} {platform.release()} ({platform.version()})"[:120],
            "role": args.role, "app": args.app, "ip": ip, "instance_id": ec2.get("instance_id"),
            "instance_type": ec2.get("instance_type"), "region": ec2.get("region"),
            "cpus": psutil.cpu_count(), "mem_total": round(psutil.virtual_memory().total / 1e9, 1),
            "agent_version": VERSION}


# ------------------------------------------------------------------ metrics
class Collector:
    def __init__(self):
        self.last_net = psutil.net_io_counters()
        self.last_t = time.time()
        psutil.cpu_percent(None)  # prime

    def host(self) -> dict:
        now = time.time()
        net = psutil.net_io_counters()
        dt = max(1e-6, now - self.last_t)
        net_in = (net.bytes_recv - self.last_net.bytes_recv) / dt
        net_out = (net.bytes_sent - self.last_net.bytes_sent) / dt
        self.last_net, self.last_t = net, now
        disks = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or not p.fstype or p.mountpoint.startswith(("/snap", "/boot/efi")):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({"mount": p.mountpoint, "pct": u.percent, "used": u.used, "total": u.total})
            except (PermissionError, OSError):
                continue
        vm = psutil.virtual_memory()
        try:
            conns = len(psutil.net_connections(kind="tcp"))
        except (psutil.AccessDenied, PermissionError):
            conns = None
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
        return {"cpu": psutil.cpu_percent(None), "mem_pct": vm.percent, "mem_used": vm.used,
                "disk_pct": max((d["pct"] for d in disks), default=None), "disks": disks,
                "net_in": round(net_in, 1), "net_out": round(net_out, 1), "load1": load1,
                "procs": len(psutil.pids()), "tcp_conns": conns}


def docker_containers() -> list[dict]:
    """Container status + stats via the docker CLI (no SDK dependency)."""
    if not shutil.which("docker"):
        return []
    try:
        ps = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"], capture_output=True, text=True, timeout=15)
        rows = [json.loads(l) for l in ps.stdout.splitlines() if l.strip()]
        stats = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True, text=True, timeout=30)
        st = {}
        for l in stats.stdout.splitlines():
            if l.strip():
                s = json.loads(l)
                st[s.get("Name")] = s
        ids = [r["ID"] for r in rows]
        insp = {}
        if ids:
            out = subprocess.run(["docker", "inspect", "--format",
                                  "{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}"] + ids,
                                 capture_output=True, text=True, timeout=15)
            for l in out.stdout.splitlines():
                name, status, health, restarts = (l.split("|") + ["", "", "", "0"])[:4]
                insp[name.lstrip("/")] = (status, health, int(restarts or 0))
    except Exception as e:
        print("docker collection failed:", e, file=sys.stderr)
        return []

    def pct(v):
        try:
            return float(str(v).rstrip("%"))
        except ValueError:
            return None

    def size(v):
        v = str(v).strip()
        units = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
        for u in sorted(units, key=len, reverse=True):
            if v.endswith(u):
                try:
                    return float(v[: -len(u)]) * units[u]
                except ValueError:
                    return None
        return None

    out = []
    for r in rows:
        name = r.get("Names")
        status, health, restarts = insp.get(name, (r.get("State", ""), "", 0))
        s = st.get(name, {})
        net = (s.get("NetIO") or "0B / 0B").split("/")
        out.append({"name": name, "image": r.get("Image"), "status": status, "health": health or None,
                    "cpu": pct(s.get("CPUPerc", 0)), "mem_pct": pct(s.get("MemPerc", 0)),
                    "mem_used": size((s.get("MemUsage") or "0B").split("/")[0]), "restarts": restarts,
                    "net_in": size(net[0]), "net_out": size(net[1]) if len(net) > 1 else None})
    return out


# ------------------------------------------------------------------- checks
def parse_check(spec: str) -> dict:
    """'Name=http://host:port/path@service' or 'Name=tcp://host:1433@service'."""
    name, _, rest = spec.partition("=")
    target, _, service = rest.partition("@")
    typ = "tcp" if target.startswith("tcp://") else "http"
    return {"name": name.strip(), "type": typ, "target": target.replace("tcp://", ""), "service": service or None}


def run_check(c: dict) -> dict:
    t0 = time.perf_counter()
    ok, msg = False, ""
    try:
        if c["type"] == "tcp":
            host, _, port = c["target"].rpartition(":")
            with socket.create_connection((host, int(port)), timeout=5):
                ok, msg = True, "connected"
        else:
            try:
                with urllib.request.urlopen(urllib.request.Request(c["target"], headers={"User-Agent": "pulse-agent"}), timeout=5) as r:
                    ok, msg = 200 <= r.status < 400, f"HTTP {r.status}"
            except urllib.error.HTTPError as e:
                msg = f"HTTP {e.code}"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:300]
    return {**c, "ok": ok, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "message": msg}


# --------------------------------------------------------------- SQL Server
MSSQL_COUNTERS = {
    "User Connections": ("mssql.user_connections", "{connections}"),
    "Batch Requests/sec": ("mssql.batch_requests_per_sec", "1/s"),
    "Processes blocked": ("mssql.blocked_processes", "1"),
    "Page life expectancy": ("mssql.page_life_expectancy", "s"),
    "SQL Compilations/sec": ("mssql.compilations_per_sec", "1/s"),
}


class MssqlCollector:
    def __init__(self, conn_str: str):
        import pyodbc  # optional dependency
        self.pyodbc, self.conn_str = pyodbc, conn_str
        self.prev: dict[str, tuple[float, float]] = {}

    def collect(self) -> tuple[list[dict], dict]:
        t0 = time.perf_counter()
        try:
            with self.pyodbc.connect(self.conn_str, timeout=5) as cn:
                cur = cn.cursor()
                names = ",".join(f"'{n}'" for n in MSSQL_COUNTERS)
                cur.execute(f"SELECT RTRIM(counter_name), cntr_value, cntr_type FROM sys.dm_os_performance_counters "
                            f"WHERE RTRIM(counter_name) IN ({names}) AND (instance_name = '' OR instance_name LIKE '%Buffer%' OR object_name LIKE '%Buffer Manager%' OR object_name LIKE '%General Statistics%' OR object_name LIKE '%SQL Statistics%')")
                now = time.time()
                out = {}
                for name, value, ctype in cur.fetchall():
                    metric, unit = MSSQL_COUNTERS[name]
                    if ctype == 272696576:  # cumulative per-second counter -> rate
                        prev = self.prev.get(metric)
                        self.prev[metric] = (now, float(value))
                        if not prev:
                            continue
                        value = (float(value) - prev[1]) / max(1e-6, now - prev[0])
                    out[metric] = {"name": metric, "value": float(value), "unit": unit, "service": "SQL Server"}
                cur.execute("SELECT TOP 1 wait_type, wait_time_ms FROM sys.dm_os_wait_stats "
                            "WHERE wait_type NOT LIKE 'SLEEP%' AND wait_type NOT LIKE '%QUEUE%' ORDER BY wait_time_ms DESC")
                row = cur.fetchone()
                metrics = list(out.values())
                if row:
                    metrics.append({"name": "mssql.top_wait_ms", "value": float(row[1]), "unit": "ms",
                                    "service": "SQL Server", "attrs": {"wait_type": row[0]}})
            check = {"name": "SQL Server login", "type": "sql", "target": "local", "service": "SQL Server",
                     "ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "message": "query ok"}
            return metrics, check
        except Exception as e:
            return [], {"name": "SQL Server login", "type": "sql", "target": "local", "service": "SQL Server",
                        "ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "message": str(e)[:300]}


# --------------------------------------------------------------------- main
def read_settings(path: str) -> dict:
    """KEY=VALUE lines; '#' starts a comment line; CHECK may appear many times. Unknown keys are ignored."""
    out: dict = {"CHECK": []}
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().upper(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k == "CHECK":
                if v:
                    out["CHECK"].append(v)
            else:
                out[k] = v
    return out


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    cfg_path = pre.parse_known_args()[0].config
    ap = argparse.ArgumentParser(description="RapidPulse infrastructure agent")
    ap.add_argument("--config", help="settings file (KEY=VALUE), e.g. the one the installers write")
    ap.add_argument("--server", default=os.environ.get("PULSE_URL", "http://127.0.0.1:4400"))
    ap.add_argument("--key", default=os.environ.get("PULSE_KEY"), help="ingest API key")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--role", default=os.environ.get("PULSE_ROLE", ""), help="web | app | database | ...")
    ap.add_argument("--app", default=os.environ.get("PULSE_APP", ""), help="application name (service.namespace)")
    ap.add_argument("--host-name", default=os.environ.get("PULSE_HOST_NAME"), help="override the reported host name")
    ap.add_argument("--check", action="append", default=[], help="Name=http://url@service or Name=tcp://host:port@service")
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--mssql", default=os.environ.get("PULSE_MSSQL"), help="ODBC connection string for SQL Server metrics")
    ap.add_argument("--once", action="store_true", help="collect and send once, then exit (for testing)")
    if cfg_path:
        f = read_settings(cfg_path)
        defaults = {"server": f.get("SERVER"), "key": f.get("KEY"), "role": f.get("ROLE"), "app": f.get("APP"),
                    "host_name": f.get("HOST_NAME"), "mssql": f.get("MSSQL"), "check": list(f["CHECK"])}
        if f.get("INTERVAL"):
            defaults["interval"] = int(f["INTERVAL"])
        if f.get("DOCKER", "").lower() in ("off", "no", "false", "0"):
            defaults["no_docker"] = True
        ap.set_defaults(**{k: v for k, v in defaults.items() if v not in (None, "")})
    a = ap.parse_args()

    info = host_info(a)
    col = Collector()
    checks = [parse_check(c) for c in a.check]
    mssql = None
    if a.mssql:
        try:
            mssql = MssqlCollector(a.mssql)
        except ImportError:
            print("pyodbc not installed — SQL Server metrics disabled (pip install pyodbc)", file=sys.stderr)
    print(f"pulse-agent {VERSION} on {info['name']} ({info['os']}) -> {a.server} every {a.interval}s", flush=True)
    time.sleep(1 if not a.once else 1)
    while True:
        t0 = time.time()
        payload = {"ts": int(t0), "host": info, "metrics": col.host(),
                   "containers": [] if a.no_docker else docker_containers(),
                   "checks": [run_check(c) for c in checks], "custom": []}
        if mssql:
            metrics, chk = mssql.collect()
            payload["custom"] += metrics
            payload["checks"].append(chk)
        sent = True
        try:
            req = urllib.request.Request(a.server.rstrip("/") + "/api/ingest/infra", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", **({"X-Pulse-Key": a.key} if a.key else {})},
                                         method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            sent = False
            print(f"{time.strftime('%H:%M:%S')} send failed: {e}", file=sys.stderr, flush=True)
        if a.once:
            print(json.dumps({"host": info["name"], "containers": len(payload["containers"]),
                              "checks": [(c["name"], c["ok"]) for c in payload["checks"]], "metrics": payload["metrics"]}, indent=1))
            sys.exit(0 if sent else 1)
        time.sleep(max(1.0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
