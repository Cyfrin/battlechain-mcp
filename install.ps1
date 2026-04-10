# BattleChain MCP installer for WSL users.
# Run this in PowerShell (Windows Terminal), NOT inside WSL.
#
#   irm https://raw.githubusercontent.com/Cyfrin/battlechain-mcp/main/install.ps1 | iex

Write-Host "Installing BattleChain..."

# ── Install Python package inside WSL ─────────────────────────────────────────
# *>&1 | Out-Null suppresses all streams so pip's stderr warnings don't get
# misread as PowerShell errors (NativeCommandError).
$installed = $false
foreach ($py in @("python3.11", "python3.12", "python3.10", "python3.13")) {
    wsl -- $py -m pip install "git+https://github.com/Cyfrin/battlechain-mcp.git" --quiet *>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $installed = $true
        break
    }
}

if (-not $installed) {
    Write-Host "ERROR: Could not install the package in WSL."
    Write-Host "Make sure WSL is running and has Python 3.10+ installed."
    exit 1
}

# ── Write Claude Desktop config (native Windows, no interop needed) ───────────
$configDir  = "$env:APPDATA\Claude"
$configFile = "$configDir\claude_desktop_config.json"

$cfg = [PSCustomObject]@{}
if (Test-Path $configFile) {
    try { $cfg = Get-Content $configFile -Raw | ConvertFrom-Json } catch {}
}

if (-not ($cfg.PSObject.Properties.Name -contains "mcpServers")) {
    $cfg | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([PSCustomObject]@{})
}

# Use bash -lc so WSL starts a login shell with ~/.local/bin in PATH
$bcEntry = [PSCustomObject]@{ command = "wsl"; args = @("bash", "-lc", "battlechain-mcp") }

if ($cfg.mcpServers.PSObject.Properties.Name -contains "battlechain") {
    $cfg.mcpServers.battlechain = $bcEntry
} else {
    $cfg.mcpServers | Add-Member -NotePropertyName "battlechain" -NotePropertyValue $bcEntry
}

New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$cfg | ConvertTo-Json -Depth 10 | Set-Content $configFile -Encoding UTF8

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
