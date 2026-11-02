# Put ONE icon on the desktop and start link-bond. Everything else is done on its screen. (ASCII source on purpose.)
$dir = $PSScriptRoot
if (-not (Test-Path (Join-Path $dir 'link-bond.exe')) -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Output 'Python was not found. Install Python 3.9+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run install.bat again.'
    exit 1
}
# linkbond.exe = a copy of python.exe, so a VPN's split tunneling can exclude link-bond alone (see README "VPN")
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) { Copy-Item $pyCmd.Source (Join-Path (Split-Path $pyCmd.Source) 'linkbond.exe') -Force -ErrorAction SilentlyContinue }
$desktop = [Environment]::GetFolderPath('Desktop')
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'link-bond.lnk'))
$s.TargetPath = 'powershell.exe'
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\open.ps1`""
$s.WorkingDirectory = $dir
$s.Description = 'link-bond: save mobile data and use several connections at once (opens its window)'
$s.Save()
& (Join-Path $dir 'open.ps1')
Write-Output "Done. Use the 'link-bond' icon on your desktop."
