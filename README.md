# BattleChain Demo

**What you need:** Claude Desktop, MetaMask, Python 3.11+

**Supported platforms:** macOS, Linux, or WSL (Windows Subsystem for Linux)

---

## Setup (one command)

**macOS or Linux** — run in Terminal:
```bash
curl -fsSL https://raw.githubusercontent.com/Cyfrin/battlechain-mcp/main/install.sh | bash
```

**WSL** — run in PowerShell (Windows Terminal), not inside WSL:
```powershell
irm https://raw.githubusercontent.com/Cyfrin/battlechain-mcp/main/install.ps1 | iex
```

Restart Claude Desktop when it finishes.

---

## Start the demo

Paste this into Claude Desktop:

```
Use your battlechain tools to run the security demo. Start by calling
prepare_environment right now, then walk me through each step in plain
English. I have MetaMask. Ask me to confirm before the attack.
```

Claude handles everything from there.
