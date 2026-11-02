# Supervisor: keeps link-bond running and applies the PC-wide switch.
#  - The Windows proxy is ON only while link-bond is really listening AND the dashboard switch is ON (no power.off file).
#  - If link-bond dies, the proxy is removed at once (internet keeps working) and link-bond is restarted.
#  - It touches supervisor.beat every few seconds so the dashboard can tell that this safety net is alive.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'proxy.ps1')

$mutex = New-Object Threading.Mutex($false, 'Local\link-bond-supervisor')
if (-not $mutex.WaitOne(0)) { exit }   # already running

function Test-Listening {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 1080); $c.Close(); return $true } catch { return $false }
}

$beat = Join-Path $PSScriptRoot 'supervisor.beat'
$powerOff = Join-Path $PSScriptRoot 'power.off'
$exe = Join-Path $PSScriptRoot 'link-bond.exe'   # the packaged release has an .exe; otherwise run the Python file
# A copy of python.exe named linkbond.exe (made by install / VPN setup) lets a VPN's split tunneling exclude
# link-bond alone - not every Python program on the PC.
$py = 'python'
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) { $own = Join-Path (Split-Path $pyCmd.Source) 'linkbond.exe'; if (Test-Path $own) { $py = $own } }

Set-BondProxy $false   # never leave a stale proxy setting from a previous crash

while ($true) {
    $log = @{ RedirectStandardOutput = (Join-Path $PSScriptRoot 'bond.log'); RedirectStandardError = (Join-Path $PSScriptRoot 'bond.err.log') }
    if (Test-Path $exe) {
        $p = Start-Process $exe -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru @log
    } else {
        $p = Start-Process $py -ArgumentList '-I', 'bond.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru @log
    }
    $applied = $false; $up = $false; $fails = 0; $waited = 0
    while (-not $p.HasExited -and $fails -lt 2) {
        Set-Content -Path $beat -Value (Get-Date).Ticks
        $ok = Test-Listening
        if ($ok) { $up = $true; $fails = 0 }
        elseif ($up) { $fails++ }                                  # was listening, now is not: 2 misses = dead
        else { $waited++; if ($waited -ge 20) { break } }          # never started listening within ~40 s
        $want = $ok -and (-not (Test-Path $powerOff))
        if ($want -ne $applied) { Set-BondProxy $want; $applied = $want }
        Start-Sleep 2
    }
    Set-BondProxy $false
    if (-not $p.HasExited) { $p.Kill() }
    Start-Sleep 2
}
