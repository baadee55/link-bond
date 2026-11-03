# link-bond repair helper. Runs with administrator rights as the scheduled task "link-bond-repair" (installed once by
# install-repair.ps1, which copies this file to C:\ProgramData\link-bond-repair where only administrators can change it).
# link-bond asks for a repair by dropping a file "<ifIndex>.req" into ...\requests. This script only ever:
#   - restarts a network adapter that is a phone link (USB tethering / Wi-Fi), so it asks for a new address and
#     reconnects. Nothing else: no services, no settings, no other adapters.
# Limits: the same adapter at most once every 5 minutes and 12 times a day. Every action is written to repair.log
# (next to this script: only administrators can write there, everyone can read it).
# ASCII only.
$ErrorActionPreference = 'SilentlyContinue'
$base = Split-Path -Parent $PSCommandPath
$req = Join-Path $base 'requests'
$log = Join-Path $base 'repair.log'
$stateFile = Join-Path $base 'state.json'
$inv = [Globalization.CultureInfo]::InvariantCulture
function Log([string]$m) { Add-Content -Path $log -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss', $inv) + ' ' + $m) }
function Is-Link($item) { return ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }

$ri = Get-Item -LiteralPath $req -Force
if (-not $ri -or (Is-Link $ri)) { Log 'requests folder missing or redirected - refused'; exit }

$state = @{}
try { (Get-Content $stateFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $state[$_.Name] = @($_.Value) } } catch { }
$today = (Get-Date).ToString('yyyy-MM-dd', $inv)
$allowed = 'Remote NDIS|RNDIS|Apple Mobile Device Ethernet|Wi-Fi|Wireless|802\.11|WLAN|USB.*Ethernet|Ethernet.*USB'

foreach ($f in Get-ChildItem -LiteralPath $req -Filter '*.req' -File -Force) {
    if (Is-Link $f) { Log "$($f.Name): a link, not a file - ignored"; continue }
    $idx = 0
    [void][int]::TryParse($f.BaseName, [ref]$idx)
    Remove-Item -LiteralPath $f.FullName -Force
    if ($idx -le 0) { continue }
    $a = Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue
    if (-not $a) { Log "ifIndex $idx`: no such adapter"; continue }
    if ($a.InterfaceDescription -notmatch $allowed -or $a.InterfaceDescription -match 'Virtual|VPN|TAP|WireGuard|Nord|Tailscale|Hyper-V|Bluetooth') {
        Log "ifIndex $idx ($($a.Name)): not a phone link - refused"; continue
    }
    $key = [string]$idx
    $times = @($state[$key] | Where-Object { $_ -like "$today*" })
    $last = $times | Select-Object -Last 1
    $lastAt = [datetime]::MinValue
    if ($last) { [void][datetime]::TryParseExact($last, 'yyyy-MM-dd HH:mm:ss', $inv, [Globalization.DateTimeStyles]::None, [ref]$lastAt) }
    if (((Get-Date) - $lastAt).TotalMinutes -lt 5) { Log "$($a.Name): asked again within 5 min - skipped"; continue }
    if ($times.Count -ge 12) { Log "$($a.Name): 12 restarts today already - skipped"; continue }
    Log "$($a.Name) ($($a.InterfaceDescription)): restarting the adapter"
    $a | Restart-NetAdapter -Confirm:$false
    $state[$key] = @($times) + @((Get-Date).ToString('yyyy-MM-dd HH:mm:ss', $inv))
}
try { ($state | ConvertTo-Json -Depth 3) | Set-Content -Path $stateFile -Encoding ASCII } catch { }
