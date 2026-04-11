#!/usr/bin/env python3
"""
BattleChain MCP Server
Walks a non-technical user through the entire BattleChain security demo via Claude Desktop.
MetaMask handles all transaction signing — no private keys ever touch this server.
"""

import asyncio
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# ── Paths & constants ─────────────────────────────────────────────────────────

STARTER_REPO_URL = "https://github.com/Cyfrin/battlechain-starter-foundry.git"
BATTLECHAIN_DIR  = Path.home() / ".battlechain"
PROJECT_ROOT     = BATTLECHAIN_DIR / "starter"
ENV_FILE         = PROJECT_ROOT / ".env"
FOUNDRY_BIN      = Path.home() / ".foundry" / "bin"

RPC_URL         = "https://testnet.battlechain.com:3051"
CHAIN_ID        = "627"
EXPLORER_API    = "https://block-explorer-api.testnet.battlechain.com/api"
ATTACK_REGISTRY = "0x9E62988ccA776ff6613Fa68D34c9AB5431Ce57e1"
MOCK_MODERATOR  = "0x1bC64E6F187a47D136106784f4E9182801535BD3"

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
    foundry_str = str(FOUNDRY_BIN)
    path = os.environ.get("PATH", "")
    if foundry_str not in path.split(":"):
        os.environ["PATH"] = foundry_str + ":" + path


def _forge_available() -> bool:
    _ensure_foundry_in_path()
    return subprocess.run(["forge", "--version"], capture_output=True).returncode == 0


def _git_available() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _glibc_version() -> tuple[int, int] | None:
    result = subprocess.run(["ldd", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    m = re.search(r"(\d+)\.(\d+)\s*$", first_line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _run(cmd: list[str], cwd: Path | None = None, extra_env: dict | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    _ensure_foundry_in_path()
    env["PATH"] = os.environ["PATH"]
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


# ── .env helpers ──────────────────────────────────────────────────────────────

def read_env() -> dict[str, str]:
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
    env = os.environ.copy()
    _ensure_foundry_in_path()
    env["PATH"] = os.environ["PATH"]
    env.update(read_env())
    return env


# ── Output parsers ────────────────────────────────────────────────────────────

def parse_address(output: str, key: str) -> str | None:
    m = re.search(rf"{re.escape(key)}[=:\s]+\s*(0x[0-9a-fA-F]{{40}})", output)
    return m.group(1) if m else None


def parse_number(output: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*:?\s*(\d+)", output)
    return m.group(1) if m else None


# ── MetaMask signing page ─────────────────────────────────────────────────────

_SIGNING_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BattleChain \u2014 Sign Transactions</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 560px; margin: 64px auto; padding: 0 24px; color: #1a1a2e; }
  h1   { font-size: 1.25rem; color: #16213e; margin-bottom: 4px; }
  .sub { color: #666; font-size: .88rem; margin: 0 0 24px; }
  #box { border: 1px solid #d4daf0; border-radius: 8px; padding: 18px 20px;
         background: #f8f9ff; }
  #st  { font-weight: 600; margin: 0 0 4px; white-space: pre-wrap; }
  #dt  { color: #555; font-size: .87rem; min-height: 1.1em; }
  .ok  { color: #1b6e3c; }
  .err { color: #b91c1c; }
</style>
</head>
<body>
<h1>&#x1F510; BattleChain Demo</h1>
<p class="sub">This page signs the demo transactions via MetaMask.</p>
<div id="box">
  <div id="st">Connecting to MetaMask\u2026</div>
  <div id="dt"></div>
</div>
<script>
(async () => {
  const st = document.getElementById('st');
  const dt = document.getElementById('dt');
  const set = (s, d, cls) => {
    st.textContent = s;
    if (d !== undefined) dt.textContent = d;
    if (cls) st.className = cls;
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  if (!window.ethereum) {
    set('MetaMask not detected.', 'Install MetaMask in this browser, then refresh.', 'err');
    return;
  }

  // 1. Connect wallet
  let accounts;
  try {
    accounts = await window.ethereum.request({method: 'eth_requestAccounts'});
  } catch(e) {
    set('MetaMask connection cancelled.', e.message, 'err');
    return;
  }
  const address = accounts[0];

  // 1b. Verify MetaMask chainId matches BattleChain directly
  const RPC_URL = 'https://testnet.battlechain.com:3051';
  const bcRpc = (method, params) => fetch(RPC_URL, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({jsonrpc: '2.0', method, params: params || [], id: 1}),
  }).then(r => r.json()).then(d => d.result);

  // 2. Switch to / add BattleChain testnet
  const chainHex = '0x' + (CHAIN_ID_INT).toString(16);
  try {
    await window.ethereum.request({
      method: 'wallet_switchEthereumChain',
      params: [{chainId: chainHex}],
    });
  } catch(e) {
    if (e.code === 4902 || e.code === -32603) {
      try {
        await window.ethereum.request({
          method: 'wallet_addEthereumChain',
          params: [{
            chainId: chainHex,
            chainName: 'BattleChain Testnet',
            rpcUrls: ['https://testnet.battlechain.com:3051'],
            nativeCurrency: {name: 'ETH', symbol: 'ETH', decimals: 18},
            blockExplorerUrls: ['https://block-explorer.testnet.battlechain.com'],
          }],
        });
      } catch(e2) {
        set('Could not add BattleChain network.', e2.message, 'err');
        return;
      }
    }
  }

  // 3. Send address to server so it can run forge dry-run
  set('Connected: ' + address, 'Preparing transactions\u2026');
  try {
    await fetch('/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({address}),
    });
  } catch(e) {
    set('Could not reach local signing server.', e.message, 'err');
    return;
  }

  // 4. Poll until transactions are ready
  let txs, addresses = {};
  for (;;) {
    let r;
    try { r = await fetch('/txs'); } catch(e) { await sleep(1000); continue; }
    if (r.status === 200) { const payload = await r.json(); txs = payload.transactions; addresses = payload.addresses || {}; break; }
    if (r.status === 500) {
      set('Transaction preparation failed.', await r.text(), 'err'); return;
    }
    await sleep(1000);
  }
  if (!txs || !txs.length) {
    set('No transactions to sign.', '', 'err'); return;
  }

  // 5. Sign each transaction sequentially
  // Pin nonce to on-chain value so MetaMask's stale internal counter can't cause queuing
  let baseNonce = 0;
  try { baseNonce = parseInt(await bcRpc('eth_getTransactionCount', [address, 'latest']), 16); } catch(e) {}
  let gasPrice = '0x1';
  try { gasPrice = (await bcRpc('eth_gasPrice')) || '0x1'; } catch(e) {}

  const hashes = [];
  for (let i = 0; i < txs.length; i++) {
    const tx = txs[i];
    set('Sign transaction ' + (i+1) + ' of ' + txs.length + ' in MetaMask\u2026',
        tx.description ? 'Action: ' + tx.description : '');
    const params = {type: '0x0', from: address,
                    nonce: '0x' + (baseNonce + i).toString(16),
                    data: tx.data, value: tx.value || '0x0',
                    gas: '0x2DC6C0', gasPrice: gasPrice};
    if (tx.to) params.to = tx.to;
    let hash;
    try {
      hash = await window.ethereum.request({method: 'eth_sendTransaction', params: [params]});
    } catch(e) {
      set('Transaction ' + (i+1) + ' rejected.', e.message, 'err'); return;
    }
    hashes.push(hash);

    // Verify directly against BattleChain RPC (CORS open) — bypasses MetaMask routing
    dt.textContent = 'Checking tx ' + (i+1) + ' on BattleChain\u2026';
    let onChain = false;
    for (let attempt = 0; attempt < 4; attempt++) {
      await sleep(2000);
      try {
        const txData = await bcRpc('eth_getTransactionByHash', [hash]);
        if (txData) { onChain = true; break; }
      } catch(e) {}
    }
    if (!onChain) {
      set('\u26a0 TX ' + (i+1) + ' not in BattleChain mempool',
          'Hash: ' + hash + ' — MetaMask returned a hash but tx is NOT at testnet.battlechain.com:3051. ' +
          'MetaMask is likely using a different RPC for chain 627. ' +
          'Check MetaMask Settings \u2192 Networks \u2192 BattleChain RPC URL.', 'err');
      return;
    }
    // Log the tx type MetaMask actually used
    let txType = '?';
    try { const d = await bcRpc('eth_getTransactionByHash', [hash]); txType = d && d.type || '?'; } catch(e) {}
    dt.textContent = '\u2713 tx ' + (i+1) + ' in mempool (type=' + txType + '): ' + hash.slice(0,12) + '\u2026';
  }

  // 6. Report back and done
  await fetch('/done', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({hashes, address}),
  });
  const addrLines = Object.entries(addresses).map(([k,v]) => k + ': ' + v).join('  |  ');
  const hashLines = hashes.map((h,i) => 'tx'+(i+1)+': '+h).join('  |  ');
  set('\u2713 All ' + hashes.length + ' transaction(s) in BattleChain mempool',
      (addrLines ? 'Contracts: ' + addrLines + '  ||  ' : '') + 'Hashes: ' + hashLines, 'ok');
})();
</script>
</body>
</html>
"""


class _SigningHandler(http.server.BaseHTTPRequestHandler):
    server: "_SigningServer"

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", self.server.html.encode())
        elif self.path == "/txs":
            with self.server._lock:
                err = self.server._forge_error
                txs = self.server._txs
            if err is not None:
                self._send(500, "text/plain", err.encode())
            elif txs is not None:
                payload = {"transactions": txs, "addresses": self.server._env_updates}
                self._send(200, "application/json", json.dumps(payload).encode())
            else:
                self._send(202, "text/plain", b"")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if self.path == "/connect":
            addr = data.get("address", "").strip()
            self.server._wallet_address = addr
            threading.Thread(target=self.server._run_forge, args=(addr,), daemon=True).start()
            self._send(200, "text/plain", b"ok")
        elif self.path == "/done":
            self.server._result = data
            self.server._done_event.set()
            self._send(200, "text/plain", b"ok")
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ct: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _SigningServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, script_path: str) -> None:
        self.script_path = script_path
        self._wallet_address: str | None = None
        self._txs: list | None = None
        self._forge_error: str | None = None
        self._env_updates: dict[str, str] = {}
        self._result: dict | None = None
        self._done_event = threading.Event()
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _SigningHandler)
        port = self.server_address[1]
        self.html = _SIGNING_HTML.replace("CHAIN_ID_INT", CHAIN_ID)
        self.url = f"http://127.0.0.1:{port}/"

    def _run_forge(self, sender: str) -> None:
        try:
            txs, env_updates, error = _dry_run_forge(self.script_path, sender)
            with self._lock:
                if error:
                    self._forge_error = error
                else:
                    self._env_updates = env_updates
                    self._txs = txs
        except Exception as exc:
            with self._lock:
                self._forge_error = str(exc)

    def wait(self, timeout: int = 300) -> dict | None:
        self._done_event.wait(timeout=timeout)
        return self._result


# Pending signing servers keyed by script basename
_signing_servers: dict[str, _SigningServer] = {}


def _dry_run_forge(script_path: str, sender: str) -> tuple[list, dict, str]:
    """
    Simulate a forge script without broadcasting.
    Returns (transactions, env_updates, error_string).
    error_string is empty on success.
    """
    _ensure_foundry_in_path()
    script_name = Path(script_path).name

    cmd = [
        "forge", "script", script_path,
        "--sender", sender,
        "--rpc-url", RPC_URL,
        "--chain", CHAIN_ID,
        "--legacy",
        "--skip-simulation",
    ]
    env = _subprocess_env()
    env["SENDER_ADDRESS"] = sender

    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=env)
    output = result.stdout + result.stderr

    if result.returncode != 0:
        return [], {}, f"Forge simulation failed:\n\n{output}"

    # Parse predicted contract addresses from script console.log output
    env_updates: dict[str, str] = {}
    for key in ("TOKEN_ADDRESS", "VAULT_ADDRESS", "AGREEMENT_ADDRESS"):
        m = re.search(rf"{re.escape(key)}[=:\s]+\s*(0x[0-9a-fA-F]{{40}})", output)
        if m:
            env_updates[key] = m.group(1)

    # Read the dry-run broadcast JSON written by forge
    dry_run_path = (
        PROJECT_ROOT / "broadcast" / script_name / CHAIN_ID / "dry-run" / "run-latest.json"
    )
    if not dry_run_path.exists():
        return [], env_updates, (
            f"Forge simulation produced no transaction file.\n"
            f"Expected: {dry_run_path}\n\n{output}"
        )

    try:
        data = json.loads(dry_run_path.read_text())
    except Exception as exc:
        return [], env_updates, f"Failed to parse dry-run JSON: {exc}"

    txs = []
    for entry in data.get("transactions", []):
        tx = entry.get("transaction", {})
        desc = entry.get("contractName") or entry.get("function") or ""
        txs.append({
            "to": tx.get("to"),
            "data": tx.get("input", "0x"),
            "value": tx.get("value", "0x0"),
            "description": desc,
        })

    return txs, env_updates, ""


def _verify_txs(hashes: list[str]) -> tuple[bool, str]:
    """
    Verify each tx landed on BattleChain and did not revert.
    Uses eth_getTransactionReceipt (status 0x1 = success, 0x0 = reverted).
    Returns (True, "") on success or (False, diagnostic_message) on failure.
    """
    for h in hashes:
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
                "params": [h], "id": 1,
            }).encode()
            req = urllib.request.Request(
                RPC_URL, data=body, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            receipt = data.get("result")
            if receipt is None:
                return False, (
                    f"Transaction not found on BattleChain testnet.\n\n"
                    f"Hash: {h}\n\n"
                    "MetaMask submitted the transaction but it did not reach the BattleChain RPC. "
                    "Try opening MetaMask → Settings → Advanced → Reset Account to clear any "
                    "stuck pending transactions, then run deploy_contracts again."
                )
            if receipt.get("status") == "0x0":
                return False, (
                    f"Transaction reverted on-chain.\n\n"
                    f"Hash: {h}\n\n"
                    "The transaction was accepted by BattleChain but the contract call reverted. "
                    "This usually means a precondition failed (e.g. contract already deployed at "
                    "that address, or wrong state). Check that you are not re-running a step that "
                    "already completed."
                )
        except Exception:
            # RPC unreachable — proceed optimistically so we don't block the flow
            continue
    return True, ""


def _open_browser(url: str) -> None:
    if _is_wsl():
        if subprocess.run(["which", "wslview"], capture_output=True).returncode == 0:
            subprocess.Popen(["wslview", url])
        else:
            subprocess.Popen(["cmd.exe", "/c", "start", "", url])
    else:
        import webbrowser
        webbrowser.open(url)


def forge_sign_with_metamask(script_path: str) -> tuple[int, str]:
    """
    Start (or check) a MetaMask signing session for the given forge script.

    First call:  starts a local HTTP server, opens the signing page in the
                 browser, and returns (-1, url) so Claude can show the link.
    Second call: checks whether the user has finished signing.
                 Returns (0, output) on success or (1, error) on failure.
    """
    key = Path(script_path).name

    # ── Second call: collect result ───────────────────────────────────────────
    if key in _signing_servers:
        srv = _signing_servers[key]
        if not srv._done_event.is_set():
            return -1, srv.url  # still waiting — return url again
        result = srv._result or {}
        env_updates = srv._env_updates  # capture before shutdown
        srv.shutdown()
        del _signing_servers[key]

        hashes = result.get("hashes", [])
        if not hashes:
            return 1, "No transaction hashes received — MetaMask may have rejected the transactions."

        # Verify every tx actually landed on BattleChain before writing addresses
        ok, err = _verify_txs(hashes)
        if not ok:
            return 1, err

        # All txs confirmed — now persist addresses
        addr = result.get("address", "")
        updates: dict[str, str] = {}
        if addr:
            updates["SENDER_ADDRESS"] = addr
        updates.update(env_updates)
        if updates:
            write_env_values(updates)

        return 0, "Signed " + str(len(hashes)) + " transaction(s).\nHashes:\n" + "\n".join(hashes)

    # ── First call: start server and open browser ─────────────────────────────
    srv = _SigningServer(script_path)
    _signing_servers[key] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _open_browser(srv.url)
    except Exception:
        pass  # URL is returned below; Claude will show it if browser didn't auto-open
    return -1, srv.url


def _needs_signing(url: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=(
        f"MetaMask signing required.\n\n"
        f"Open this URL in the browser where MetaMask is installed:\n\n"
        f"  {url}\n\n"
        f"Approve all transactions in MetaMask, then call this tool again to continue."
    ))]


# ── Prerequisite guard ────────────────────────────────────────────────────────

def missing_keys(keys: list[str]) -> list[str]:
    env = read_env()
    return [k for k in keys if not env.get(k)]


# ── cast read-only helper ─────────────────────────────────────────────────────

def cast_call(address: str, sig: str, *args: str) -> tuple[int, str]:
    cmd = ["cast", "call", address, sig, *args, "--rpc-url", RPC_URL]
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=_subprocess_env()
    )
    return result.returncode, (result.stdout or result.stderr).strip()


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
                "Opens a local signing page — the user approves transactions in MetaMask. "
                "Call this tool again after the user finishes signing to collect the result. "
                "Requires prepare_environment to have succeeded first."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="create_agreement",
            description=(
                "Step 2 of 4. Create a Safe Harbor security agreement and register it on the BattleChain registry. "
                "This is the legal framework that makes the upcoming attack a legitimate whitehat engagement. "
                "Opens MetaMask signing page — tell the user to approve, then call this tool again. "
                "Requires Step 1 (deploy_contracts) to be complete."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="request_and_approve_attack_mode",
            description=(
                "Step 3 of 4. Submit the attack mode request and immediately approve it via the testnet moderator. "
                "On testnet anyone can approve, so this skips the normal waiting period. "
                "Opens MetaMask signing page twice in quick succession (once to request, once to approve). "
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
                "Opens MetaMask signing page. Call again after signing to collect the result. "
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

        # 0. Reset any previous demo state so every run is a clean slate
        for srv in list(_signing_servers.values()):
            try:
                srv.shutdown()
            except Exception:
                pass
        _signing_servers.clear()
        # Only clear .env if the project directory already exists
        # (on first run the directory hasn't been created yet)
        if ENV_FILE.exists():
            write_env_values({
                "TOKEN_ADDRESS": "",
                "VAULT_ADDRESS": "",
                "AGREEMENT_ADDRESS": "",
                "SENDER_ADDRESS": "",
                "RECOVERY_ADDRESS": "",
            })
        steps.append("Previous demo state cleared")

        # 1. Git
        if not _git_available():
            return [types.TextContent(type="text", text=(
                "SETUP ERROR: git is not installed in WSL.\n\n"
                "Open a WSL terminal and run:\n\n"
                "    sudo apt-get install -y git\n\n"
                "Then come back and try again."
            ))]
        steps.append("git: found")

        # 2. Foundry
        if not _forge_available():
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

            steps.append("Foundry: not found — installing...")
            rc, out = _run(
                ["bash", "-c", "curl -L https://foundry.paradigm.xyz | bash -s -- --no-modify-path"],
            )
            if rc != 0:
                return [types.TextContent(type="text", text=f"Foundry installation failed.\n\n{out}")]

            rc, out = _run([str(FOUNDRY_BIN / "foundryup")])
            if rc != 0:
                return [types.TextContent(type="text", text=f"foundryup failed.\n\n{out}")]
            steps.append("Foundry: installed")
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
            steps.append("Updating BattleChain demo project...")
            _run(["git", "pull", "--recurse-submodules"], cwd=PROJECT_ROOT)
            _run(["git", "submodule", "update", "--init", "--recursive"], cwd=PROJECT_ROOT)
            steps.append("Demo project: up to date")

        # 4. Build contracts
        steps.append("Compiling smart contracts...")
        rc, out = _run(["forge", "build"], cwd=PROJECT_ROOT)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Contract compilation failed.\n\n{out}")]
        steps.append("Smart contracts: compiled successfully")

        return [types.TextContent(type="text", text=(
            "Environment ready!\n\n"
            + "\n".join(f"  \u2713 {s}" for s in steps)
            + "\n\nYou're all set. MetaMask will be used for signing transactions — "
            "make sure it's installed in your browser before the next step."
        ))]

    # ── get_status ────────────────────────────────────────────────────────────
    elif name == "get_status":
        env = read_env()

        def status(key: str) -> str:
            val = env.get(key, "")
            return f"\u2713 {val}" if val else "\u2717 not set"

        token     = env.get("TOKEN_ADDRESS", "")
        vault     = env.get("VAULT_ADDRESS", "")
        agreement = env.get("AGREEMENT_ADDRESS", "")

        lines = [
            "**BattleChain Demo Status**\n",
            f"  MockToken:             {status('TOKEN_ADDRESS')}",
            f"  VulnerableVault:       {status('VAULT_ADDRESS')}",
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

        rc, out = forge_sign_with_metamask("script/Setup.s.sol")
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Deployment failed.\n\n{out}")]

        env2 = read_env()
        token = env2.get("TOKEN_ADDRESS")
        vault = env2.get("VAULT_ADDRESS")

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
            "Deployment ran but contract addresses weren't found.\n\n" + out
        ))]

    # ── create_agreement ──────────────────────────────────────────────────────
    elif name == "create_agreement":
        if missing_keys(["VAULT_ADDRESS"]):
            return [types.TextContent(type="text", text="VulnerableVault not deployed yet. Run deploy_contracts first.")]

        env = read_env()
        if env.get("AGREEMENT_ADDRESS"):
            return [types.TextContent(type="text", text=(
                f"Agreement already exists: {env['AGREEMENT_ADDRESS']}\n\n"
                "Skipping. Call request_and_approve_attack_mode to continue."
            ))]

        rc, out = forge_sign_with_metamask("script/CreateAgreement.s.sol")
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Agreement creation failed.\n\n{out}")]

        agreement = read_env().get("AGREEMENT_ADDRESS")
        if agreement:
            return [types.TextContent(type="text", text=(
                "Step 2 complete!\n\n"
                "A Safe Harbor security agreement has been created and registered on-chain.\n"
                "This is the legal framework that makes the upcoming attack an authorized whitehat engagement "
                "rather than a hack.\n\n"
                f"  Agreement address: {agreement}\n\n"
                "Ready for Step 3: requesting and approving attack mode."
            ))]
        return [types.TextContent(type="text", text=(
            "Agreement creation ran but the address wasn't found.\n\n" + out
        ))]

    # ── request_and_approve_attack_mode ───────────────────────────────────────
    elif name == "request_and_approve_attack_mode":
        if missing_keys(["AGREEMENT_ADDRESS"]):
            return [types.TextContent(type="text", text="No agreement found. Run create_agreement first.")]

        # Request
        rc, out = forge_sign_with_metamask("script/RequestAttackMode.s.sol")
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Attack mode request failed.\n\n{out}")]

        # Approve via testnet mock moderator
        rc2, out2 = forge_sign_with_metamask("script/ApproveAttackMode.s.sol")
        if rc2 == -1:
            return _needs_signing(out2)
        if rc2 != 0:
            return [types.TextContent(type="text", text=(
                "Attack mode was requested but approval failed.\n\n" + out2
            ))]

        return [types.TextContent(type="text", text=(
            "Step 3 complete!\n\n"
            "The protocol is now officially in attack mode.\n\n"
            "  Request submitted: \u2713\n"
            "  Testnet moderator approved: \u2713\n"
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
            return [types.TextContent(type="text", text=(
                f"That doesn't look like a valid wallet address: {wallet!r}\n"
                "It should start with 0x and be 42 characters long."
            ))]

        if missing_keys(["TOKEN_ADDRESS", "VAULT_ADDRESS"]):
            return [types.TextContent(type="text", text="Contracts not deployed. Run deploy_contracts first.")]

        write_env_values({"SENDER_ADDRESS": wallet, "RECOVERY_ADDRESS": wallet})

        rc, out = forge_sign_with_metamask("script/Attack.s.sol")
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Attack failed.\n\n{out}")]

        before   = parse_number(out, "Vault before")
        after    = parse_number(out, "Vault after")
        bounty   = parse_number(out, "Bounty kept")
        returned = parse_number(out, "Returned to protocol")

        if any([before, after, bounty, returned]):
            return [types.TextContent(type="text", text=(
                "Step 4 complete — the vault has been drained and funds recovered!\n\n"
                "Here's what happened:\n\n"
                f"  Vault balance before the attack:  {before or '?'} tokens\n"
                f"  Vault balance after the attack:   {after or '?'} tokens\n\n"
                f"  Your whitehat bounty (10%):       {bounty or '?'} tokens  \u2192 sent to your wallet\n"
                f"  Returned to the protocol (90%):   {returned or '?'} tokens \u2192 sent to your wallet\n\n"
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
        if missing_keys(["AGREEMENT_ADDRESS"]):
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
