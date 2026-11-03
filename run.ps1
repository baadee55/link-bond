# Supervisor: keeps link-bond running and applies the PC-wide switch.
#  - The Windows proxy is ON only while link-bond is really working AND the dashboard switch is ON (no power.off file).
#    "Working" = it accepts connections on 1080 and answers its status page; a hung link-bond counts as dead.
#  - If link-bond dies, the proxy is removed at once (internet keeps working) and link-bond is restarted.
#  - If a just-installed update keeps dying right after start, the previous version (update-backup) is put back.
#  - The floating box is kept alive too (it touches widget.beat); "Quit all" stops this supervisor with it.
#  - It touches supervisor.beat every few seconds so the dashboard can tell that this safety net is alive.
#  - When this file itself is replaced (an update), it restarts itself so the new rules apply.
# ASCII only (Windows PowerShell 5.1 reads files without a BOM as the ANSI code page).
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is slow with a progress bar
[System.Net.WebRequest]::DefaultWebProxy = $null   # talk to link-bond directly, never through the proxy it runs
. (Join-Path $PSScriptRoot 'proxy.ps1')

$mutex = New-Object Threading.Mutex($false, 'Local\link-bond-supervisor')
if (-not $mutex.WaitOne(0)) { exit }   # already running

function Test-Listening {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 1080); $c.Close(); return $true } catch { return $false }
}
function Test-Answering {     # returns the status object, or $null when link-bond does not answer
    try { return (Invoke-WebRequest -Uri 'http://127.0.0.1:1081/status.json' -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json } catch { return $null }
}
function Test-Web([switch]$ViaBond) {   # one tiny request (~1 KB): through link-bond, or straight out
    $a = @('-s', '-o', 'NUL', '-m', '8', '-w', '%{http_code}')
    if ($ViaBond) { $a += @('-x', 'http://127.0.0.1:1080') } else { $a += @('--noproxy', '*') }
    $code = & curl.exe @a 'http://cp.cloudflare.com/generate_204' 2>$null
    return ($code -eq '204')
}
function Start-Widget {
    $wexe = Join-Path $PSScriptRoot 'link-bond-widget.exe'
    if (Test-Path $wexe) { Start-Process $wexe -ArgumentList '--background' -WorkingDirectory $PSScriptRoot; return }
    $pyw = Get-Command pythonw -ErrorAction SilentlyContinue
    # --background: if a box is already open (even hidden), leave it as it is - do not pop it up
    if ($pyw) { Start-Process $pyw.Source -ArgumentList "`"$(Join-Path $PSScriptRoot 'widget.pyw')`"", '--background' -WorkingDirectory $PSScriptRoot }
}
function Test-WidgetBeat {   # the box writes widget.beat every 5 s while it runs (hidden or not)
    $f = Get-Item (Join-Path $PSScriptRoot 'widget.beat') -ErrorAction SilentlyContinue
    return ($f -and ((Get-Date) - $f.LastWriteTime).TotalSeconds -lt 30)
}
function Restore-Previous {  # put back the version that ran before the last update
    $bak = Join-Path $PSScriptRoot 'update-backup'
    if (-not (Test-Path (Join-Path $bak 'bond.py'))) { return $false }
    $failed = ''
    try { $failed = [string](Get-Content (Join-Path $PSScriptRoot 'update-pending.json') -Raw | ConvertFrom-Json).to } catch { }
    $added = @()
    try { $added = @((Get-Content (Join-Path $PSScriptRoot 'update-pending.json') -Raw | ConvertFrom-Json).files) } catch { }
    foreach ($f in $added) {     # files the update brought that the old version did not have: remove them
        $rel = ([string]$f) -replace '/', '\'
        if ($rel -and -not $rel.Contains('..') -and -not (Test-Path (Join-Path $bak $rel))) { Remove-Item (Join-Path $PSScriptRoot $rel) -Force }
    }
    Copy-Item -Path (Join-Path $bak '*') -Destination $PSScriptRoot -Recurse -Force
    Remove-Item (Join-Path $PSScriptRoot 'update-pending.json') -Force
    # last word = the version that failed: link-bond will not install it again
    Set-Content -Path (Join-Path $PSScriptRoot 'update-rolled-back.txt') -Value ((Get-Date -Format 'yyyy-MM-dd_HH:mm:ss') + ' ' + $failed).Trim()
    return $true
}
function Test-SelfChanged {  # this file was replaced (and the new one parses): $true once per change
    $t = (Get-Item $self).LastWriteTime
    if ($t -eq $script:selfTime) { return $false }
    $errs = $null
    [void][System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw $self), [ref]$errs)
    if ($errs) { return $false }    # still being written, or broken: look again next time
    $script:selfTime = $t
    return $true
}
function Save-OldLogs {      # keep the logs of the last 3 runs (a crash message must not be lost by the restart)
    foreach ($n in 'bond.log', 'bond.err.log') {
        $f = Join-Path $PSScriptRoot $n
        if (-not (Test-Path $f)) { continue }
        $b = [IO.Path]::GetFileNameWithoutExtension($n)
        Remove-Item (Join-Path $PSScriptRoot "$b.3.log") -Force
        if (Test-Path (Join-Path $PSScriptRoot "$b.2.log")) { Rename-Item (Join-Path $PSScriptRoot "$b.2.log") "$b.3.log" }
        if (Test-Path (Join-Path $PSScriptRoot "$b.1.log")) { Rename-Item (Join-Path $PSScriptRoot "$b.1.log") "$b.2.log" }
        Rename-Item $f "$b.1.log"
    }
}

$self = $PSCommandPath
$selfTime = (Get-Item $self).LastWriteTime
$beat = Join-Path $PSScriptRoot 'supervisor.beat'
$powerOff = Join-Path $PSScriptRoot 'power.off'
$pending = Join-Path $PSScriptRoot 'update-pending.json'
$exe = Join-Path $PSScriptRoot 'link-bond.exe'   # the packaged release has an .exe; otherwise run the Python file
# A copy of python.exe named linkbond.exe (made by install / VPN setup) lets a VPN's split tunneling exclude
# link-bond alone - not every Python program on the PC.
$py = 'python'
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) { $own = Join-Path (Split-Path $pyCmd.Source) 'linkbond.exe'; if (Test-Path $own) { $py = $own } }

Set-BondProxy $false   # never leave a stale proxy setting from a previous crash
$quickFails = 0; $lastWidgetStart = [datetime]::MinValue; $reload = $null

while ($true) {
    Save-OldLogs
    $log = @{ RedirectStandardOutput = (Join-Path $PSScriptRoot 'bond.log'); RedirectStandardError = (Join-Path $PSScriptRoot 'bond.err.log') }
    if (Test-Path $exe) {
        $p = Start-Process $exe -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru @log
    } else {
        $p = Start-Process $py -ArgumentList '-I', 'bond.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru @log
    }
    $started = Get-Date
    $applied = $false; $up = $false; $fails = 0; $waited = 0; $tick = 0; $silent = 0; $broken = 0
    while (-not $p.HasExited -and $fails -lt 2 -and $silent -lt 3 -and $broken -lt 2) {
        Set-Content -Path $beat -Value (Get-Date).Ticks
        $ok = Test-Listening
        if ($ok) { $up = $true; $fails = 0 }
        elseif ($up) { $fails++ }                                  # was listening, now is not: 2 misses = dead
        else { $waited++; if ($waited -ge 20) { break } }          # never started listening within ~40 s
        $tick++
        if ($ok -and $tick % 5 -eq 0) {                            # every ~10 s: does it still answer? (3 misses = hung)
            $st = Test-Answering
            if ($st) { $silent = 0 } else { $silent++ }
            # Fail-open: link-bond answers but carries nothing (a bug, a stuck state)? When nothing went through it
            # for a minute, try one tiny request through it; if that fails twice while the internet works without it,
            # the proxy is removed and link-bond restarted. Idle PCs pay ~1 KB a minute for this, busy ones nothing.
            if ($st -and ($tick % 30 -eq 0 -or $broken -gt 0) -and $st.last_open_age -gt 60 -and -not (Test-Path $powerOff)) {
                if (Test-Web -ViaBond) { $broken = 0 }
                elseif (Test-Web) {
                    $broken++
                    if ($broken -ge 2) {
                        Add-Content -Path (Join-Path $PSScriptRoot 'supervisor.log') -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' link-bond carried nothing while the internet worked without it: proxy off, restarting it')
                    }
                }
            }
        }
        $want = $ok -and $silent -lt 2 -and (-not (Test-Path $powerOff))   # one slow answer is not a failure
        if ($want -ne $applied) { Set-BondProxy $want; $applied = $want }
        if ($ok -and $tick % 5 -eq 0 -and -not (Test-WidgetBeat) -and ((Get-Date) - $lastWidgetStart).TotalSeconds -gt 60) {
            Start-Widget; $lastWidgetStart = Get-Date
        }
        if ($tick % 5 -eq 0 -and -not $reload -and (Test-SelfChanged)) {
            $reload = Get-Date              # this file was replaced by an update: switch over when link-bond restarts
        }
        if ($reload -and ((Get-Date) - $reload).TotalMinutes -gt 35) { break }  # it never did: switch now
        Start-Sleep 2
    }
    # link-bond may restart into an update a few seconds before we noticed run.ps1 changed too: look once more
    if (-not $reload -and $p.HasExited -and (Test-SelfChanged)) { $reload = Get-Date }
    if ($reload) {              # start the new supervisor (it starts link-bond again) and leave
        Set-BondProxy $false
        if (-not $p.HasExited) { $p.Kill() }
        $mutex.ReleaseMutex()
        Start-Process powershell.exe -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', "`"$self`"" -WindowStyle Hidden
        exit
    }
    Set-BondProxy $false
    if (-not $p.HasExited) { $p.Kill() }
    # A new version that dies within 90 s three times in a row is rolled back to the previous one.
    # (the old version that just installed the update and restarted is not a failure of the new one)
    $pf = Get-Item $pending -ErrorAction SilentlyContinue
    $installedByThisRun = $pf -and $pf.LastWriteTime -gt $started
    if (((Get-Date) - $started).TotalSeconds -lt 90 -and -not $installedByThisRun) { $quickFails++ } else { $quickFails = 0 }
    if ($quickFails -ge 3 -and (Test-Path $pending)) {
        if (Restore-Previous) { $quickFails = 0 }
    }
    Start-Sleep 2
}
