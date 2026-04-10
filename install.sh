#!/usr/bin/env bash
set -e

# ── Install the Python package ─────────────────────────────────────────────────
echo "Installing BattleChain..."
pip3 install battlechain-mcp --quiet 2>/dev/null \
  || python3 -m pip install battlechain-mcp --quiet

# ── Find the Claude Desktop config file ───────────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
  CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
else
  CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
fi

mkdir -p "$(dirname "$CONFIG")"

# ── Add the battlechain MCP server entry to the config ────────────────────────
python3 - "$CONFIG" <<'EOF'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
config.setdefault("mcpServers", {})
config["mcpServers"]["battlechain"] = {"command": "battlechain-mcp"}
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
