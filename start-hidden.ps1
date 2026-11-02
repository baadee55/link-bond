# Start the supervisor hidden now, and register it (and the small floating box) to start at every logon
# (Startup folder shortcuts, no admin needed). Starting it explicitly means "ON": the dashboard's OFF flag is cleared.
$startup = [Environment]::GetFolderPath('Startup')
$sh = New-Object -ComObject WScript.Shell
Remove-Item (Join-Path $PSScriptRoot 'power.off') -Force -ErrorAction SilentlyContinue

$run = Join-Path $PSScriptRoot 'run.ps1'
$args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
$s = $sh.CreateShortcut((Join-Path $startup 'link-bond.lnk'))
$s.TargetPath = 'powershell.exe'
$s.Arguments = $args
$s.WorkingDirectory = $PSScriptRoot
$s.WindowStyle = 7
$s.Save()

$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
$widget = Join-Path $PSScriptRoot 'widget.pyw'
if ($pyw -and (Test-Path $widget)) {
    $w = $sh.CreateShortcut((Join-Path $startup 'link-bond-widget.lnk'))
    $w.TargetPath = $pyw
    $w.Arguments = "`"$widget`""
    $w.WorkingDirectory = $PSScriptRoot
    $w.Save()
}
Start-Process powershell.exe -ArgumentList $args -WindowStyle Hidden
Write-Output "started; startup shortcuts registered in: $startup"
