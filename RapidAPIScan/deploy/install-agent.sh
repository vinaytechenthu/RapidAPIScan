#!/usr/bin/env bash
# Older command-line form of the Linux agent installer, kept for existing scripts:
#   sudo ./deploy/install-agent.sh <server-url> [role] [application] [ingest-key] ["Name=url@service" ...]
# It writes those values to a temporary settings file and runs ../install-agent-linux.sh.
# New installations: fill in rapidpulse-agent.conf and run  sudo ./install-agent-linux.sh
set -euo pipefail
SERVER="${1:?usage: install-agent.sh <server-url> [role] [application] [ingest-key] [checks...]}"
ROLE="${2:-}"; APP="${3:-}"; KEY="${4:-}"; shift $(( $# < 4 ? $# : 4 ))
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"
{ echo "SERVER=$SERVER"; echo "ROLE=$ROLE"; echo "APP=$APP"; echo "KEY=$KEY"; for c in "$@"; do echo "CHECK=$c"; done; } > "$TMP"
bash "$(cd "$(dirname "$0")/.." && pwd)/install-agent-linux.sh" --config "$TMP"
