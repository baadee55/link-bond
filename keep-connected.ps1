# Keep every link (Wi-Fi AND USB / wired) connected at the same time, and stop Windows from dropping them to save
# power. Run once; Windows asks for administrator permission once. Nothing is restarted: the links stay up while it
# runs, and adapter settings take effect the next time an adapter reconnects. Undo: .\keep-connected.ps1 -Undo
# (ASCII source on purpose.)
param([switch]$Undo)
$log = Join-Path $PSScriptRoot 'keep-connected.log'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"") + $(if ($Undo) { '-Undo' })
    Start-Process powershell -Verb RunAs -ArgumentList $a -Wait
    if (Test-Path $log) { Get-Content $log }
    exit
}
$out = New-Object System.Collections.Generic.List[string]
function Note($s) { $out.Add("$(Get-Date -Format 'HH:mm:ss')  $s") }
$usbSub = '2a737441-1930-4402-8d77-b2bebba308a3'; $usbSet = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'   # USB selective suspend
$wlSub = '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1'; $wlSet = '12bbebe6-58d6-4636-95bb-3217ef867c1a'     # wireless power saving
$wcm = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WcmSvc\GroupPolicy'
$classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}'  # network adapters
# real network adapters: Wi-Fi, USB tethering (Android RNDIS, iPhone), USB modems, Ethernet - not VPN / virtual ones
$nics = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue)
$nics += @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match 'Remote NDIS|RNDIS|Apple Mobile Device|Mobile Broadband|USB' })
$nics = $nics | Where-Object { $_.InterfaceDescription -notmatch 'VPN|TAP-|WireGuard|NordLynx|Tailscale|Hyper-V|Virtual|Bluetooth' } | Sort-Object Name -Unique
try {
    if ($Undo) {
        Remove-ItemProperty -Path $wcm -Name fMinimizeConnections -ErrorAction SilentlyContinue
        powercfg /setacvalueindex SCHEME_CURRENT $usbSub $usbSet 1; powercfg /setdcvalueindex SCHEME_CURRENT $usbSub $usbSet 1
        powercfg /setactive SCHEME_CURRENT
        foreach ($n in $nics) {
            Get-ChildItem $classKey -ErrorAction SilentlyContinue |
                Where-Object { (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).NetCfgInstanceId -eq $n.InterfaceGuid } |
                ForEach-Object { Remove-ItemProperty -Path $_.PSPath -Name PnPCapabilities -ErrorAction SilentlyContinue }
        }
        Note 'undone: Windows defaults for simultaneous connections, USB and adapter power saving'
    } else {
        # 1. never disconnect one link because another one came up
        if (-not (Test-Path $wcm)) { New-Item -Path $wcm -Force | Out-Null }
        New-ItemProperty -Path $wcm -Name fMinimizeConnections -PropertyType DWord -Value 0 -Force | Out-Null
        Note 'simultaneous connections: allowed (fMinimizeConnections = 0)'
        # 2. never suspend USB (phones on USB tethering) and never put the Wi-Fi radio to sleep
        powercfg /setacvalueindex SCHEME_CURRENT $usbSub $usbSet 0; powercfg /setdcvalueindex SCHEME_CURRENT $usbSub $usbSet 0
        powercfg /setacvalueindex SCHEME_CURRENT $wlSub $wlSet 0; powercfg /setdcvalueindex SCHEME_CURRENT $wlSub $wlSet 0
        powercfg /setactive SCHEME_CURRENT
        Note 'power: USB selective suspend OFF, Wi-Fi power saving = maximum performance'
        # 3. Windows may not switch the adapters off to save power ("Allow the computer to turn off this device")
        foreach ($n in $nics) {
            try {
                Set-NetAdapterPowerManagement -Name $n.Name -AllowComputerToTurnOffDevice Disabled -NoRestart -ErrorAction Stop
                Note "adapter '$($n.Name)': Windows may no longer turn it off"
            } catch {
                # older / other builds: the same switch lives in the adapter's driver key (PnPCapabilities 24 = never turn off)
                $k = Get-ChildItem $classKey -ErrorAction SilentlyContinue |
                    Where-Object { (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).NetCfgInstanceId -eq $n.InterfaceGuid } | Select-Object -First 1
                if ($k) {
                    Set-ItemProperty -Path $k.PSPath -Name PnPCapabilities -Value 24 -Type DWord
                    Note "adapter '$($n.Name)': Windows may no longer turn it off (applies at the next reconnect)"
                } else { Note "adapter '$($n.Name)': power setting not found" }
            }
        }
        # 4. Wi-Fi: keep the radio fully awake and do not roam away to other saved networks
        $wifi = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.NdisPhysicalMedium -eq 9 -or $_.Name -eq 'Wi-Fi' } | Select-Object -First 1
        if ($wifi) {
            foreach ($p in @(@('MIMO Power Save Mode', 'No SMPS'), @('Roaming Aggressiveness', '1. Lowest'))) {
                try {
                    Set-NetAdapterAdvancedProperty -Name $wifi.Name -DisplayName $p[0] -DisplayValue $p[1] -NoRestart -ErrorAction Stop
                    Note "Wi-Fi '$($p[0])' = '$($p[1])' (applies at the next reconnect)"
                } catch { Note "Wi-Fi '$($p[0])': not available on this adapter" }
            }
        }
        gpupdate /target:computer 2>&1 | Out-Null
        Note 'done - nothing was restarted, your links stayed up'
    }
} catch { Note "failed: $($_.Exception.Message)" }
$out | Set-Content $log
