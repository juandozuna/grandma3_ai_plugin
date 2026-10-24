#!/usr/bin/env bash
set -Eeuo pipefail

# Start the grandMA3 onPC MCP server locally.
# The grandMA3 plugin must already be running with: Plugin <n> "Start"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MCP_SERVER="$SCRIPT_DIR/companion_server.py"
LOCAL_LAUNCHER="$SCRIPT_DIR/run_mcp_server.sh"
PROFILE="grandma3-local"
TUNNEL_ID="${TUNNEL_ID:-${TUNNEL_ID_ENV:-}}"
PLUGIN_HOST="127.0.0.1"
PLUGIN_PORT="17844"
DOCTOR_ONLY=0
SKIP_BRIDGE_CHECK=0
USE_TUNNEL=0

usage() {
    cat <<'EOF'
Usage: start_grandma3_mcp.sh [options]

Verify and run the grandMA3 MCP server locally. The local mode does not need
an OpenAI Control Plane API key, tunnel, or internet connection.
The grandMA3 plugin must already be running in onPC with Plugin <n> "Start".

Options:
  --tunnel             use OpenAI Secure MCP Tunnel instead of local mode
  --tunnel-id ID       OpenAI tunnel ID (required with --tunnel)
  --profile NAME       tunnel-client profile name (default: grandma3-local)
  --plugin-port PORT   grandMA3 bridge port (default: 17844)
  --skip-bridge-check  do not check 127.0.0.1 before starting the tunnel
  --doctor-only        validate the selected mode, then stop
  -h, --help           show this help

Local mode launches companion_server.py over stdio for a local MCP client.
Tunnel mode reads the control-plane key from the terminal or
CONTROL_PLANE_API_KEY and never writes it to disk.
EOF
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --tunnel-id) [[ $# -ge 2 ]] || die "--tunnel-id requires a value"; TUNNEL_ID="$2"; shift 2 ;;
        --tunnel) USE_TUNNEL=1; shift ;;
        --profile) [[ $# -ge 2 ]] || die "--profile requires a value"; PROFILE="$2"; shift 2 ;;
        --plugin-port) [[ $# -ge 2 ]] || die "--plugin-port requires a value"; PLUGIN_PORT="$2"; shift 2 ;;
        --skip-bridge-check) SKIP_BRIDGE_CHECK=1; shift ;;
        --doctor-only) DOCTOR_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -f "$MCP_SERVER" ]] || die "MCP server not found: $MCP_SERVER"
[[ -x "$LOCAL_LAUNCHER" ]] || die "local launcher is not executable: $LOCAL_LAUNCHER"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi
[[ -n "$PYTHON_BIN" ]] || die "python3 is required"

if (( SKIP_BRIDGE_CHECK == 0 )); then
    PLUGIN_PORT="$PLUGIN_PORT" PLUGIN_HOST="$PLUGIN_HOST" "$PYTHON_BIN" - <<'PY' >&2
import os, socket, sys
host = os.environ["PLUGIN_HOST"]
port = int(os.environ["PLUGIN_PORT"])
try:
    with socket.create_connection((host, port), timeout=1):
        pass
except OSError:
    print(f"grandMA3 bridge is not reachable at {host}:{port}. Run Plugin <n> \\\"Start\\\" in onPC first.", file=sys.stderr)
    sys.exit(2)
print(f"grandMA3 bridge reachable at {host}:{port}")
PY
fi

if (( USE_TUNNEL == 0 )); then
    if (( DOCTOR_ONLY == 1 )); then
        printf 'Local MCP server is ready. Start it with: %q\n' "$LOCAL_LAUNCHER"
        exit 0
    fi
    GRANDMA3_AI_PLUGIN_HOST="$PLUGIN_HOST" GRANDMA3_AI_PLUGIN_PORT="$PLUGIN_PORT" exec "$LOCAL_LAUNCHER"
fi

[[ -n "$TUNNEL_ID" ]] || die "--tunnel requires --tunnel-id or TUNNEL_ID"
command -v tunnel-client >/dev/null 2>&1 || die "tunnel-client is not installed or is not on PATH"

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
    read -r -s -p "OpenAI CONTROL_PLANE_API_KEY: " CONTROL_PLANE_API_KEY
    printf '\n'
    [[ -n "$CONTROL_PLANE_API_KEY" ]] || die "a control-plane key is required"
    export CONTROL_PLANE_API_KEY
fi

if ! tunnel-client doctor --profile "$PROFILE" --explain >/dev/null 2>&1; then
    MCP_COMMAND="$PYTHON_BIN $MCP_SERVER"
    tunnel-client init \
        --sample sample_mcp_stdio_local \
        --profile "$PROFILE" \
        --tunnel-id "$TUNNEL_ID" \
        --mcp-command "$MCP_COMMAND"
fi

tunnel-client doctor --profile "$PROFILE" --explain
if (( DOCTOR_ONLY == 1 )); then
    printf 'Profile is ready. Start it later with: tunnel-client run --profile %q\n' "$PROFILE"
    exit 0
fi

exec tunnel-client run --profile "$PROFILE"
