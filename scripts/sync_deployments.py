#!/usr/bin/env python3
"""Sync the vendored battlechain_mcp/deployments.json from Cyfrin/battlechain-lib.

The MCP targets the BattleChain testnet (chain 627) only, so this fetches the
upstream registry and writes a testnet-only subset. Run after a redeploy, then
review and commit the resulting diff:

    python3 scripts/sync_deployments.py
    git diff battlechain_mcp/deployments.json

Use --check to verify the vendored copy is in sync without writing (exit 1 on
drift). The drift-check CI workflow runs this mode.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/Cyfrin/battlechain-lib/main/deployments.json"
TARGET = Path(__file__).resolve().parent.parent / "battlechain_mcp" / "deployments.json"
CHAIN_ID = "627"


def _build_subset() -> str:
    """Fetch upstream and return the testnet-only deployments.json as a string."""
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:
        networks = json.loads(resp.read().decode())["networks"]

    net = networks.get(CHAIN_ID)
    if not net or "attackRegistry" not in net or "mockRegistryModerator" not in net:
        raise ValueError(f"testnet {CHAIN_ID} entry is missing required fields")
    explorer = networks.get("_explorer", {}).get(CHAIN_ID)
    if not explorer:
        raise ValueError(f"_explorer entry for {CHAIN_ID} is missing")

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
    return json.dumps(subset, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="exit non-zero if the vendored copy differs from upstream; do not write",
    )
    args = parser.parse_args()

    print(f"Fetching {SOURCE}")
    try:
        latest = _build_subset()
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != latest:
            print(
                "error: vendored deployments.json is out of sync with "
                "Cyfrin/battlechain-lib.\n"
                "Run `python3 scripts/sync_deployments.py`, review the diff, and "
                "commit the change.",
                file=sys.stderr,
            )
            return 1
        print("vendored deployments.json is in sync with upstream")
        return 0

    TARGET.write_text(latest)
    print(f"Wrote {TARGET}")
    print("Review the diff before committing: git diff battlechain_mcp/deployments.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
