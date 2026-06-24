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

# This server targets the BattleChain testnet exclusively. Addresses come from a
# vendored, testnet-only subset of the canonical deployments.json published by
# Cyfrin/battlechain-lib. Refresh it with scripts/sync_deployments.py (run after a
# testnet redeploy) so changes land as a reviewable diff.

CHAIN_ID = "627"  # BattleChain testnet

_DEPLOYMENTS_FILE = Path(__file__).resolve().parent / "deployments.json"


def _load_deployments() -> dict:
    """Return deployments.json["networks"] from the vendored copy."""
    return json.loads(_DEPLOYMENTS_FILE.read_text())["networks"]


def _resolve_deployment(networks: dict, chain_id: str) -> dict:
    """Flatten the testnet chain entry into the constants this server uses.

    RPC and explorer URLs are not in deployments.json; they follow the
    battlechain-lib README convention https://testnet.battlechain.com.
    """
    net = networks[chain_id]
    return {
        "rpc_url": "https://testnet.battlechain.com",
        "explorer_web": "https://explorer.testnet.battlechain.com",
        "explorer_api": "https://block-explorer-api.testnet.battlechain.com/api",
        "attack_registry": net["attackRegistry"],
        "moderator": net["mockRegistryModerator"],
    }


_DEPLOYMENT     = _resolve_deployment(_load_deployments(), CHAIN_ID)
RPC_URL         = _DEPLOYMENT["rpc_url"]
EXPLORER_WEB    = _DEPLOYMENT["explorer_web"]
EXPLORER_API    = _DEPLOYMENT["explorer_api"]
ATTACK_REGISTRY = _DEPLOYMENT["attack_registry"]
MOCK_MODERATOR  = _DEPLOYMENT["moderator"]

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
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#f1f5f9;min-height:100vh;display:flex;
     align-items:flex-start;justify-content:center;padding:32px 16px}
.card{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.09);
      max-width:560px;width:100%;overflow:hidden}
.banner{background:PAGE_ACCENT;padding:14px 22px;display:flex;align-items:center;gap:14px}
.bl{color:rgba(255,255,255,.85);font-size:.78rem;font-weight:600;
    letter-spacing:.04em;white-space:nowrap}
.dots{display:flex;align-items:center;gap:0;flex:1}
.dot{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;
     justify-content:center;font-size:.72rem;font-weight:700;flex-shrink:0}
.da{background:#fff;color:PAGE_ACCENT}
.dd{background:rgba(255,255,255,.6);color:PAGE_ACCENT}
.df{background:rgba(255,255,255,.15);color:rgba(255,255,255,.5)}
.dl{flex:1;height:2px;background:rgba(255,255,255,.2)}
.dld{flex:1;height:2px;background:rgba(255,255,255,.65)}
.hero{padding:22px 24px 10px;display:flex;gap:14px;align-items:flex-start}
.hib{width:52px;height:52px;border-radius:12px;background:PAGE_ACCENT_LIGHT;
     display:flex;align-items:center;justify-content:center;font-size:1.25rem;
     font-weight:800;color:PAGE_ACCENT;letter-spacing:-.03em;flex-shrink:0}
.ht h1{font-size:1.2rem;color:#111827;font-weight:700}
.ht p{font-size:.86rem;color:#6b7280;margin-top:4px;line-height:1.4}
.vis{padding:10px 24px}
.nar{padding:4px 24px 18px;font-size:.87rem;color:#374151;line-height:1.6}
.nar strong{color:PAGE_ACCENT}
.nar em{font-style:italic}
hr{border:none;border-top:1px solid #f3f4f6}
.sw{margin:14px 24px 22px;border:1.5px solid #e5e7eb;border-radius:10px;
    padding:14px 16px;background:#f9fafb}
#st{font-weight:600;font-size:.9rem;color:#111827;white-space:pre-wrap}
#dt{font-size:.82rem;color:#6b7280;margin-top:4px;min-height:1.1em}
.ok{color:#059669!important}
.err{color:#dc2626!important}
.footer{display:flex;align-items:center;gap:8px;padding:10px 24px;
        border-top:1px solid #f3f4f6;font-size:.74rem;color:#9ca3af}
.footer img{height:16px;opacity:.55;display:block}
</style>
</head>
<body>
<div class="card">
<div class="banner">
  <img src="/logo/battlechain-wm" alt="BattleChain" style="height:20px;flex-shrink:0">
  <div class="dots" style="justify-content:flex-end">PAGE_STEP_DOTS</div>
</div>
<div class="hero">
  <div class="hib">PAGE_ICON</div>
  <div class="ht"><h1>PAGE_TITLE</h1><p>PAGE_TAGLINE</p></div>
</div>
<div class="vis">PAGE_VISUAL</div>
<div class="nar">PAGE_NARRATIVE</div>
<hr>
<div class="sw">
  <div id="st">Connecting to MetaMask\u2026</div>
  <div id="dt"></div>
  <div id="cta" style="display:none;margin-top:12px;padding:9px 14px;
       background:PAGE_ACCENT_LIGHT;border-radius:7px;font-size:.84rem;
       color:PAGE_ACCENT;font-weight:700;text-align:center">
    \u21a9\u00a0 Close this tab and return to Claude Desktop
  </div>
</div>
<span id="done-hl" style="display:none">PAGE_DONE_HEADLINE</span>
<span id="done-dt" style="display:none">PAGE_DONE_DETAIL</span>
<div class="footer">
  <img src="/logo/cyfrin" alt="Cyfrin">
  <span>BattleChain \u00b7 a Cyfrin protocol</span>
</div>
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
  const RPC_URL = 'RPC_URL_PLACEHOLDER';
  const bcRpc = (method, params) => fetch(RPC_URL, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({jsonrpc: '2.0', method, params: params || [], id: 1}),
  }).then(r => r.json()).then(d => d.result);
  const balOf = async (token, owner) => {
    const data = '0x70a08231' + owner.slice(2).padStart(64, '0');
    const r = await bcRpc('eth_call', [{to: token, data}, 'latest']);
    return r ? (parseInt(r, 16) / 1e18).toFixed(0) : null;
  };

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
            rpcUrls: ['RPC_URL_PLACEHOLDER'],
            nativeCurrency: {name: 'ETH', symbol: 'ETH', decimals: 18},
            blockExplorerUrls: ['EXPLORER_WEB_PLACEHOLDER'],
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

  // Fetch live balances if the attack-step balance display exists
  const balDisplay = document.getElementById('bal-display');
  if (balDisplay && addresses.TOKEN_ADDRESS && addresses.VAULT_ADDRESS && addresses.SENDER_ADDRESS) {
    try {
      const vb = await balOf(addresses.TOKEN_ADDRESS, addresses.VAULT_ADDRESS);
      const wb = await balOf(addresses.TOKEN_ADDRESS, addresses.SENDER_ADDRESS);
      const ev = document.getElementById('bal-vault-b'); if (ev && vb !== null) ev.textContent = vb + ' BCT';
      const ew = document.getElementById('bal-wallet-b'); if (ew && wb !== null) ew.textContent = wb + ' BCT';
      balDisplay.style.display = 'block';
    } catch(e) {}
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
                    gasPrice: gasPrice};
    if (tx.to) params.to = tx.to;
    try {
      const est = await bcRpc('eth_estimateGas', [params]);
      // Add 30% headroom for ZK pubdata overhead
      params.gas = '0x' + Math.ceil(parseInt(est, 16) * 1.3).toString(16);
    } catch(e) {
      // Fall back to forge's -g 300 estimate, or a safe default
      params.gas = tx.gas || '0x600000';
    }
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
          'Hash: ' + hash + ' — MetaMask returned a hash but tx is NOT at testnet.battlechain.com. ' +
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
  // Refresh after-balances
  if (balDisplay && addresses.TOKEN_ADDRESS && addresses.VAULT_ADDRESS && addresses.SENDER_ADDRESS) {
    try {
      const vb = await balOf(addresses.TOKEN_ADDRESS, addresses.VAULT_ADDRESS);
      const wb = await balOf(addresses.TOKEN_ADDRESS, addresses.SENDER_ADDRESS);
      const ev = document.getElementById('bal-vault-a'); if (ev && vb !== null) ev.textContent = vb + ' BCT';
      const ew = document.getElementById('bal-wallet-a'); if (ew && wb !== null) ew.textContent = wb + ' BCT';
    } catch(e) {}
  }
  const doneHl = document.getElementById('done-hl');
  const doneDt = document.getElementById('done-dt');
  const headline = (doneHl && doneHl.textContent) || ('\u2713 ' + hashes.length + ' transaction(s) submitted');
  let detail = (doneDt && doneDt.textContent) || hashes.map((h,i) => 'tx'+(i+1)+': '+h).join(' | ');
  // Append truncated explorer links for known contract addresses
  const explorerBase = 'EXPLORER_WEB_PLACEHOLDER/address/';
  const addrLabels = {TOKEN_ADDRESS:'BCToken',VAULT_ADDRESS:'VulnerableVault',AGREEMENT_ADDRESS:'Agreement',SENDER_ADDRESS:'Your Wallet'};
  const addrLinks = Object.entries(addresses)
    .filter(([k,v]) => v && /^0x[0-9a-fA-F]{40}$/.test(v) && addrLabels[k])
    .map(([k,v]) => addrLabels[k]+': <a href="'+explorerBase+v+'" target="_blank" style="color:inherit;font-weight:600;text-decoration:underline">'+v.slice(0,6)+'\u2026'+v.slice(-4)+'</a>');
  if (addrLinks.length) detail += '<br><span style="font-size:.76rem;color:#6b7280;display:block;margin-top:5px">'+addrLinks.join(' \u00b7 ')+'</span>';
  st.textContent = headline;
  st.className = 'ok';
  dt.innerHTML = detail;
  const cta = document.getElementById('cta');
  if (cta) cta.style.display = 'block';
})();
</script>
</body>
</html>
"""


def _build_dots(step: int, total: int = 4) -> str:
    if step < 1:
        return ""
    parts = []
    for i in range(1, total + 1):
        cls = "dot da" if i == step else ("dot dd" if i < step else "dot df")
        parts.append(f'<span class="{cls}">{i}</span>')
        if i < total:
            parts.append(f'<span class="{"dld" if i < step else "dl"}"></span>')
    return "".join(parts)


_VIS_DEPLOY = (
    '<div style="display:flex;align-items:center;gap:10px">'
    # Sender wallet node
    '<div style="flex-shrink:0;text-align:center;padding:10px 14px;background:#dbeafe;'
    'border:1.5px solid #2563eb;border-radius:8px">'
    '<div style="width:32px;height:32px;border-radius:50%;background:#1e40af;color:#fff;'
    'display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:800;'
    'margin:0 auto">W</div>'
    '<div style="font-size:.72rem;font-weight:700;color:#1e40af;margin-top:5px">WALLET</div>'
    '</div>'
    '<div style="color:#cbd5e1;font-size:1.2rem;font-weight:300">\u2192</div>'
    # Contracts column
    '<div style="flex:1;display:flex;flex-direction:column;gap:7px">'
    '<div style="padding:8px 12px;background:#fef2f2;border:1.5px solid #ef4444;'
    'border-radius:7px">'
    '<div style="font-size:.8rem;font-weight:700;color:#991b1b">VulnerableVault</div>'
    '<div style="font-size:.72rem;color:#9ca3af;margin-top:1px">holds protocol funds</div>'
    '</div>'
    '<div style="padding:8px 12px;background:#f0fdf4;border:1.5px solid #16a34a;'
    'border-radius:7px">'
    '<div style="font-size:.8rem;font-weight:700;color:#166534">BCToken</div>'
    '<div style="font-size:.72rem;color:#9ca3af;margin-top:1px">ERC-20 asset at risk</div>'
    '</div>'
    '</div>'
    '<div style="color:#cbd5e1;font-size:1.2rem;font-weight:300">\u2192</div>'
    # Chain destination node
    '<div style="flex-shrink:0;text-align:center;padding:10px 14px;background:#dbeafe;'
    'border:2px solid #2563eb;border-radius:8px">'
    '<div style="width:32px;height:32px;border-radius:50%;background:#1d4ed8;color:#fff;'
    'display:flex;align-items:center;justify-content:center;font-size:.62rem;font-weight:800;'
    'margin:0 auto;letter-spacing:-.02em">BC</div>'
    '<div style="font-size:.72rem;font-weight:700;color:#1d4ed8;margin-top:5px">BATTLECHAIN</div>'
    '<div style="font-size:.67rem;color:#93c5fd">testnet</div>'
    '</div></div>'
)

_VIS_AGREEMENT = (
    '<div style="background:#f9fafb;border:1.5px solid #d1fae5;border-radius:10px;overflow:hidden">'
    '<div style="background:#059669;color:white;padding:9px 16px">'
    '<span style="font-size:.8rem;font-weight:700;letter-spacing:.06em">'
    'SAFE HARBOR AGREEMENT \u2014 ON-CHAIN</span></div>'
    '<div style="display:flex;justify-content:space-between;padding:9px 16px;'
    'border-bottom:1px solid #f3f4f6;font-size:.84rem">'
    '<span style="color:#6b7280">Bounty rate</span>'
    '<span style="color:#059669;font-weight:700">10% of recovered funds</span></div>'
    '<div style="display:flex;justify-content:space-between;padding:9px 16px;'
    'border-bottom:1px solid #f3f4f6;font-size:.84rem">'
    '<span style="color:#6b7280">Maximum payout</span>'
    '<span style="color:#059669;font-weight:700">$5,000,000 USD</span></div>'
    '<div style="display:flex;justify-content:space-between;padding:9px 16px;'
    'border-bottom:1px solid #f3f4f6;font-size:.84rem">'
    '<span style="color:#6b7280">Identity required</span>'
    '<span style="color:#059669;font-weight:700">Not required</span></div>'
    '<div style="display:flex;justify-content:space-between;padding:9px 16px;font-size:.84rem">'
    '<span style="color:#6b7280">Legal status</span>'
    '<span style="color:#059669;font-weight:700">Safe Harbor Protected \u2713</span></div>'
    '</div>'
)

_VIS_ATTACK_MODE = (
    '<div style="display:flex;align-items:center;gap:6px;padding:4px 0">'
    '<div style="flex:1;text-align:center;padding:10px 8px;background:#d1fae5;'
    'border:2px solid #059669;border-radius:8px">'
    '<div style="font-size:.72rem;font-weight:700;color:#065f46;letter-spacing:.04em">'
    '\u25cf ACTIVE</div>'
    '<div style="font-size:.68rem;color:#6b7280;margin-top:2px">safe harbor live</div>'
    '</div>'
    '<div style="color:#94a3b8;font-size:1.1rem">\u2192</div>'
    '<div style="flex:1;text-align:center;padding:10px 8px;background:#ffedd5;'
    'border:2px solid #ea580c;border-radius:8px">'
    '<div style="font-size:.72rem;font-weight:700;color:#9a3412;letter-spacing:.04em">'
    '\u25cf REQUESTED</div>'
    '<div style="font-size:.68rem;color:#6b7280;margin-top:2px">DAO reviewing</div>'
    '</div>'
    '<div style="color:#94a3b8;font-size:1.1rem">\u2192</div>'
    '<div style="flex:1;text-align:center;padding:10px 8px;background:#fee2e2;'
    'border:2.5px solid #dc2626;border-radius:8px;'
    'box-shadow:0 0 0 3px #fee2e2,0 0 0 5px #dc2626">'
    '<div style="font-size:.72rem;font-weight:700;color:#991b1b;letter-spacing:.04em">'
    '\u2694 UNDER ATTACK</div>'
    '<div style="font-size:.68rem;color:#dc2626;margin-top:2px">\u2190 target state</div>'
    '</div></div>'
)

_VIS_EXECUTE = (
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
    '<div style="background:#fef2f2;border:1.5px solid #ef4444;border-radius:10px;'
    'padding:12px 14px">'
    '<div style="font-size:.72rem;font-weight:700;color:#991b1b;letter-spacing:.04em;'
    'margin-bottom:8px">WITHOUT BATTLECHAIN</div>'
    '<div style="font-size:.81rem;color:#374151;line-height:1.6">'
    '<div style="padding:3px 0;border-bottom:1px solid #fecaca">'
    'Malicious actor exploits the bug</div>'
    '<div style="padding:3px 0;border-bottom:1px solid #fecaca">'
    '100% of funds stolen</div>'
    '<div style="padding:3px 0;border-bottom:1px solid #fecaca">'
    'No legal recourse</div>'
    '<div style="padding:4px 0;color:#dc2626;font-weight:700">'
    'Protocol: total loss</div></div></div>'
    '<div style="background:#f0fdf4;border:1.5px solid #16a34a;border-radius:10px;'
    'padding:12px 14px">'
    '<div style="font-size:.72rem;font-weight:700;color:#166534;letter-spacing:.04em;'
    'margin-bottom:8px">WITH BATTLECHAIN</div>'
    '<div style="font-size:.81rem;color:#374151;line-height:1.6">'
    '<div style="padding:3px 0;border-bottom:1px solid #bbf7d0">'
    'Whitehat operates under legal authorization</div>'
    '<div style="padding:3px 0;border-bottom:1px solid #bbf7d0">'
    '90% of funds returned to protocol</div>'
    '<div style="padding:3px 0;border-bottom:1px solid #bbf7d0">'
    '10% bounty paid to researcher</div>'
    '<div style="padding:4px 0;color:#16a34a;font-weight:700">'
    'Structured recovery \u2713</div></div></div>'
    '</div>'
    # Live balance tracker — hidden until JS populates it
    '<div id="bal-display" style="display:none;margin-top:10px;background:#f9fafb;'
    'border:1px solid #e5e7eb;border-radius:8px;padding:9px 12px">'
    '<div style="font-size:.69rem;font-weight:700;color:#6b7280;letter-spacing:.05em;'
    'margin-bottom:6px">LIVE BALANCES</div>'
    '<div style="display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:.8rem;'
    'align-items:center">'
    '<span style="color:#6b7280">Vault (BCToken)</span>'
    '<span><span id="bal-vault-b" style="color:#dc2626;font-weight:600">\u2026</span>'
    '<span style="color:#94a3b8"> \u2192 </span>'
    '<span id="bal-vault-a" style="color:#059669;font-weight:600">?</span></span>'
    '<span style="color:#6b7280">Attack seed</span>'
    '<span style="font-weight:600;color:#7c3aed">100 BCT'
    '<span style="font-weight:400;color:#9ca3af;font-size:.74rem"> deposited to open position</span>'
    '</span>'
    '<span style="color:#6b7280">Your Wallet</span>'
    '<span><span id="bal-wallet-b" style="color:#6b7280;font-weight:600">\u2026</span>'
    '<span style="color:#94a3b8"> \u2192 </span>'
    '<span id="bal-wallet-a" style="color:#059669;font-weight:600">?</span></span>'
    '</div>'
    '</div>'
)

_PAGE_CONFIGS: "dict[str, dict]" = {
    "DeployProtocol.s.sol": {
        "step": 1, "accent": "#2563eb", "accent_light": "#dbeafe",
        "icon": "01",
        "title": "Deploying Your Protocol",
        "tagline": "Deploying the protocol to BattleChain testnet",
        "visual": _VIS_DEPLOY,
        "narrative": (
            "The <strong>VulnerableVault</strong> holds protocol funds and contains a deliberately "
            "exploitable reentrancy bug. The <strong>BCToken</strong> is the asset at risk. "
            "Without a structured engagement framework, a discovered vulnerability leaves two options: "
            "disclose and hope, or watch a malicious actor move first. "
            "BattleChain provides a third path \u2014 authorized, compensated recovery."
        ),
        "tx_labels": [
            "Deploying VulnerableVault (it deploys + seeds its own token with 1,000 tokens)",
        ],
        "done_headline": "\u2713 Protocol deployed \u2014 vault is live on BattleChain",
        "done_detail":   "VulnerableVault is seeded with 1,000 BCTokens and ready.",
    },
    "McpCreateAgreement.s.sol": {
        "step": 2, "accent": "#059669", "accent_light": "#d1fae5",
        "icon": "02",
        "title": "Establishing Safe Harbor",
        "tagline": "Registering the on-chain legal framework for authorized exploitation",
        "visual": _VIS_AGREEMENT,
        "narrative": (
            "Safe harbor converts a vulnerability discovery into a structured recovery engagement. "
            "<strong>Ethical researchers</strong> who identify and return funds operate under legal "
            "protection and receive a defined bounty. This agreement is registered on-chain \u2014 "
            "immutable, auditable, and unambiguous. Both parties understand the exact terms "
            "before any engagement begins."
        ),
        "tx_labels": [
            "Creating Safe Harbor agreement on-chain",
            "Locking the agreement's commitment window",
            "Adopting the Safe Harbor agreement",
        ],
        "done_headline": "\u2713 Safe harbor agreement is live on-chain",
        "done_detail":   "10% bounty \u00b7 $5M cap \u00b7 Anonymity permitted. Researchers operate under full legal protection.",
    },
    "RequestAttackMode.s.sol": {
        "step": 3, "accent": "#ea580c", "accent_light": "#ffedd5",
        "icon": "03",
        "title": "Activating Attack Mode",
        "tagline": "Transitioning the agreement to UNDER_ATTACK — whitehat exploitation authorized",
        "visual": _VIS_ATTACK_MODE,
        "narrative": (
            "<strong>Attack mode</strong> is a formal on-chain state \u2014 <em>UNDER_ATTACK</em> \u2014 "
            "that legally opens this protocol to whitehat exploitation under the safe harbor terms. "
            "Getting there requires DAO approval: the protocol signals readiness by requesting it, "
            "then the BattleChain DAO formally grants authorization. "
            "<em>Two transactions:</em> the first submits the request to the registry; "
            "the second is the DAO approval. On testnet a permissionless mock moderator approves "
            "instantly \u2014 on mainnet this would be a real governance vote."
        ),
        "tx_labels": [
            "Requesting attack mode for the agreement",
            "DAO approving the attack mode request",
        ],
        "done_headline": "\u2713 Attack mode active \u2014 vault is open for ethical exploitation",
        "done_detail":   "Request submitted and DAO-approved. The vault is ready for the whitehat.",
    },
    "McpAttack.s.sol": {
        "step": 4, "accent": "#dc2626", "accent_light": "#fee2e2",
        "icon": "04",
        "title": "The Ethical Exploit",
        "tagline": "Executing the reentrancy exploit and recovering funds per the safe harbor agreement",
        "visual": _VIS_EXECUTE,
        "narrative": (
            "The Attacker deposits <strong>100 seed tokens</strong> to open a position in the vault, "
            "then calls <code>withdrawAll()</code>. The vault transfers tokens back before clearing "
            "its internal balance, triggering a re-entry \u2014 and the cycle repeats until the vault "
            "is empty. The full drained amount (1,000 vault + 100 seed = 1,100) is split per the "
            "safe harbor terms."
        ),
        "tx_labels": [
            "Deploying Attacker contract",
            "Executing reentrancy exploit \u2014 draining the vault",
        ],
        "done_headline": "\u2713 Ethical exploit complete \u2014 funds recovered under safe harbor",
        "done_detail":   "The vault has been drained and funds returned per the agreement terms.",
    },
}


_LOGO_ASSETS: "dict[str, tuple[str, str]]" = {
    "battlechain-wm":   ("BattleChain.svg",          "image/svg+xml"),
    "battlechain-mark": ("BattleChain-logomark.svg",  "image/svg+xml"),
    "cyfrin":           ("CYFRIN - logo - Color.png", "image/png"),
}


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
        elif self.path.startswith("/logo/"):
            entry = _LOGO_ASSETS.get(self.path[6:])
            if entry:
                filename, ct = entry
                asset_path = PROJECT_ROOT / "assets" / filename
                if asset_path.exists():
                    self._send(200, ct, asset_path.read_bytes())
                    return
            self._send(404, "text/plain", b"not found")
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

    def __init__(self, script_paths: "list[str | dict] | str",
                 page_config: "dict | None" = None) -> None:
        if isinstance(script_paths, str):
            script_paths = [script_paths]
        self.script_paths = script_paths
        self._wallet_address: str | None = None
        self._txs: list | None = None
        self._forge_error: str | None = None
        self._env_updates: dict[str, str] = {}
        self._result: dict | None = None
        self._done_event = threading.Event()
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _SigningHandler)
        port = self.server_address[1]
        self._page_config = page_config or {}
        cfg = self._page_config
        html = (_SIGNING_HTML
            .replace("CHAIN_ID_INT", CHAIN_ID)
            .replace("RPC_URL_PLACEHOLDER", RPC_URL)
            .replace("EXPLORER_WEB_PLACEHOLDER", EXPLORER_WEB))
        html = (html
            .replace("PAGE_ACCENT_LIGHT",  cfg.get("accent_light",   "#dbeafe"))
            .replace("PAGE_ACCENT",        cfg.get("accent",          "#2563eb"))
            .replace("PAGE_STEP_DOTS",     _build_dots(cfg.get("step", 0)))
            .replace("PAGE_ICON",          cfg.get("icon",            "\U0001f510"))
            .replace("PAGE_TITLE",         cfg.get("title",           "Sign Transactions"))
            .replace("PAGE_TAGLINE",       cfg.get("tagline",         "BattleChain Demo"))
            .replace("PAGE_VISUAL",        cfg.get("visual",          ""))
            .replace("PAGE_NARRATIVE",     cfg.get("narrative",       ""))
            .replace("PAGE_DONE_HEADLINE", cfg.get("done_headline",   ""))
            .replace("PAGE_DONE_DETAIL",   cfg.get("done_detail",     ""))
        )
        self.html = html
        self.url = f"http://127.0.0.1:{port}/"

    def _run_forge(self, sender: str) -> None:
        all_txs: list = []
        all_env_updates: dict[str, str] = {}
        for item in self.script_paths:
            if isinstance(item, dict):
                # Raw transaction — bypass forge, inject directly.
                all_txs.append(item)
                continue
            try:
                txs, env_updates, error = _dry_run_forge(item, sender)
                if error:
                    with self._lock:
                        self._forge_error = error
                    return
                all_txs.extend(txs)
                all_env_updates.update(env_updates)
            except Exception as exc:
                with self._lock:
                    self._forge_error = str(exc)
                return
        # Apply human-readable labels from page config
        labels = self._page_config.get("tx_labels", [])
        for i, label in enumerate(labels):
            if i < len(all_txs):
                all_txs[i]["description"] = label
        # Supplement with existing env addresses so the JS explorer-link builder
        # has context even on steps that deploy no new contracts (e.g. Attack).
        existing = read_env()
        for key in ("TOKEN_ADDRESS", "VAULT_ADDRESS", "AGREEMENT_ADDRESS", "SENDER_ADDRESS"):
            if key not in all_env_updates and existing.get(key):
                all_env_updates[key] = existing[key]
        with self._lock:
            self._env_updates = all_env_updates
            self._txs = all_txs

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
            "gas": tx.get("gas"),
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


def forge_sign_with_metamask(script_paths: "list[str | dict] | str") -> tuple[int, str]:
    """
    Start (or check) a MetaMask signing session for one or more forge scripts.
    Multiple scripts are dry-run in order and their transactions combined into
    one signing page.  Dict entries are raw transactions injected directly
    without running forge (use when on-chain state won't satisfy simulation).

    First call:  starts a local HTTP server, opens the signing page in the
                 browser, and returns (-1, url) so Claude can show the link.
    Second call: checks whether the user has finished signing.
                 Returns (0, output) on success or (1, error) on failure.
    """
    if isinstance(script_paths, str):
        script_paths = [script_paths]
    key = "+".join(Path(p).name for p in script_paths if isinstance(p, str))

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
    first_script = next((p for p in script_paths if isinstance(p, str)), None)
    page_config = _PAGE_CONFIGS.get(Path(first_script).name) if first_script else None
    srv = _SigningServer(script_paths, page_config=page_config)
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
You are guiding a non-technical user through the BattleChain security demo, step by step. \
Use battlechain tools — do not search the web or read files.

CRITICAL: All battlechain tools run on the USER'S LOCAL MACHINE via MCP — not in Claude's \
environment. Errors returned by tools are errors on the user's machine. \
Never say "my sandbox" or "my environment" — always say "your machine".

YOUR JOB: Be an active, narrating guide. Before each tool call, tell the user in plain \
English what is about to happen and why. After each tool call, explain what just happened. \
Before each MetaMask signing step, tell the user exactly what to expect in MetaMask.

Run the full demo in this order — do not skip steps or wait to be asked:

1. Tell the user the first step is setting up the environment, then call `prepare_environment`.
2. Briefly explain what's being deployed (vault + token), then call `deploy_contracts`.
3. Explain what a Safe Harbor agreement does, then call `create_agreement`.
4. Explain what "attack mode" means as an on-chain state, then call `request_and_approve_attack_mode`.
5. Explain the vault is about to be drained and funds recovered, then call `execute_attack`.
6. Show a clean, exciting summary of the full demo — the vulnerability exploited, funds moved, bounty earned.\
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
                "The vault holds 1,000 tokens; the attack will drain it completely. "
                "90% is returned to the whitehat's wallet as protocol recovery, 10% kept as the bounty. "
                "The wallet address is taken from their previous signing session automatically. "
                "Opens MetaMask signing page. Call again after signing to collect the result. "
                "Requires Step 3 (request_and_approve_attack_mode) to be complete."
            ),
            inputSchema={"type": "object", "properties": {}},
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

        rc, out = forge_sign_with_metamask("script/DeployProtocol.s.sol")
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Deployment failed.\n\n{out}")]

        env2 = read_env()
        token = env2.get("TOKEN_ADDRESS")
        vault = env2.get("VAULT_ADDRESS")

        if token and vault:
            return [types.TextContent(type="text", text=(
                "Step 1 complete.\n\n"
                "Two contracts are now live on BattleChain testnet:\n\n"
                f"  BCToken (ERC-20 asset):        {token}\n"
                f"  VulnerableVault (target):      {vault}\n\n"
                "The vault holds 1,000 BCTokens — these will be recovered in Step 4.\n\n"
                "Ready for Step 2: creating the Safe Harbor agreement."
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

        rc, out = forge_sign_with_metamask("script/McpCreateAgreement.s.sol")
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

        # Both transactions in one MetaMask signing session.
        # ApproveAttackMode is injected as a raw tx (not forge-simulated) because
        # the dry-run would fail — the agreement is still ACTIVE on-chain at that
        # point, not ATTACK_REQUESTED.  The nonces are sequential so both land in
        # the same block and execute in order.
        agreement = read_env().get("AGREEMENT_ADDRESS", "")
        # approveAttack(address) selector = keccak256("approveAttack(address)")[:4]
        approve_data = "0x351cec52" + agreement[2:].lower().zfill(64)
        rc, out = forge_sign_with_metamask([
            "script/RequestAttackMode.s.sol",
            {"to": MOCK_MODERATOR, "data": approve_data, "value": "0x0",
             "gas": None, "description": "approveAttack"},
        ])
        if rc == -1:
            return _needs_signing(out)
        if rc != 0:
            return [types.TextContent(type="text", text=f"Attack mode failed.\n\n{out}")]

        return [types.TextContent(type="text", text=(
            "Step 3 complete!\n\n"
            "The protocol is now officially in attack mode.\n\n"
            "  Request submitted: \u2713\n"
            "  Testnet moderator approved: \u2713\n"
            "  Agreement state: UNDER_ATTACK\n\n"
            "The vault is open for the authorized whitehat attack. Ready for Step 4."
        ))]

    # ── execute_attack ────────────────────────────────────────────────────────
    elif name == "execute_attack":
        if missing_keys(["TOKEN_ADDRESS", "VAULT_ADDRESS"]):
            return [types.TextContent(type="text", text="Contracts not deployed. Run deploy_contracts first.")]

        wallet = read_env().get("SENDER_ADDRESS", "").strip()
        if not wallet or not re.match(r"^0x[0-9a-fA-F]{40}$", wallet):
            return [types.TextContent(type="text", text=(
                "No wallet address on file. Please run deploy_contracts first so MetaMask "
                "can establish your signing address."
            ))]

        write_env_values({"RECOVERY_ADDRESS": wallet})

        rc, out = forge_sign_with_metamask("script/McpAttack.s.sol")
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
