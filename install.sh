#!/usr/bin/env bash
set -e

# ── Find Python 3.10+ ─────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" &>/dev/null; then
    version=$("$candidate" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
    if [[ "$version" == "True" ]]; then
      # Also verify pip is functional for this Python (e.g. py3.12 system pip
      # on Ubuntu can be broken due to missing distutils)
      if "$candidate" -m pip --version &>/dev/null 2>&1; then
        PYTHON="$candidate"
        break
      fi
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "Error: Python 3.10 or higher is required but not found."
  echo "Install it with: sudo apt install python3.11  (or brew install python@3.11 on macOS)"
  exit 1
fi

# ── Install the Python package ─────────────────────────────────────────────────
echo "Installing BattleChain..."
"$PYTHON" -m pip install "git+https://github.com/Cyfrin/battlechain-mcp.git" --quiet

# ── Resolve config path and MCP command ───────────────────────────────────────
# WSL: Claude Desktop runs on Windows, so we write to the Windows AppData path
# and invoke the command via `wsl battlechain-mcp`.
if grep -qi microsoft /proc/version 2>/dev/null; then
  # Try several methods to resolve the Windows AppData path from WSL
  APPDATA_WSL=""

  # Method 1: wslvar (available when wslu package is installed)
  if command -v wslvar &>/dev/null; then
    _raw="$(wslvar APPDATA 2>/dev/null | tr -d '\r\n')"
    [[ -n "$_raw" ]] && APPDATA_WSL="$(wslpath "$_raw" 2>/dev/null)"
  fi

  # Method 2: powershell.exe via $env:APPDATA
  if [[ -z "$APPDATA_WSL" ]] && command -v powershell.exe &>/dev/null; then
    _raw="$(powershell.exe -NoProfile -NonInteractive -Command 'Write-Output $env:APPDATA' 2>/dev/null | tr -d '\r\n')"
    [[ -n "$_raw" ]] && APPDATA_WSL="$(wslpath "$_raw" 2>/dev/null)"
  fi

  # Method 3: cmd.exe USERNAME → construct path manually
  if [[ -z "$APPDATA_WSL" ]] && command -v cmd.exe &>/dev/null; then
    _user="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r\n')"
    [[ -n "$_user" ]] && APPDATA_WSL="/mnt/c/Users/$_user/AppData/Roaming"
  fi

  if [[ -z "$APPDATA_WSL" ]]; then
    echo ""
    echo "Could not auto-detect your Windows AppData path."
    echo "Manually add this to: %APPDATA%\\Claude\\claude_desktop_config.json"
    echo ""
    echo '  { "mcpServers": { "battlechain": { "command": "wsl", "args": ["battlechain-mcp"] } } }'
    echo ""
    echo "Then restart Claude Desktop."
    exit 0
  fi

  CONFIG="$APPDATA_WSL/Claude/claude_desktop_config.json"
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
"$PYTHON" - "$CONFIG" "$MCP_COMMAND" "$MCP_ARGS" <<'EOF'
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
