# Automatic repair for link-bond: run ONCE (it asks for administrator rights). It
#   1. copies repair.ps1 to C:\ProgramData\link-bond-repair, owned by Administrators, where only administrators
#      can change anything; link-bond may only drop request files into its "requests" folder;
#   2. registers the on-demand task "link-bond-repair" (no schedule: it runs only when link-bond asks) that restarts
#      a stuck phone adapter (USB tethering / Wi-Fi) so it gets a new address and reconnects;
#   3. lets Windows find the phone hotspots link-bond has used even while the phone hides their name
#      (an iPhone stops showing its hotspot when nothing is connected): Wi-Fi profile "connect even if hidden".
# It changes nothing else. Undo: Unregister-ScheduledTask link-bond-repair; remove C:\ProgramData\link-bond-repair.
# ASCII only.
$ErrorActionPreference = 'Stop'
$me = $PSCommandPath
$out = Join-Path $PSScriptRoot 'repair-install.log'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$me`""
    exit
}
$lines = @()
function Say([string]$m) { $script:lines += ((Get-Date -Format 'HH:mm:ss') + ' ' + $m) }
try {
    $dir = Join-Path $env:ProgramData 'link-bond-repair'
    $req = Join-Path $dir 'requests'
    # A folder someone else made first (a normal user can create folders in ProgramData) is not trusted: start clean.
    if (Test-Path $dir) {
        $owner = (Get-Acl $dir).Owner
        if ($owner -notmatch 'Administrators|SYSTEM|Administratoren|Administrateurs') {
            Remove-Item $dir -Recurse -Force
            Say "removed a folder owned by $owner"
        }
    }
    New-Item -ItemType Directory -Force -Path $req | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'repair.ps1') (Join-Path $dir 'repair.ps1') -Force
    # Owner Administrators, everything below too. Administrators + SYSTEM: full. Users: read and run.
    & icacls.exe $dir /setowner '*S-1-5-32-544' /T /C | Out-Null
    & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /T /C | Out-Null
    # requests: Users may only ADD files (not change the folder); a file they add belongs to them (CREATOR OWNER).
    & icacls.exe $req /grant '*S-1-5-32-545:(WD)' '*S-1-3-0:(OI)(IO)(M)' | Out-Null
    Say "repair helper copied to $dir (owner: Administrators)"

    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $dir 'repair.ps1') + '"')
    $pri = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'link-bond-repair' -Action $act -Principal $pri -Settings $set -Description 'link-bond: restart a stuck phone adapter when link-bond asks (on demand only)' -Force | Out-Null
    Say "task link-bond-repair registered (on demand, for $user)"

    # phone hotspots link-bond has used as links: connect even when the phone hides the name
    $wifi = Join-Path $PSScriptRoot 'wifi.json'
    if (Test-Path $wifi) {
        $names = @((Get-Content $wifi -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { [string]$_.Value } | Select-Object -Unique)
        foreach ($n in $names) {
            $r = & netsh.exe wlan set profileparameter name="$n" nonBroadcast=yes connectionmode=auto 2>&1
            Say ("Wi-Fi profile '" + $n + "': connect even if hidden -> " + (($r | Out-String).Trim() -replace '\s+', ' '))
        }
    } else { Say 'no wifi.json yet: no Wi-Fi profile changed' }
    Say 'done'
} catch {
    Say ('FAILED: ' + $_.Exception.Message)
}
$lines | Set-Content -Path $out -Encoding UTF8
