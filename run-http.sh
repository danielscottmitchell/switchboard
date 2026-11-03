#!/bin/zsh
# Launch the HTTP MCP server with the token pulled from Keychain.
set -e
export IMESSAGE_TOKEN=$(security find-generic-password -s imessage-mcp-token -w)
export IMESSAGE_PUBLIC_HOST="${IMESSAGE_PUBLIC_HOST:?set to the tunnel hostname}"
export IMESSAGE_ALLOW_SEND="${IMESSAGE_ALLOW_SEND:-0}"
export IMESSAGE_PORT="${IMESSAGE_PORT:-8765}"
exec "$(dirname "$0")/.venv/bin/python3" "$(dirname "$0")/http_server.py"
