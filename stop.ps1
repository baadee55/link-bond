# Stop link-bond completely (including the floating box), give the PC its original proxy settings back,
# and remove the logon auto-start.
$ErrorActionPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'proxy.ps1')
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*link-bond*run.ps1*' -or $_.CommandLine -like '*bond.py*' -or $_.CommandLine -like '*widget.pyw*' -or
    $_.Name -eq 'link-bond.exe' -or $_.Name -eq 'link-bond-widget.exe' } |
    Where-Object { $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
Set-BondProxy $false
$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $startup 'link-bond.lnk') -Force
Remove-Item (Join-Path $startup 'link-bond-widget.lnk') -Force
Write-Output 'link-bond stopped; proxy restored.'
