#!/usr/bin/env python3
"""
BattleChain MCP Server
Walks a non-technical user through the entire BattleChain security demo via Claude Desktop.
MetaMask handles all transaction signing — no private keys ever touch this server.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# ── Paths & constants ─────────────────────────────────────────────────────────

STARTER_REPO_URL = "https://github.com/Cyfrin/battlechain-starter.git"
BATTLECHAIN_DIR  = Path.home() / ".battlechain"
PROJECT_ROOT     = BATTLECHAIN_DIR / "starter"
ENV_FILE         = PROJECT_ROOT / ".env"
FOUNDRY_BIN      = Path.home() / ".foundry" / "bin"

RPC_URL          = "https://testnet.battlechain.com:3051"
CHAIN_ID         = "627"
EXPLORER_API     = "https://block-explorer-api.testnet.battlechain.com/api"
ATTACK_REGISTRY  = "0x9E62988ccA776ff6613Fa68D34c9AB5431Ce57e1"
MOCK_MODERATOR   = "0x1bC64E6F187a47D136106784f4E9182801535BD3"

FORGE_BROWSER_FLAGS = [
    "--rpc-url", RPC_URL,
    "--broadcast",
    "--browser",
    "--chain", CHAIN_ID,
    "--skip-simulation",
    "--verifier-url", EXPLORER_API,
    "--verifier", "custom",
    "--etherscan-api-key", "1234",
]

AGREEMENT_STATES = {
    "0": "UNREGISTERED",
    "1": "ACTIVE",
    "2": "ATTACK_REQUESTED",
    "3": "UNDER_ATTACK",
    "4": "DISPUTED",
    "5": "PRODUCTION",
}


# ── Foundry PATH helpers ──────────────────────────────────────────────────────

def _ensure_foundry_in_path() -> None:
    """Add ~/.foundry/bin to PATH for this process if it isn't there already."""
    foundry_str = str(FOUNDRY_BIN)
    path = os.environ.get("PATH", "")
    if foundry_str not in path.split(":"):
        os.environ["PATH"] = foundry_str + ":" + path


def _forge_available() -> bool:
    _ensure_foundry_in_path()
    result = subprocess.run(["forge", "--version"], capture_output=True)
    return result.returncode == 0


def _forge_has_browser() -> bool:
    """Check if the installed forge supports the --browser flag (requires nightly >= 1.6.0, 2026-03-10)."""
    _ensure_foundry_in_path()
    result = subprocess.run(["forge", "script", "--help"], capture_output=True, text=True)
    return "--browser" in result.stdout


def _git_available() -> bool:
    result = subprocess.run(["git", "--version"], capture_output=True)
    return result.returncode == 0


def _glibc_version() -> tuple[int, int] | None:
    """Return the host glibc version as (major, minor), or None if undetectable."""
    result = subprocess.run(["ldd", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    m = re.search(r"(\d+)\.(\d+)\s*$", first_line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _run(cmd: list[str], cwd: Path | None = None, extra_env: dict | None = None) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output)."""
    env = os.environ.copy()
    _ensure_foundry_in_path()
    env["PATH"] = os.environ["PATH"]  # pick up any updates from _ensure_foundry_in_path
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


# ── .env helpers ──────────────────────────────────────────────────────────────

def read_env() -> dict[str, str]:
    """Read key=value pairs from the project .env file."""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


def write_env_values(updates: dict[str, str]) -> None:
    """Update specific key=value lines in .env, preserving comments and structure."""
    if not ENV_FILE.exists():
        example = PROJECT_ROOT / ".env.example"
        ENV_FILE.write_text(example.read_text() if example.exists() else "")

    lines = ENV_FILE.read_text().splitlines()
    updated: set[str] = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        new_lines.append(line)

    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n")


def _subprocess_env() -> dict[str, str]:
    """OS env + .env values so forge scripts can read all required variables."""
    env = os.environ.copy()
    _ensure_foundry_in_path()
    env["PATH"] = os.environ["PATH"]
    env.update(read_env())
    return env


# ── Forge/cast runners ────────────────────────────────────────────────────────

def forge_browser(script_path: str) -> tuple[int, str]:
    """Run a forge script with --browser wallet signing.
    Blocks until the user approves all MetaMask transactions."""
    cmd = ["forge", "script", script_path, *FORGE_BROWSER_FLAGS]
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=_subprocess_env()
    )
    return result.returncode, result.stdout + result.stderr


def cast_call(address: str, sig: str, *args: str) -> tuple[int, str]:
    """Read-only cast call against the testnet."""
    cmd = ["cast", "call", address, sig, *args, "--rpc-url", RPC_URL]
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=_subprocess_env()
    )
    return result.returncode, (result.stdout or result.stderr).strip()


# ── Output parsers ────────────────────────────────────────────────────────────

def parse_address(output: str, key: str) -> str | None:
    m = re.search(rf"{re.escape(key)}[=:\s]+\s*(0x[0-9a-fA-F]{{40}})", output)
    return m.group(1) if m else None


def parse_number(output: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*:?\s*(\d+)", output)
    return m.group(1) if m else None


# ── Prerequisite guard ────────────────────────────────────────────────────────

def missing_keys(keys: list[str]) -> list[str]:
    env = read_env()
    return [k for k in keys if not env.get(k)]


# ── MCP server ────────────────────────────────────────────────────────────────

server = Server("battlechain")


DEMO_PROMPT = """\
You have access to battlechain tools. Use them now — do not search the web or read files.

CRITICAL: All battlechain tools execute commands on the USER'S LOCAL MACHINE via MCP. \
They do NOT run inside Claude's environment. \
If a tool returns an error, it is an error on the user's machine — not a Claude limitation. \
Never say "my sandbox" or "my environment" — always say "your machine".

Run the full BattleChain security demo by calling tools in this order:
1. Call `prepare_environment` immediately to set up the user's machine.
2. Call `deploy_contracts` (Step 1).
3. Call `create_agreement` (Step 2).
4. Call `request_and_approve_attack_mode` (Step 3).
5. Before calling `execute_attack`, explain to the user in plain English what is about to happen \
and ask them to confirm. Only proceed once they say yes.
6. Call `execute_attack` with their wallet address (Step 4).
7. Show a clean summary of what happened.

After each tool call, explain what just happened in plain English — no jargon. \
Tell the user what MetaMask will ask them to do before each signing step.\
"""


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="start",
            description="Start the BattleChain security demo",
            arguments=[],
        )
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    return types.GetPromptResult(
        description="BattleChain demo",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=DEMO_PROMPT),
            )
        ],
    )


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="prepare_environment",
            description=(
                "ALWAYS call this first before any other tool. "
                "IMPORTANT: This tool executes shell commands on the USER'S LOCAL MACHINE via MCP — "
                "not in Claude's environment. Any errors it returns are happening on the user's machine. "
                "Checks whether Foundry (forge/cast) is installed on the user's machine and installs it "
                "if missing. Then downloads the BattleChain demo project and compiles the smart contracts. "
                "This may take a couple of minutes on first run. "
                "Reports clearly what was installed/found and whether the environment is ready."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_status",
            description=(
                "Show a plain-English summary of where the user is in the demo flow: "
                "which contracts are deployed, which steps are complete, and what comes next. "
                "Call this any time to orient the user."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="deploy_contracts",
            description=(
                "Step 1 of 4. Deploy the demo smart contracts (MockToken and VulnerableVault) "
                "to the BattleChain testnet and seed the vault with 1,000 tokens. "
                "This opens MetaMask in the user's browser — tell them to approve the transactions when prompted. "
                "Requires prepare_environment to have succeeded first."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="create_agreement",
            description=(
                "Step 2 of 4. Create a Safe Harbor security agreement and register it on the BattleChain registry. "
                "This is the legal framework that makes the upcoming attack a legitimate whitehat engagement. "
                "Opens MetaMask — tell the user to approve. "
                "Requires Step 1 (deploy_contracts) to be complete."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="request_and_approve_attack_mode",
            description=(
                "Step 3 of 4. Submit the attack mode request and immediately approve it via the testnet moderator. "
                "On testnet anyone can approve, so this skips the normal waiting period. "
                "Opens MetaMask twice in quick succession (once to request, once to approve). "
                "Requires Step 2 (create_agreement) to be complete."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="execute_attack",
            description=(
                "Step 4 of 4 — THE ATTACK. Executes the reentrancy exploit against the vault. "
                "IMPORTANT: Only call this tool AFTER you have explicitly told the user what is about to happen "
                "and they have confirmed they want to proceed. "
                "Explain to the user: the vault holds 1,000 tokens; the attack will drain it completely; "
                "90% will be returned to their wallet as protocol recovery, 10% kept as the whitehat bounty. "
                "Once the user confirms, call this tool with their wallet address. "
                "Opens MetaMask for final signing. "
                "Requires Step 3 (request_and_approve_attack_mode) to be complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wallet_address": {
                        "type": "string",
                        "description": (
                            "The user's Ethereum wallet address (starts with 0x). "
                            "This receives both the 10% bounty and the 90% recovery. "
                            "It must match the MetaMask wallet they will use to sign."
                        ),
                    }
                },
                "required": ["wallet_address"],
            },
        ),
        types.Tool(
            name="check_agreement_state",
            description=(
                "Query the on-chain state of the Safe Harbor agreement at any point. "
                "Returns a human-readable status: ACTIVE, ATTACK_REQUESTED, UNDER_ATTACK, or PRODUCTION. "
                "No MetaMask required — this is a read-only check."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    # ── prepare_environment ───────────────────────────────────────────────────
    if name == "prepare_environment":
        steps = []

        # 1. Git
        if not _git_available():
            return [types.TextContent(type="text", text=(
                "SETUP ERROR: git is not installed in WSL.\n\n"
                "Open a WSL terminal and run:\n\n"
                "    sudo apt-get install -y git\n\n"
                "Then come back and try again."
            ))]
        steps.append("git: found")

        # 2. Foundry — must be nightly >= 1.6.0 (2026-03-10) for --browser support.
        needs_install = not _forge_available()
        needs_upgrade = not needs_install and not _forge_has_browser()

        if needs_install or needs_upgrade:
            # Foundry's prebuilt binaries require glibc 2.34+ (Ubuntu 22.04+).
            # Check before attempting install so we give a clear actionable error.
            glibc = _glibc_version()
            if glibc is not None and glibc < (2, 34):
                return [types.TextContent(type="text", text=(
                    f"SETUP ERROR: Your WSL Ubuntu is too old to run Foundry.\n\n"
                    f"Your WSL glibc version: {glibc[0]}.{glibc[1]}\n"
                    f"Required:               2.34 or higher (Ubuntu 22.04+)\n\n"
                    "To fix this, open PowerShell on Windows and run:\n\n"
                    "    wsl --install -d Ubuntu-22.04\n\n"
                    "This installs Ubuntu 22.04 alongside your current WSL without removing anything. "
                    "Once installed, set it as default with:\n\n"
                    "    wsl --set-default Ubuntu-22.04\n\n"
                    "Then re-run the BattleChain installer in PowerShell and try again."
                ))]

            if needs_install:
                steps.append("Foundry: not found — installing...")
                rc, out = _run(
                    ["bash", "-c", "curl -L https://foundry.paradigm.xyz | bash -s -- --no-modify-path"],
                )
                if rc != 0:
                    return [types.TextContent(type="text", text=f"Foundry installation failed.\n\n{out}")]
            else:
                steps.append("Foundry: found but needs upgrade for --browser support — updating...")

            # Install/upgrade to latest nightly (required for --browser flag).
            # Use --version nightly explicitly; plain foundryup may default to stable.
            rc, out = _run([str(FOUNDRY_BIN / "foundryup"), "--version", "nightly"])
            if rc != 0:
                # Some older foundryup scripts don't support --version; try bare
                rc, out = _run([str(FOUNDRY_BIN / "foundryup")])
                if rc != 0:
                    return [types.TextContent(type="text", text=f"foundryup failed.\n\n{out}")]

            # Verify the upgrade actually gave us --browser support
            if not _forge_has_browser():
                return [types.TextContent(type="text", text=(
                    "Foundry was upgraded but the installed version still does not support --browser.\n\n"
                    "Please run this manually in WSL, then restart Claude Desktop:\n\n"
                    "    foundryup --version nightly\n\n"
                    "This demo requires forge >= 1.6.0-nightly (2026-03-10)."
                ))]
            steps.append("Foundry: upgraded to nightly")
        else:
            steps.append("Foundry (forge/cast): found")

        # 3. Clone repo
        BATTLECHAIN_DIR.mkdir(parents=True, exist_ok=True)
        if not (PROJECT_ROOT / "foundry.toml").exists():
            steps.append("Downloading BattleChain demo project...")
            rc, out = _run([
                "git", "clone", "--recurse-submodules", STARTER_REPO_URL, str(PROJECT_ROOT)
            ])
            if rc != 0:
                return [types.TextContent(type="text", text=f"Failed to download the demo project.\n\n{out}")]
            steps.append("Demo project: downloaded")
        else:
            steps.append("Demo project: already present")

        # 4. Build contracts
        steps.append("Compiling smart contracts...")
        rc, out = _run(["forge", "build"], cwd=PROJECT_ROOT)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Contract compilation failed.\n\n{out}")]
        steps.append("Smart contracts: compiled successfully")

        return [types.TextContent(type="text", text=(
            "Environment ready!\n\n"
            + "\n".join(f"  ✓ {s}" for s in steps)
            + "\n\nYou're all set. MetaMask will be used for signing transactions — "
            "make sure it's installed in your browser before the next step."
        ))]

    # ── get_status ────────────────────────────────────────────────────────────
    elif name == "get_status":
        env = read_env()

        def status(key: str) -> str:
            val = env.get(key, "")
            return f"✓ {val}" if val else "✗ not set"

        token    = env.get("TOKEN_ADDRESS", "")
        vault    = env.get("VAULT_ADDRESS", "")
        agreement = env.get("AGREEMENT_ADDRESS", "")

        lines = [
            "**BattleChain Demo Status**\n",
            f"  MockToken:          {status('TOKEN_ADDRESS')}",
            f"  VulnerableVault:    {status('VAULT_ADDRESS')}",
            f"  Safe Harbor agreement: {status('AGREEMENT_ADDRESS')}",
            "",
        ]

        if not token or not vault:
            lines.append("Next step: deploy_contracts (Step 1)")
        elif not agreement:
            lines.append("Next step: create_agreement (Step 2)")
        else:
            lines.append("Next steps: request_and_approve_attack_mode (Step 3), then execute_attack (Step 4)")

        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── deploy_contracts ──────────────────────────────────────────────────────
    elif name == "deploy_contracts":
        if not _forge_available():
            return [types.TextContent(type="text", text="Foundry is not installed. Run prepare_environment first.")]

        env = read_env()
        if env.get("TOKEN_ADDRESS") and env.get("VAULT_ADDRESS"):
            return [types.TextContent(type="text", text=(
                "Contracts are already deployed:\n"
                f"  MockToken:       {env['TOKEN_ADDRESS']}\n"
                f"  VulnerableVault: {env['VAULT_ADDRESS']}\n\n"
                "Skipping. Call create_agreement to continue."
            ))]

        rc, out = forge_browser("script/Setup.s.sol")
        if rc != 0:
            return [types.TextContent(type="text", text=f"Deployment failed.\n\n{out}")]

        token = parse_address(out, "TOKEN_ADDRESS")
        vault = parse_address(out, "VAULT_ADDRESS")
        updates = {k: v for k, v in {"TOKEN_ADDRESS": token, "VAULT_ADDRESS": vault}.items() if v}
        if updates:
            write_env_values(updates)

        if token and vault:
            return [types.TextContent(type="text", text=(
                "Step 1 complete!\n\n"
                "Two smart contracts have been deployed to the BattleChain testnet:\n\n"
                f"  MockToken (the demo currency):  {token}\n"
                f"  VulnerableVault (the target):   {vault}\n\n"
                "The vault has been loaded with 1,000 tokens — that's the pot we'll recover later.\n\n"
                "Ready for Step 2: creating the Safe Harbor security agreement."
            ))]
        return [types.TextContent(type="text", text=(
            "Deployment ran but addresses couldn't be parsed from output.\n\n"
            + out
        ))]

    # ── create_agreement ──────────────────────────────────────────────────────
    elif name == "create_agreement":
        missing = missing_keys(["VAULT_ADDRESS"])
        if missing:
            return [types.TextContent(type="text", text="VulnerableVault not deployed yet. Run deploy_contracts first.")]

        env = read_env()
        if env.get("AGREEMENT_ADDRESS"):
            return [types.TextContent(type="text", text=(
                f"Agreement already exists: {env['AGREEMENT_ADDRESS']}\n\n"
                "Skipping. Call request_and_approve_attack_mode to continue."
            ))]

        rc, out = forge_browser("script/CreateAgreement.s.sol")
        if rc != 0:
            return [types.TextContent(type="text", text=f"Agreement creation failed.\n\n{out}")]

        agreement = parse_address(out, "AGREEMENT_ADDRESS")
        if agreement:
            write_env_values({"AGREEMENT_ADDRESS": agreement})
            return [types.TextContent(type="text", text=(
                "Step 2 complete!\n\n"
                "A Safe Harbor security agreement has been created and registered on-chain.\n"
                "This is the legal framework that makes the upcoming attack an authorized whitehat engagement "
                "rather than a hack — the protocol has officially invited a security researcher to test it.\n\n"
                f"  Agreement address: {agreement}\n\n"
                "Ready for Step 3: requesting and approving attack mode."
            ))]
        return [types.TextContent(type="text", text=(
            "Agreement creation ran but the address couldn't be parsed.\n\n" + out
        ))]

    # ── request_and_approve_attack_mode ───────────────────────────────────────
    elif name == "request_and_approve_attack_mode":
        missing = missing_keys(["AGREEMENT_ADDRESS"])
        if missing:
            return [types.TextContent(type="text", text="No agreement found. Run create_agreement first.")]

        # Request
        rc, out = forge_browser("script/RequestAttackMode.s.sol")
        if rc != 0:
            return [types.TextContent(type="text", text=f"Attack mode request failed.\n\n{out}")]

        # Approve via testnet mock moderator
        rc2, out2 = forge_browser("script/ApproveAttackMode.s.sol")
        if rc2 != 0:
            return [types.TextContent(type="text", text=(
                "Attack mode was requested but approval failed.\n\n" + out2
            ))]

        return [types.TextContent(type="text", text=(
            "Step 3 complete!\n\n"
            "The protocol is now officially in attack mode.\n\n"
            "  Request submitted: ✓\n"
            "  Testnet moderator approved: ✓\n"
            "  Agreement state: UNDER_ATTACK\n\n"
            "The vault is open for the authorized whitehat attack. "
            "When you're ready, confirm with the user before proceeding to Step 4."
        ))]

    # ── execute_attack ────────────────────────────────────────────────────────
    elif name == "execute_attack":
        wallet = arguments.get("wallet_address", "").strip()
        if not wallet:
            return [types.TextContent(type="text", text="wallet_address is required.")]
        if not re.match(r"^0x[0-9a-fA-F]{40}$", wallet):
            return [types.TextContent(type="text", text=f"That doesn't look like a valid wallet address: {wallet!r}\nIt should start with 0x and be 42 characters long.")]

        missing = missing_keys(["TOKEN_ADDRESS", "VAULT_ADDRESS"])
        if missing:
            return [types.TextContent(type="text", text="Contracts not deployed. Run deploy_contracts first.")]

        # Set wallet as both attacker (SENDER_ADDRESS) and recovery recipient
        write_env_values({"SENDER_ADDRESS": wallet, "RECOVERY_ADDRESS": wallet})

        rc, out = forge_browser("script/Attack.s.sol")
        if rc != 0:
            return [types.TextContent(type="text", text=f"Attack failed.\n\n{out}")]

        before  = parse_number(out, "Vault before")
        after   = parse_number(out, "Vault after")
        bounty  = parse_number(out, "Bounty kept")
        returned = parse_number(out, "Returned to protocol")

        if any([before, after, bounty, returned]):
            return [types.TextContent(type="text", text=(
                "Step 4 complete — the vault has been drained and funds recovered!\n\n"
                "Here's what happened:\n\n"
                f"  Vault balance before the attack:  {before or '?'} tokens\n"
                f"  Vault balance after the attack:   {after or '?'} tokens\n\n"
                f"  Your whitehat bounty (10%):       {bounty or '?'} tokens  → sent to your wallet\n"
                f"  Returned to the protocol (90%):   {returned or '?'} tokens → sent to your wallet\n\n"
                "The full BattleChain demo is complete!\n\n"
                "What just happened under the hood:\n"
                "  1. A reentrancy vulnerability was exploited in the vault contract\n"
                "  2. The vault's own withdrawal function was called repeatedly before it could "
                "update its internal balance, draining all 1,000 tokens\n"
                "  3. Because this was a Safe Harbor engagement, the funds are legally yours to "
                "keep as a bounty — and you've returned 90% to the protocol as agreed"
            ))]
        return [types.TextContent(type="text", text=f"Attack ran.\n\n{out}")]

    # ── check_agreement_state ─────────────────────────────────────────────────
    elif name == "check_agreement_state":
        missing = missing_keys(["AGREEMENT_ADDRESS"])
        if missing:
            return [types.TextContent(type="text", text="No agreement found. Run create_agreement first.")]

        agreement = read_env()["AGREEMENT_ADDRESS"]
        rc, raw = cast_call(ATTACK_REGISTRY, "getAgreementState(address)(uint8)", agreement)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Could not read agreement state.\n\n{raw}")]

        num = raw.strip()
        label = AGREEMENT_STATES.get(num, "UNKNOWN")
        descriptions = {
            "ACTIVE":           "The agreement exists and the protocol is operating normally.",
            "ATTACK_REQUESTED": "An attack has been requested and is awaiting approval.",
            "UNDER_ATTACK":     "Attack mode is approved — the vault is open for the whitehat exploit.",
            "PRODUCTION":       "The engagement is complete and the protocol is back in production.",
            "UNREGISTERED":     "Agreement not found on-chain.",
        }
        desc = descriptions.get(label, "")
        return [types.TextContent(type="text", text=f"Agreement state: **{label}** ({num})\n{desc}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name!r}")]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
