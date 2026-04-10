#!/usr/bin/env bash
set -e

# ── Install the Python package ─────────────────────────────────────────────────
echo "Installing BattleChain..."
pip3 install battlechain-mcp --quiet 2>/dev/null \
  || python3 -m pip install battlechain-mcp --quiet

# ── Resolve config path and MCP command ───────────────────────────────────────
# WSL: Claude Desktop runs on Windows, so we write to the Windows AppData path
# and invoke the command via `wsl battlechain-mcp`.
if grep -qi microsoft /proc/version 2>/dev/null; then
  # powershell.exe is more reliable than cmd.exe for resolving Windows env vars from WSL
  WINDOWS_APPDATA="$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath("ApplicationData")' 2>/dev/null | tr -d '\r\n')"
  if [[ -z "$WINDOWS_APPDATA" ]]; then
    echo "Error: could not resolve Windows AppData path from WSL."
    echo "Please run the install script from a native macOS or Linux terminal, or manually add the config."
    exit 1
  fi
  CONFIG="$(wslpath "$WINDOWS_APPDATA")/Claude/claude_desktop_config.json"
  MCP_COMMAND="wsl"
  MCP_ARGS='["battlechain-mcp"]'
elif [[ "$OSTYPE" == "darwin"* ]]; then
  CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  MCP_COMMAND="battlechain-mcp"
  MCP_ARGS='[]'
else
  CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
  MCP_COMMAND="battlechain-mcp"
  MCP_ARGS='[]'
fi

mkdir -p "$(dirname "$CONFIG")"

# ── Add the battlechain MCP server entry to the config ────────────────────────
python3 - "$CONFIG" "$MCP_COMMAND" "$MCP_ARGS" <<'EOF'
import json, sys
from pathlib import Path

path    = Path(sys.argv[1])
command = sys.argv[2]
args    = json.loads(sys.argv[3])

config = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
config.setdefault("mcpServers", {})

entry = {"command": command}
if args:
    entry["args"] = args
config["mcpServers"]["battlechain"] = entry

path.write_text(json.dumps(config, indent=2))
EOF

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "Done!"
echo ""
echo "  1. Restart Claude Desktop"
echo "  2. Paste this into the chat:"
echo ""
echo "     Run the BattleChain security demo. Walk me through the whole"
echo "     thing step by step and ask me to confirm before the attack."
echo "     I have MetaMask installed."
echo ""
