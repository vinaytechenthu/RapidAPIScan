#!/usr/bin/env bash
# RapidPulse agent installer for Linux servers (Amazon Linux, RHEL/CentOS/Rocky/Alma, Ubuntu/Debian, SUSE).
#   1. Fill in rapidpulse-agent.conf (next to this file).
#   2. Run:  sudo ./install-agent-linux.sh
# Options:
#   --config <file>   use another settings file
#   --uninstall       stop and remove the agent
#   --show-config     print the settings as the installer reads them (key hidden), change nothing
# Re-running it upgrades the agent and applies changed settings. It also replaces the older
# "pulse-agent" service if that is installed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/rapidpulse-agent.conf"
ACTION=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONF="${2:?--config needs a file}"; shift 2 ;;
    --uninstall) ACTION=uninstall; shift ;;
    --show-config) ACTION=show; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 && $ACTION != show ]]; then
  echo "Administrator rights are needed; re-running with sudo..."
  exec sudo bash "$0" --config "$CONF" $([[ $ACTION == uninstall ]] && echo --uninstall)
fi

SERVICE=rapidpulse-agent
UNIT=/etc/systemd/system/$SERVICE.service
ETC=/etc/rapidpulse-agent
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning: %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ settings
declare -A C=()
CHECKS=()
if [[ -f "$CONF" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"                                   # tolerate a file saved on Windows
    line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    k="${line%%=*}"; v="${line#*=}"
    k="${k%"${k##*[![:space:]]}"}"; k="${k^^}"
    v="${v#"${v%%[![:space:]]*}"}"
    if [[ ${#v} -ge 2 && ( ( "${v:0:1}" == '"' && "${v: -1}" == '"' ) || ( "${v:0:1}" == "'" && "${v: -1}" == "'" ) ) ]]; then v="${v:1:${#v}-2}"; fi
    if [[ "$k" == CHECK ]]; then [[ -n "$v" ]] && CHECKS+=("$v"); else C[$k]="$v"; fi
  done < "$CONF"
elif [[ $ACTION == install ]]; then
  die "Settings file not found: $CONF"
fi
get() { local v="${C[$1]:-}"; echo "${v:-${2:-}}"; }
SERVER="$(get SERVER)"; SERVER="${SERVER%/}"
KEY="$(get KEY)"; APP="$(get APP)"; ROLE="$(get ROLE)"; HOST_NAME="$(get HOST_NAME)"
INTERVAL="$(get INTERVAL 15)"; DOCKER="$(get DOCKER auto)"
DIR="$(get INSTALL_DIR /opt/rapidpulse-agent)"; RUN_AS="$(get RUN_AS root)"
PY="$(get PYTHON)"; PIP_INDEX="$(get PIP_INDEX_URL)"; PROXY="$(get PROXY)"

remove_old() {   # the earlier installer's service name
  if [[ -f /etc/systemd/system/pulse-agent.service ]]; then
    echo "Removing the older 'pulse-agent' service"
    systemctl disable --now pulse-agent >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/pulse-agent.service; rm -rf /opt/pulse-agent
    systemctl daemon-reload
  fi
}

if [[ $ACTION == show ]]; then
  echo "Settings read from $CONF:"
  printf '  %-12s %s
' SERVER "$SERVER" KEY "$([[ -n $KEY ]] && echo "(set, ${#KEY} characters)" || echo "(empty)")"     APP "$APP" ROLE "$ROLE" HOST_NAME "${HOST_NAME:-(this computer host name)}" INTERVAL "$INTERVAL" DOCKER "$DOCKER"     INSTALL_DIR "$DIR" RUN_AS "$RUN_AS" PYTHON "${PY:-(auto)}" PIP_INDEX_URL "${PIP_INDEX:-(none)}" PROXY "${PROXY:-(none)}"
  for c in "${CHECKS[@]+"${CHECKS[@]}"}"; do printf '  %-12s %s
' CHECK "$c"; done
  exit 0
fi
command -v systemctl >/dev/null || die "systemd is required (systemctl not found)."

# ------------------------------------------------------------------ uninstall
if [[ $ACTION == uninstall ]]; then
  step "Removing the RapidPulse agent"
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  rm -f "$UNIT"; rm -rf "$DIR" "$ETC"
  remove_old
  systemctl daemon-reload
  echo "Removed $SERVICE, $DIR and $ETC."
  exit 0
fi

[[ "$SERVER" =~ ^https?://[^/]+ ]] || die "SERVER is not set. Put the RapidPulse address in $CONF, e.g. SERVER=http://10.0.0.5:4400"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "INTERVAL must be a number of seconds."
[[ -n "$APP" ]] || warn "APP is empty: this server will not be grouped under an application."
[[ -n "${C[MSSQL]:-}" ]] && warn "MSSQL is for Windows SQL Server hosts; it is ignored here."
SRV_HOST="$(sed -E 's#^https?://([^/:]+).*#\1#' <<<"$SERVER")"
if [[ -n "$PROXY" ]]; then export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"; fi
export no_proxy="$SRV_HOST,localhost,127.0.0.1${no_proxy:+,$no_proxy}" NO_PROXY="$SRV_HOST,localhost,127.0.0.1${NO_PROXY:+,$NO_PROXY}"
[[ -n "$PIP_INDEX" ]] && export PIP_INDEX_URL="$PIP_INDEX"

# ------------------------------------------------------------------ 1. connection test
step "Testing the connection to RapidPulse at $SERVER"
if command -v curl >/dev/null; then
  curl -fsS --max-time 10 "$SERVER/api/health" >/dev/null 2>&1 || \
    die "Cannot reach $SERVER/api/health. Check SERVER, and allow TCP ${SERVER##*:} from this server ($(hostname -I 2>/dev/null | awk '{print $1}')) in the RapidPulse security group and Windows Firewall."
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST -H 'Content-Type: application/json' \
          -H "x-pulse-key: $KEY" --data '{"resourceSpans":[]}' "$SERVER/v1/traces" || true)"
  [[ "$code" == 401 ]] && die "RapidPulse rejected the key (HTTP 401). Set KEY to the ingest key from C:\\RapidPulse\\config\\pulse.local.yaml on the RapidPulse server."
  echo "Reachable; ingest key accepted."
else
  echo "curl not found; the connection is tested after installation."
fi

# ------------------------------------------------------------------ 2. Python
step "Checking Python (3.8+)"
pm_install() {
  if command -v dnf >/dev/null; then dnf install -y "$@"
  elif command -v yum >/dev/null; then yum install -y "$@"
  elif command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get update -y -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
  elif command -v zypper >/dev/null; then zypper --non-interactive install "$@"
  else die "No supported package manager found. Install python3 (3.8+) with its venv module, or set PYTHON in $CONF."; fi
}
py_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)' >/dev/null 2>&1; }
if [[ -n "$PY" ]]; then
  [[ -x "$PY" ]] || die "PYTHON is set to '$PY' but it is not an executable."
else
  PY="$(command -v python3 || true)"
  if [[ -z "$PY" ]] || ! py_ok "$PY"; then
    echo "Installing python3 ..."
    if command -v apt-get >/dev/null; then pm_install python3 python3-venv; else pm_install python3; fi
    PY="$(command -v python3 || true)"
    if [[ -n "$PY" ]] && ! py_ok "$PY" && command -v apt-get >/dev/null; then   # Debian/Ubuntu split venv per version
      pm_install "python3.$("$PY" -c 'import sys; print(sys.version_info[1])')-venv" || true
    fi
  fi
fi
[[ -n "$PY" ]] && py_ok "$PY" || die "Python 3.8+ with the venv module is not available. Install it (e.g. python3 python3-venv) or set PYTHON in $CONF."
echo "Using $PY ($("$PY" --version 2>&1))"

# ------------------------------------------------------------------ 3. files
step "Installing to $DIR"
systemctl stop "$SERVICE" >/dev/null 2>&1 || true
remove_old
SRC="$HERE/agent/pulse_agent.py"
[[ -f "$SRC" ]] || die "agent/pulse_agent.py not found next to the installer. Copy the 'agent' folder together with this script."
mkdir -p "$DIR" "$ETC"
install -m 0644 "$SRC" "$DIR/pulse_agent.py"
[[ -x "$DIR/venv/bin/python" ]] || "$PY" -m venv "$DIR/venv" || die "Could not create the Python environment in $DIR/venv."
"$DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check psutil==6.1.0 || \
  die "Package install failed: this server needs pypi.org, or PIP_INDEX_URL / PROXY in $CONF."

# account
GROUP_LINE=""
if [[ "$RUN_AS" != root ]]; then
  if ! id "$RUN_AS" >/dev/null 2>&1; then
    echo "Creating system user $RUN_AS"
    useradd --system --no-create-home --shell "$(command -v nologin || echo /sbin/nologin)" "$RUN_AS"
  fi
  if getent group docker >/dev/null; then usermod -aG docker "$RUN_AS"; GROUP_LINE="SupplementaryGroups=docker"; fi
fi

# installed settings: only what the agent needs; readable by root (and the agent's account)
{
  echo "# RapidPulse agent settings (written by the installer $(date -Is)). Edit, then: sudo systemctl restart $SERVICE"
  echo "SERVER=$SERVER"; echo "KEY=$KEY"; echo "APP=$APP"; echo "ROLE=$ROLE"; echo "HOST_NAME=$HOST_NAME"
  echo "INTERVAL=$INTERVAL"; echo "DOCKER=$DOCKER"
  for c in "${CHECKS[@]+"${CHECKS[@]}"}"; do echo "CHECK=$c"; done
} > "$ETC/agent.conf"
if [[ "$RUN_AS" != root ]]; then chown "root:$(id -gn "$RUN_AS")" "$ETC/agent.conf"; chmod 0640 "$ETC/agent.conf"; else chmod 0600 "$ETC/agent.conf"; fi
if [[ "$DOCKER" != off ]] && ! command -v docker >/dev/null; then
  echo "Docker CLI not found: container metrics are skipped (set DOCKER=off to silence this)."
fi

# ------------------------------------------------------------------ 4. test send
step "Sending a test sample"
RUN=()
[[ "$RUN_AS" != root ]] && RUN=(runuser -u "$RUN_AS" --)
if ! out="$(PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin" \
            "${RUN[@]+"${RUN[@]}"}" "$DIR/venv/bin/python" "$DIR/pulse_agent.py" --config "$ETC/agent.conf" --once 2>&1)"; then
  echo "$out" | tail -20
  die "The test sample was not accepted (see above)."
fi
"$DIR/venv/bin/python" - "$out" <<'PY' || true
import json, sys
text = sys.argv[1]
s = json.loads(text[text.index("{"):])
m = s["metrics"]
print(f"Host '{s['host']}': CPU {m.get('cpu')}%, memory {m.get('mem_pct')}%, {s['containers']} container(s), {len(s['checks'])} check(s)")
for name, ok in s["checks"]:
    print(f"  check {name}: {'OK' if ok else 'FAILING'}")
PY

# ------------------------------------------------------------------ 5. service
step "Registering the systemd service '$SERVICE'"
cat > "$UNIT" <<EOF
[Unit]
Description=RapidPulse infrastructure agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart="$DIR/venv/bin/python" "$DIR/pulse_agent.py" --config "$ETC/agent.conf"
Restart=always
RestartSec=10
$( [[ "$RUN_AS" != root ]] && echo "User=$RUN_AS" )
$GROUP_LINE
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  die "The service did not stay running (see the log above)."
fi

printf '\n\033[32mRapidPulse agent is running.\033[0m\n'
echo "  Reporting to : $SERVER  (application '$APP', role '$ROLE')"
echo "  Settings     : $ETC/agent.conf"
echo "  Status / log : systemctl status $SERVICE   |   journalctl -u $SERVICE -f"
echo "  Uninstall    : sudo $0 --uninstall"
echo "This server appears on the RapidPulse Infrastructure page within about $INTERVAL seconds."
