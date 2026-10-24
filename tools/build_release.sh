#!/usr/bin/env bash
# Build dist/grandma3-ai-plugin.zip (the stable release asset name).
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="grandma3-ai-plugin"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME/tools" "$ROOT/dist"
cd "$ROOT"
cp grandma3_ai_assistant.lua grandma3_ai_assistant.xml companion_server.py mvr.py patterns.py \
   noconfirm.py run_mcp_server.sh start_grandma3_mcp.sh README.md api_index.json "$STAGE/$NAME/"
cp tools/sync_api.py "$STAGE/$NAME/tools/"
chmod +x "$STAGE/$NAME/"*.sh
rm -f "dist/$NAME.zip"
(cd "$STAGE" && zip -qr -X "$ROOT/dist/$NAME.zip" "$NAME")
unzip -l "dist/$NAME.zip"
