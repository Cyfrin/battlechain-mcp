#!/usr/bin/env python3
"""Refresh the vendored battlechain_mcp/deployments.json from Cyfrin/battlechain-lib.

The MCP targets the BattleChain testnet (chain 627) only, so this fetches the
upstream registry and writes a testnet-only subset. Run after a redeploy, then
review and commit the resulting diff:

    python3 scripts/sync_deployments.py
    git diff battlechain_mcp/deployments.json
"""

import json
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/Cyfrin/battlechain-lib/main/deployments.json"
TARGET = Path(__file__).resolve().parent.parent / "battlechain_mcp" / "deployments.json"
CHAIN_ID = "627"


def main() -> int:
    print(f"Fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:
        upstream = json.loads(resp.read().decode())

    networks = upstream["networks"]
    net = networks.get(CHAIN_ID)
    if not net or "attackRegistry" not in net or "mockRegistryModerator" not in net:
        print(f"error: testnet {CHAIN_ID} entry is missing required fields", file=sys.stderr)
        return 1
    explorer = networks.get("_explorer", {}).get(CHAIN_ID)
    if not explorer:
        print(f"error: _explorer entry for {CHAIN_ID} is missing", file=sys.stderr)
        return 1

    subset = {
        "_comment": (
            "Testnet-only subset of Cyfrin/battlechain-lib deployments.json. "
            "Refresh with scripts/sync_deployments.py. The BattleChain MCP targets "
            "testnet (chain 627) exclusively."
        ),
        "networks": {
            CHAIN_ID: net,
            "_explorer": {CHAIN_ID: explorer},
        },
    }

    TARGET.write_text(json.dumps(subset, indent=2) + "\n")
    print(f"Wrote {TARGET}")
    print("Review the diff before committing: git diff battlechain_mcp/deployments.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
