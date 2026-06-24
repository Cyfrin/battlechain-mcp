#!/usr/bin/env python3
"""Refresh the vendored battlechain_mcp/deployments.json from Cyfrin/battlechain-lib.

Run after a BattleChain redeploy, then review and commit the resulting diff:

    python3 scripts/sync_deployments.py
    git diff battlechain_mcp/deployments.json
"""

import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/Cyfrin/battlechain-lib/main/deployments.json"
TARGET = Path(__file__).resolve().parent.parent / "battlechain_mcp" / "deployments.json"


def main() -> int:
    print(f"Fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:
        body = resp.read().decode()

    # Validate it parses and has the keys the server relies on before writing.
    import json
    networks = json.loads(body)["networks"]
    for chain in ("626", "627"):
        if chain not in networks or "attackRegistry" not in networks[chain]:
            print(f"error: network {chain} or its attackRegistry is missing", file=sys.stderr)
            return 1

    if not body.endswith("\n"):
        body += "\n"
    TARGET.write_text(body)
    print(f"Wrote {TARGET}")
    print("Review the diff before committing: git diff battlechain_mcp/deployments.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
