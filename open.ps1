# The one desktop icon: make sure link-bond is running, then show its small floating box. ASCII source.
# Starting it again just brings the existing box forward.
param([switch]$NoWindow)
$ErrorActionPreference = 'SilentlyContinue'
$url = 'http://127.0.0.1:1081/'

function Test-Up { try { $null = Invoke-RestMethod -Uri ($url + 'status.json') -TimeoutSec 2; return $true } catch { return $false } }

if (-not (Test-Up)) {
    & (Join-Path $PSScriptRoot 'start-hidden.ps1') | Out-Null     # starts the supervisor + registers start at logon
    for ($i = 0; $i -lt 30 -and -not (Test-Up); $i++) { Start-Sleep 1 }
}
if ($NoWindow) { if (Test-Up) { 'running' } else { 'not running' }; return }

$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
$exe = Join-Path $PSScriptRoot 'link-bond-widget.exe'
if (Test-Path $exe) {
    Start-Process $exe -WorkingDirectory $PSScriptRoot
} elseif ($pyw) {
    Start-Process $pyw -ArgumentList "`"$(Join-Path $PSScriptRoot 'widget.pyw')`"" -WorkingDirectory $PSScriptRoot
} else {
    # no tkinter / pythonw: fall back to the dashboard in its own app-style window (separate profile, no browser history)
    $b = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($b) { Start-Process $b -ArgumentList "--app=$url", "--user-data-dir=`"$(Join-Path $PSScriptRoot 'ui-profile')`"", '--no-first-run' } else { Start-Process $url }
}
