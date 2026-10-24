#!/usr/bin/env bash
set -Eeuo pipefail

# Launch the grandMA3 MCP stdio server for Claude (Claude Code / Claude Desktop).
# Claude starts this script itself; stdout is reserved for MCP protocol traffic,
# so every diagnostic here goes to stderr.
# The grandMA3 plugin must be running in onPC with: Plugin <n> "Start"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MCP_SERVER="$SCRIPT_DIR/companion_server.py"
HOST="${GRANDMA3_AI_PLUGIN_HOST:-127.0.0.1}"
PORT="${GRANDMA3_AI_PLUGIN_PORT:-17844}"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PYTHON_BIN" ]] || { echo "Error: python3 is required" >&2; exit 1; }
[[ -f "$MCP_SERVER" ]] || { echo "Error: MCP server not found: $MCP_SERVER" >&2; exit 1; }

bridge_up() {
    "$PYTHON_BIN" - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1).close()
except OSError:
    sys.exit(1)
PY
}

if [[ "${1:-}" == "--check" ]]; then
    if bridge_up; then
        echo "OK: grandMA3 bridge reachable at $HOST:$PORT"
        exit 0
    fi
    echo "NOT READY: nothing listening at $HOST:$PORT. In onPC run: Plugin <n> \"Start\"" >&2
    exit 2
fi

# Non-fatal: the plugin can be started after Claude connects.
bridge_up || echo "Warning: grandMA3 bridge not reachable at $HOST:$PORT yet (run Plugin <n> \"Start\" in onPC)." >&2

exec "$PYTHON_BIN" "$MCP_SERVER"
