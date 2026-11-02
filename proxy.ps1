# Helpers to switch the Windows (per-user) system proxy to link-bond and back. ASCII only on purpose.
$script:Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$script:Backup = Join-Path $PSScriptRoot 'proxy-backup.json'
$script:BondServer = '127.0.0.1:1080'
$script:BondOverride = 'localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;192.168.*;100.*;<local>'

function Update-ProxyNow {
    if (-not ('LinkBond.Wininet' -as [type])) {
        Add-Type -Namespace LinkBond -Name Wininet -MemberDefinition '[DllImport("wininet.dll", SetLastError=true)] public static extern bool InternetSetOption(IntPtr h, int o, IntPtr b, int l);'
    }
    [void][LinkBond.Wininet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)  # settings changed
    [void][LinkBond.Wininet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)  # refresh
}

function Get-Prop($name) { (Get-ItemProperty -Path $script:Key -Name $name -ErrorAction SilentlyContinue).$name }

function Set-BondProxy([bool]$on) {
    $cur = Get-Prop 'ProxyServer'
    if ($on) {
        # keep the user's original settings once (never overwrite the backup with our own values)
        if (-not (Test-Path $script:Backup) -and $cur -ne $script:BondServer) {
            @{ ProxyEnable = (Get-Prop 'ProxyEnable'); ProxyServer = $cur; ProxyOverride = (Get-Prop 'ProxyOverride') } |
                ConvertTo-Json | Set-Content -Path $script:Backup -Encoding UTF8
        }
        Set-ItemProperty -Path $script:Key -Name ProxyServer -Value $script:BondServer
        Set-ItemProperty -Path $script:Key -Name ProxyOverride -Value $script:BondOverride
        Set-ItemProperty -Path $script:Key -Name ProxyEnable -Value 1 -Type DWord
    } else {
        if ($cur -eq $script:BondServer) {
            $b = $null
            if (Test-Path $script:Backup) { $b = Get-Content $script:Backup -Raw | ConvertFrom-Json }
            if ($b -and $b.ProxyServer -and $b.ProxyServer -ne $script:BondServer) {
                Set-ItemProperty -Path $script:Key -Name ProxyServer -Value $b.ProxyServer
                if ($b.ProxyOverride) { Set-ItemProperty -Path $script:Key -Name ProxyOverride -Value $b.ProxyOverride }
                Set-ItemProperty -Path $script:Key -Name ProxyEnable -Value ([int]$b.ProxyEnable) -Type DWord
            } else {
                Set-ItemProperty -Path $script:Key -Name ProxyEnable -Value 0 -Type DWord
                Remove-ItemProperty -Path $script:Key -Name ProxyServer -ErrorAction SilentlyContinue
                Remove-ItemProperty -Path $script:Key -Name ProxyOverride -ErrorAction SilentlyContinue
            }
        }
    }
    Update-ProxyNow
}
