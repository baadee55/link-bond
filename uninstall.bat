@echo off
rem Stop link-bond completely (restores the proxy settings, removes the logon auto-start) and remove the desktop icon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
del "%USERPROFILE%\Desktop\link-bond.lnk" 2>nul
echo link-bond is stopped and the desktop icon was removed.
pause
