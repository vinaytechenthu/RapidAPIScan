#!/usr/bin/env python3
"""Turn on RapidPulse / OpenTelemetry tracing for a RUNNING Python container, without touching its code.

Every HTTP request the container serves becomes a trace (method, route, status, duration), together
with the calls it makes (httpx/requests, SQLAlchemy, Redis, ...). It works for any Python web
framework that OpenTelemetry supports (FastAPI, Flask, Django, ...).

    sudo python3 deploy/instrument-container.py <container> --server http://<rapidpulse>:4400 \
         --key <ingest-key> --app <application> [--service <name>] [--env <environment>]

What it does:
  1. Reads the container's settings (image, command, env, networks + aliases, ports, volumes,
     restart policy, health check, limits ...) with `docker inspect`.
  2. Builds <image> + OpenTelemetry (integrations/python-docker/Dockerfile). It matches the
     OpenTelemetry version already in the image, so the app's own packages are not upgraded.
  3. Keeps the original container stopped as "<container>-pre-otel" and starts an identical one
     from the new image, with the OTEL_* settings pointing at RapidPulse.
  4. Waits until the new container is healthy. If it isn't, it restores the original automatically.

Other actions:
  --status       show whether the container is instrumented
  --restore      go back to the original container (removes the instrumented one)
  --dry-run      print what would be built and run; change nothing
Re-run the same command to change the server, key or names: it always rebuilds from the original.
If the application's own deploy script recreates the container, simply run this again afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LABEL = "rapidpulse.instrumented"
SUFFIX = "-pre-otel"
TEMPLATE = Path(__file__).resolve().parent.parent / "integrations" / "python-docker" / "Dockerfile"
STATE_DIR = Path(os.environ.get("RAPIDPULSE_STATE_DIR", "/var/lib/rapidpulse/instrumented"))
DEFAULT_SHM = 64 * 1024 * 1024


class Failed(Exception):
    pass


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(args: list[str], *, check: bool = True, quiet: bool = True) -> str:
    r = subprocess.run(args, text=True, capture_output=quiet)
    if check and r.returncode:
        raise Failed(f"{shlex.join(args)}\n{(r.stderr or '').strip()}")
    return (r.stdout or "").strip() if quiet else ""


def inspect(ref: str, kind: str = "container") -> dict | None:
    r = subprocess.run(["docker", kind, "inspect", ref], text=True, capture_output=True)
    return json.loads(r.stdout)[0] if r.returncode == 0 else None


def restart_policy(spec: dict) -> str:
    rp = spec["HostConfig"].get("RestartPolicy") or {}
    name = rp.get("Name") or "no"
    if name == "on-failure" and rp.get("MaximumRetryCount"):
        return f"on-failure:{rp['MaximumRetryCount']}"
    return name


# ---------------------------------------------------------------- docker create from inspect
def create_args(spec: dict, image_cfg: dict, name: str, image: str, env_file: str) -> tuple[list[str], list[list[str]]]:
    """`docker create` arguments that reproduce `spec` (a container inspect) on `image`, plus the
    `docker network connect` commands for any extra networks."""
    cfg, hc = spec["Config"], spec["HostConfig"]
    a = ["docker", "create", "--name", name, "--label", f"{LABEL}=1", "--env-file", env_file]

    img_labels = image_cfg.get("Labels") or {}
    for k, v in (cfg.get("Labels") or {}).items():
        if img_labels.get(k) != v and k != LABEL:
            a += ["--label", f"{k}={v}"]
    if (cfg.get("User") or "") != (image_cfg.get("User") or ""):
        a += ["--user", cfg["User"] or "root"]
    if cfg.get("WorkingDir") and cfg.get("WorkingDir") != image_cfg.get("WorkingDir"):
        a += ["--workdir", cfg["WorkingDir"]]
    if cfg.get("Hostname") and not spec["Id"].startswith(cfg["Hostname"]):
        a += ["--hostname", cfg["Hostname"]]
    if cfg.get("StopSignal") and cfg.get("StopSignal") != image_cfg.get("StopSignal"):
        a += ["--stop-signal", cfg["StopSignal"]]
    if cfg.get("StopTimeout") is not None:
        a += ["--stop-timeout", str(cfg["StopTimeout"])]

    hcheck = cfg.get("Healthcheck")
    if hcheck and hcheck != image_cfg.get("Healthcheck"):
        test = hcheck.get("Test") or []
        if test and test[0] == "NONE":
            a += ["--no-healthcheck"]
        elif test:
            a += ["--health-cmd", test[1] if test[0] == "CMD-SHELL" else shlex.join(test[1:])]
            for key, flag in (("Interval", "interval"), ("Timeout", "timeout"), ("StartPeriod", "start-period")):
                if hcheck.get(key):
                    a += [f"--health-{flag}", f"{int(hcheck[key] / 1_000_000)}ms"]
            if hcheck.get("Retries"):
                a += ["--health-retries", str(hcheck["Retries"])]

    policy = restart_policy(spec)
    if policy != "no":
        a += ["--restart", policy]

    # networks: the first one on `docker create`, the rest with `docker network connect`
    connects: list[list[str]] = []
    mode = hc.get("NetworkMode") or "default"
    nets = (spec.get("NetworkSettings") or {}).get("Networks") or {}
    short_id = spec["Id"][:12]

    def net_opts(net: str, connect: bool) -> list[str]:
        n = nets.get(net) or {}
        out = []
        for alias in n.get("Aliases") or []:
            if alias != short_id and not spec["Id"].startswith(alias):
                out += ["--alias" if connect else "--network-alias", alias]
        ip = (n.get("IPAMConfig") or {}).get("IPv4Address")
        if ip:
            out += ["--ip", ip]
        return out

    if mode in ("host", "none") or mode.startswith("container:"):
        a += ["--network", mode]
    else:
        primary = mode if mode in nets else ("bridge" if mode == "default" and "bridge" in nets else next(iter(nets), "bridge"))
        a += ["--network", primary] + (net_opts(primary, False) if primary != "bridge" else [])
        for net in nets:
            if net != primary:
                connects.append(["docker", "network", "connect", *net_opts(net, True), net, name])

    for cport, binds in (hc.get("PortBindings") or {}).items():
        for b in binds or [{}]:
            hip, hport = b.get("HostIp") or "", b.get("HostPort") or ""
            a += ["-p", f"{hip + ':' if hip else ''}{hport + ':' if hport else ''}{cport}"]
    if hc.get("PublishAllPorts"):
        a += ["-P"]

    for m in spec.get("Mounts") or []:
        ro = "" if m.get("RW", True) else ":ro"
        if m.get("Type") == "bind":
            a += ["-v", f"{m['Source']}:{m['Destination']}{ro}"]
        elif m.get("Type") == "volume":
            a += ["-v", f"{m['Name']}:{m['Destination']}{ro}"]
    for path, opts in (hc.get("Tmpfs") or {}).items():
        a += ["--tmpfs", f"{path}:{opts}" if opts else path]

    for flag, key in (("--add-host", "ExtraHosts"), ("--cap-add", "CapAdd"), ("--cap-drop", "CapDrop"),
                      ("--dns", "Dns"), ("--dns-search", "DnsSearch"), ("--dns-option", "DnsOptions"),
                      ("--group-add", "GroupAdd"), ("--security-opt", "SecurityOpt")):
        for v in hc.get(key) or []:
            a += [flag, v]
    for d in hc.get("Devices") or []:
        a += ["--device", f"{d['PathOnHost']}:{d['PathInContainer']}:{d.get('CgroupPermissions') or 'rwm'}"]
    for u in hc.get("Ulimits") or []:
        a += ["--ulimit", f"{u['Name']}={u['Soft']}:{u['Hard']}"]
    for k, v in (hc.get("Sysctls") or {}).items():
        a += ["--sysctl", f"{k}={v}"]
    if hc.get("Privileged"):
        a += ["--privileged"]
    if hc.get("ReadonlyRootfs"):
        a += ["--read-only"]
    if hc.get("Init"):
        a += ["--init"]
    for flag, key in (("--memory", "Memory"), ("--memory-reservation", "MemoryReservation"),
                      ("--memory-swap", "MemorySwap"), ("--cpu-shares", "CpuShares"), ("--pids-limit", "PidsLimit")):
        if (hc.get(key) or 0) > 0:
            a += [flag, str(hc[key])]
    if hc.get("NanoCpus"):
        a += ["--cpus", f"{hc['NanoCpus'] / 1e9:g}"]
    if hc.get("ShmSize") and hc["ShmSize"] != DEFAULT_SHM:
        a += ["--shm-size", str(hc["ShmSize"])]
    if hc.get("IpcMode") and hc["IpcMode"] not in ("private", "shareable"):
        a += ["--ipc", hc["IpcMode"]]
    if hc.get("PidMode"):
        a += ["--pid", hc["PidMode"]]
    if hc.get("Runtime") and hc["Runtime"] != "runc":
        a += ["--runtime", hc["Runtime"]]
    lc = hc.get("LogConfig") or {}
    if lc.get("Type") and (lc["Type"] != "json-file" or lc.get("Config")):
        a += ["--log-driver", lc["Type"]]
        for k, v in (lc.get("Config") or {}).items():
            a += ["--log-opt", f"{k}={v}"]
    return a + [image], connects


# ---------------------------------------------------------------- environment
def user_env(spec: dict, image_cfg: dict) -> dict[str, str]:
    """Variables set for this container (env files, -e), not the image defaults."""
    defaults = set(image_cfg.get("Env") or [])
    out = {}
    for e in spec["Config"].get("Env") or []:
        if e not in defaults and "=" in e:
            k, v = e.split("=", 1)
            out[k] = v
    return out


def otel_env(o: argparse.Namespace, name: str) -> dict[str, str]:
    q = lambda s: urllib.parse.quote(str(s), safe="")
    res = {"service.namespace": o.app, "deployment.environment.name": o.env,
           "host.name": o.host_name or socket.gethostname(), "container.name": name}
    for kv in o.resource:
        k, _, v = kv.partition("=")
        res[k] = v
    env = {
        "OTEL_SERVICE_NAME": o.service or name,
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{k}={q(v)}" for k, v in res.items() if v),
        "OTEL_EXPORTER_OTLP_ENDPOINT": o.server.rstrip("/"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "otlp" if o.logs else "none",
    }
    if o.logs:
        env["OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED"] = "true"
    if o.key:
        env["OTEL_EXPORTER_OTLP_HEADERS"] = f"x-pulse-key={o.key}"
    # Request capture (headers + payload on each server span; secrets masked in the container).
    env["RAPIDPULSE_CAPTURE_BODY_BYTES"] = str(max(0, o.capture_body_bytes))
    if o.no_capture_headers:
        env["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST"] = ""
    for kv in o.extra_env:
        k, _, v = kv.partition("=")
        env[k] = v
    return env


def check_server(o: argparse.Namespace) -> None:
    base = o.server.rstrip("/")
    try:
        urllib.request.urlopen(f"{base}/api/health", timeout=8).read()
    except Exception as e:  # noqa: BLE001
        raise Failed(f"This host cannot reach RapidPulse at {base} ({e}).\n"
                     f"Allow TCP {urllib.parse.urlparse(base).port or 80} from this host in the "
                     "RapidPulse security group / Windows Firewall, or pass --skip-checks.")
    req = urllib.request.Request(f"{base}/v1/traces", data=b'{"resourceSpans":[]}', method="POST",
                                 headers={"Content-Type": "application/json", **({"x-pulse-key": o.key} if o.key else {})})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failed("RapidPulse rejected the ingest key (HTTP 401). Use the key from "
                         "C:\\RapidPulse\\config\\pulse.local.yaml (security.ingest_keys).")
        raise Failed(f"RapidPulse ingest test failed: HTTP {e.code}")
    log(f"RapidPulse reachable at {base}, ingest key accepted")


# ---------------------------------------------------------------- lifecycle
def wait_ready(name: str, timeout: int) -> None:
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(3)
        c = inspect(name)
        if not c:
            raise Failed("container disappeared")
        st = c["State"]
        if st.get("Status") in ("exited", "dead") or st.get("Restarting") or c.get("RestartCount", 0) > 0:
            raise Failed(f"container stopped (status {st.get('Status')}, exit code {st.get('ExitCode')})")
        health = (st.get("Health") or {}).get("Status")
        if health == "healthy":
            return
        if health == "unhealthy":
            raise Failed("container reported unhealthy")
        if health is None and time.time() - start > 20:
            return
    raise Failed(f"not healthy after {timeout}s")


def state_file(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def restart_dependents(names: list[str]) -> None:
    """Reverse proxies (nginx ...) resolve the container's IP once at start; the new container has a new IP."""
    for d in names:
        if inspect(d):
            log(f"Restarting '{d}' so it picks up the new container")
            run(["docker", "restart", d], check=False)
        else:
            print(f"   (no container '{d}' to restart)")


def restore(name: str, quiet: bool = False, dependents: list[str] | None = None) -> None:
    backup = f"{name}{SUFFIX}"
    if not inspect(backup):
        die(f"No original container '{backup}' to restore.")
    saved = state_file(name)
    policy = restart_policy(json.loads(saved.read_text())) if saved.exists() else "unless-stopped"
    cur = inspect(name)
    if cur:
        run(["docker", "rm", "-f", name])
    run(["docker", "rename", backup, name])
    run(["docker", "update", f"--restart={policy}", name])
    run(["docker", "start", name])
    restart_dependents(dependents or [])
    if not quiet:
        saved.unlink(missing_ok=True)
        log(f"Restored the original '{name}' (restart policy {policy}).")


def status(name: str) -> None:
    c = inspect(name)
    if not c:
        die(f"No container '{name}'.")
    env = dict(e.split("=", 1) for e in c["Config"].get("Env") or [] if "=" in e)
    on = (c["Config"].get("Labels") or {}).get(LABEL) == "1"
    print(f"{name}: {'INSTRUMENTED' if on else 'not instrumented'}  image={c['Config']['Image']}  "
          f"state={c['State']['Status']}/{(c['State'].get('Health') or {}).get('Status', '-')}")
    if on:
        for k in ("OTEL_SERVICE_NAME", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_RESOURCE_ATTRIBUTES"):
            print(f"  {k}={env.get(k, '')}")
        print(f"  original kept as: {name}{SUFFIX} ({'present' if inspect(name + SUFFIX) else 'MISSING'})")


def instrument(o: argparse.Namespace) -> None:
    name = o.container
    cur = inspect(name)
    if not cur:
        names = run(["docker", "ps", "-a", "--format", "{{.Names}}"], check=False).split()
        words = [w for w in re.split(r"[-_.]", name.lower()) if len(w) > 2]
        close = [n for n in names if sum(w in n.lower() for w in words) >= max(1, len(words) - 1)]
        die(f"No container '{name}'." + (f" Did you mean: {', '.join(close)}?" if close else "")
            + "\n       Running containers: " + (", ".join(names) or "none"))
    backup = f"{name}{SUFFIX}"
    already = (cur["Config"].get("Labels") or {}).get(LABEL) == "1"
    if already:
        if not state_file(name).exists() or not inspect(backup):
            die(f"'{name}' is already instrumented but its original ('{backup}' / {state_file(name)}) is missing.")
        spec = json.loads(state_file(name).read_text())
        log(f"'{name}' is already instrumented: rebuilding from the original settings")
    else:
        spec = cur
        if inspect(backup):  # left over from before the application was redeployed
            log(f"Removing stale '{backup}' from an earlier run")
            if not o.dry_run:
                run(["docker", "rm", "-f", backup])

    image = inspect(spec["Image"], "image")
    if not image:
        die(f"The original image {spec['Config']['Image']} ({spec['Image'][:19]}) is no longer on this host.")
    icfg = image["Config"] or {}
    safe = re.sub(r"[^a-z0-9_.-]", "-", name.lower()).strip("-.") or "app"
    base_tag, otel_tag = f"rapidpulse-otel/{safe}:base", f"rapidpulse-otel/{safe}:latest"

    entry = spec["Config"].get("Entrypoint") or []
    cmd = spec["Config"].get("Cmd") or []
    if entry[:1] == ["opentelemetry-instrument"]:
        entry = entry[1:]
    dockerfile = (TEMPLATE.read_text(encoding="utf-8")
                  + f"\n# added by instrument-container.py for '{name}'\n"
                  + f"ENTRYPOINT {json.dumps(['opentelemetry-instrument', *entry])}\n"
                  + f"CMD {json.dumps(cmd)}\n")

    env = user_env(spec, icfg)
    for k in [k for k in env if k.startswith(("OTEL_", "RAPIDPULSE_CAPTURE"))]:
        del env[k]
    env.update(otel_env(o, name))

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "otel.env"
        multiline = {k: v for k, v in env.items() if "\n" in v}
        env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items() if k not in multiline), encoding="utf-8")
        os.chmod(env_file, 0o600)
        create, connects = create_args(spec, icfg, name, otel_tag, str(env_file))
        for k, v in multiline.items():
            create[2:2] = ["-e", f"{k}={v}"]

        print(f"\nService '{env['OTEL_SERVICE_NAME']}'  application '{o.app}'  -> {o.server}")
        print(f"Original image : {spec['Config']['Image']}  (command: {shlex.join(entry + cmd)})")
        print(f"New image      : {otel_tag}")
        if o.dry_run:
            print("\n--- Dockerfile tail ---\n" + dockerfile.split("ARG BASE", 1)[-1].strip())
            print("\n--- would run ---\n" + shlex.join(create))
            for c in connects:
                print(shlex.join(c))
            print("\n--- OTEL settings ---")
            for k, v in env.items():
                if k.startswith("OTEL_"):
                    print(f"{k}={'***' if k == 'OTEL_EXPORTER_OTLP_HEADERS' else v}")
            return

        if not o.skip_checks:
            check_server(o)

        log("Building the instrumented image (needs PyPI or --pip-index-url) ...")
        run(["docker", "tag", spec["Image"], base_tag])
        (Path(tmp) / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        shutil.copy(TEMPLATE.with_name("rapidpulse_capture.py"), Path(tmp) / "rapidpulse_capture.py")
        os.chmod(Path(tmp) / "rapidpulse_capture.py", 0o644)     # the app may run as a non-root user
        build = ["docker", "build", "-t", otel_tag, "--label", f"{LABEL}=1",
                 "--build-arg", f"BASE={base_tag}", "--build-arg", f"RUN_AS={icfg.get('User') or 'root'}"]
        if o.pip_index_url:
            build += ["--build-arg", f"PIP_INDEX_URL={o.pip_index_url}"]
        if o.network_build:
            build += ["--network", o.network_build]
        try:
            run(build + [tmp], quiet=False)
        except Failed as e:
            die(f"Image build failed; nothing was changed.\n{e}")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not already:
            state_file(name).write_text(json.dumps(spec), encoding="utf-8")
            os.chmod(state_file(name), 0o600)
            log(f"Keeping the original container as '{backup}' (stopped)")
            run(["docker", "rename", name, backup])
            run(["docker", "update", "--restart=no", backup])
            run(["docker", "stop", "-t", "30", backup])
        else:
            run(["docker", "rm", "-f", name])

        try:
            log("Starting the instrumented container")
            run(create)
            for c in connects:
                run(c)
            run(["docker", "start", name])
            log(f"Waiting up to {o.timeout}s for it to become healthy ...")
            wait_ready(name, o.timeout)
            restart_dependents(o.restart_after)
        except Failed as e:
            print(f"\nThe instrumented container did not start correctly: {e}", file=sys.stderr)
            subprocess.run(["docker", "logs", "--tail", "40", name])
            log("Rolling back to the original container")
            restore(name, quiet=True, dependents=o.restart_after)
            die("Rolled back; the application is running exactly as before.")

    log(f"Done. '{name}' now sends traces to RapidPulse as service '{env['OTEL_SERVICE_NAME']}'.")
    print(f"""
  Check:    send any request to the API, then open RapidPulse > Traces (service {env['OTEL_SERVICE_NAME']}).
  Status:   sudo python3 {sys.argv[0]} {name} --status
  Undo:     sudo python3 {sys.argv[0]} {name} --restore{''.join(' --restart-after ' + d for d in o.restart_after)}
  Cleanup:  the original is kept stopped as '{backup}'; remove it with 'docker rm {backup}' once you are happy.
  Redeploy: if the application's deploy script recreates '{name}', run this command again.""")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("container", help="name of the running container")
    p.add_argument("--server", help="RapidPulse URL, e.g. http://10.0.0.5:4400")
    p.add_argument("--key", default=os.environ.get("PULSE_KEY", ""), help="ingest key (or env PULSE_KEY)")
    p.add_argument("--app", help="application name (service.namespace) it is grouped under")
    p.add_argument("--service", help="service name shown in RapidPulse (default: the container name)")
    p.add_argument("--env", default="prod", help="environment name (default prod)")
    p.add_argument("--host-name", help="host name reported (default: this machine's hostname, as the agent uses)")
    p.add_argument("--resource", action="append", default=[], metavar="K=V", help="extra resource attribute")
    p.add_argument("--set-env", dest="extra_env", action="append", default=[], metavar="K=V",
                   help="extra environment variable for the container")
    p.add_argument("--restart-after", action="append", default=[], metavar="CONTAINER",
                   help="restart this container afterwards, e.g. an nginx proxy in front (repeatable)")
    p.add_argument("--logs", action="store_true", help="also send application logs (off by default: logs may hold data)")
    p.add_argument("--capture-body-bytes", type=int, default=8192, metavar="N",
                   help="keep up to N bytes of each request payload on its trace (default 8192; 0 = off)")
    p.add_argument("--no-capture-headers", action="store_true", help="do not record request headers on traces")
    p.add_argument("--pip-index-url", help="PyPI mirror/proxy for the image build")
    p.add_argument("--network-build", help="docker build --network (e.g. host, if the build cannot reach PyPI)")
    p.add_argument("--timeout", type=int, default=180, help="seconds to wait for health (default 180)")
    p.add_argument("--skip-checks", action="store_true", help="don't test the RapidPulse connection first")
    p.add_argument("--dry-run", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--restore", action="store_true", help="go back to the original container")
    g.add_argument("--status", action="store_true")
    o = p.parse_args()

    if subprocess.run(["docker", "version"], capture_output=True).returncode:
        die("The docker CLI is not available or this user cannot use it (run with sudo).")
    if o.status:
        return status(o.container)
    if o.restore:
        return restore(o.container, dependents=o.restart_after)
    if not o.server or not o.app:
        die("--server and --app are required.")
    if not TEMPLATE.exists():
        die(f"{TEMPLATE} not found: copy the RapidPulse 'deploy' and 'integrations' folders side by side.")
    try:
        instrument(o)
    except Failed as e:
        die(str(e))


if __name__ == "__main__":
    main()
