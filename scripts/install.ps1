# Turnbook, installed in one step on Windows: no Python needed beforehand.
#
#   irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1 | iex
#
# It installs uv (Astral's installer, to your user folder, no administrator rights), which
# brings its own Python 3.12; installs Turnbook with it; then connects your AI apps
# (turnbook-install). Arguments after the script go to turnbook-install, e.g. --name "Your Name"
# or --join <code>. Set TURNBOOK_VERSION to install one version (CI does).
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Turnbook: installing uv (it brings Python)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$pin = if ($env:TURNBOOK_VERSION) { $env:TURNBOOK_VERSION } else { $env:GRAPH_MIND_VERSION }  # old name
$spec = if ($pin) { "turnbook==$pin" } else { "turnbook" }
# the name before 0.9.44: its commands would clash with these; the memory itself is kept
if ((uv tool list 2>$null) -match "^graph-mind-memory ") { uv tool uninstall graph-mind-memory | Out-Null }
Write-Host "Turnbook: installing $spec..."
uv tool install --python 3.12 --upgrade $spec
if ($LASTEXITCODE -ne 0) { throw "uv could not install $spec" }

$bin = (uv tool dir --bin).Trim()
& (Join-Path $bin "turnbook-install.exe") @args
if ($LASTEXITCODE -ne 0) { throw "turnbook-install failed" }
