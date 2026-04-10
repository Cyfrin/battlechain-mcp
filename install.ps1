# BattleChain MCP installer for WSL users.
# Run this in PowerShell (Windows Terminal), NOT inside WSL.
#
#   irm https://raw.githubusercontent.com/Cyfrin/battlechain-mcp/main/install.ps1 | iex

Write-Host "Installing BattleChain..."

# ── Install Python package inside WSL ─────────────────────────────────────────
# Run inside bash with 2>/dev/null so stderr never reaches PowerShell.
# Track which Python version succeeds so we can use it in the config.
$pythonVersion = $null
foreach ($py in @("python3.11", "python3.12", "python3.10", "python3.13", "python3")) {
    # Bootstrap pip via ensurepip if it isn't installed, then install the package.
    wsl -- bash -c "$py -m ensurepip --upgrade 2>/dev/null; $py -m pip install 'https://github.com/Cyfrin/battlechain-mcp/archive/refs/heads/main.zip' --force-reinstall --quiet 2>/dev/null"
    if ($LASTEXITCODE -eq 0) {
        # Verify this is actually 3.10+ before accepting it
        $ver = wsl -- bash -c "$py -c 'import sys; print(sys.version_info >= (3,10))' 2>/dev/null"
        if ($ver -eq "True") {
            $pythonVersion = $py
            break
        }
    }
}

if (-not $pythonVersion) {
    Write-Host ""
    Write-Host "ERROR: Could not install the package in WSL."
    Write-Host "Make sure your WSL distro has Python 3.10+ installed."
    Write-Host "To install it, open WSL and run: sudo apt-get install -y python3 python3-pip"
    Read-Host "Press Enter to close"
    exit 1
}

# ── Write Claude Desktop config (native Windows, no interop needed) ───────────
# Claude Desktop installed via the Windows Store uses a virtualized AppData path
# under LocalPackages rather than the standard %APPDATA% location.
$claudePackageDir = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "Claude_*" } |
    Select-Object -First 1

if ($claudePackageDir) {
    $configDir = "$($claudePackageDir.FullName)\LocalCache\Roaming\Claude"
} else {
    $configDir = "$env:APPDATA\Claude"
}

$configFile = "$configDir\claude_desktop_config.json"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

# Invoke Python directly by version — avoids login shell PATH issues entirely.
# ($pythonVersion is e.g. "python3.11", always in /usr/bin on standard WSL)
$json = @"
{
  "mcpServers": {
    "battlechain": {
      "command": "wsl",
      "args": ["$pythonVersion", "-m", "battlechain_mcp"]
    }
  }
}
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($configFile, $json, $utf8NoBom)

Write-Host ""
Write-Host "Done!"
Write-Host ""
Write-Host "  1. Restart Claude Desktop"
Write-Host "  2. Paste this into the chat:"
Write-Host ""
Write-Host "     Use your battlechain tools to run the security demo. Start by calling"
Write-Host "     prepare_environment right now, then walk me through each step in plain"
Write-Host "     English. I have MetaMask. Ask me to confirm before the attack."
Write-Host ""
