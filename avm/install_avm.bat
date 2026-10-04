@echo off
rem AVM installer for Windows -- see install_avm_windows.ps1. "install_avm.bat -Check" only reports.
set "PS1=%~dp0install_avm_windows.ps1"
if not exist "%PS1%" set "PS1=%~dp0avm\install_avm_windows.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
echo.
pause
