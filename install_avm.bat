@echo off
rem AVM installer for Windows. On its own (e.g. just downloaded) it fetches the
rem installer script from GitHub, which downloads AVM into %USERPROFILE%\avm.
rem   install_avm.bat            install / repair
rem   install_avm.bat -Update    also fetch the latest AVM from GitHub
rem   install_avm.bat -Check     only report what's missing
setlocal
set "REPO=m0dts/avm"
set "PS1=%~dp0install_avm_windows.ps1"
if not exist "%PS1%" set "PS1=%~dp0avm\install_avm_windows.ps1"
if not exist "%PS1%" (
    set "PS1=%TEMP%\install_avm_windows.ps1"
    echo Fetching the installer from github.com/%REPO% ...
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://raw.githubusercontent.com/%REPO%/main/avm/install_avm_windows.ps1' -OutFile $env:TEMP\install_avm_windows.ps1"
)
if not exist "%PS1%" (
    echo Could not get install_avm_windows.ps1 -- check the internet connection.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
echo.
pause
