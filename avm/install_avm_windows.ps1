# AVM installer for Windows 10/11 (64-bit). Run via install_avm.bat.
#
# On a new PC, install_avm.bat on its own is enough: it fetches this script,
# which downloads AVM -- one ready-built zip, nothing else to install (no
# Python, no conda, no admin rights) -- into %USERPROFILE%\avm\app:
#   0. AVM itself: the release zip (AVM-windows.zip) from GitHub's latest
#      release, or one beside this script, or AVM_ZIP=<file>. -Update fetches
#      it again; -Restart (an older AVM's Update button) too, then starts AVM.
#   1. AVM.bat + Desktop and Start-menu shortcuts (older ones switched over)
# Safe to re-run. "install_avm.bat -Check" only reports. It first shows what
# it would do and asks (not with -Yes or -Restart). Everything it prints also
# goes to a log, %LOCALAPPDATA%\AVM\install.log.
param([switch]$Check, [switch]$Update, [switch]$Restart, [switch]$Yes)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say($m)  { Write-Host "`n== $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!! $m" -ForegroundColor Yellow }
function Die($m)  {
    Write-Host "XX $m" -ForegroundColor Red
    if ($script:log) { Write-Host "Install log: $script:log"; try { Stop-Transcript | Out-Null } catch { } }
    exit 1
}

# ---------------------------------------------------------------- where things are
$repo = if ($env:AVM_REPO) { $env:AVM_REPO } else { "m0dts/avm" }
# the release zip: AVM_ZIP (a file), else one beside this script, else GitHub's
# latest release (AVM_RELEASE=<tag> for a particular one, e.g. a test build)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$zipUrl = if ($env:AVM_RELEASE) { "https://github.com/$repo/releases/download/$($env:AVM_RELEASE)/AVM-windows.zip" }
          else { "https://github.com/$repo/releases/latest/download/AVM-windows.zip" }
$localZip = if ($env:AVM_ZIP) { $env:AVM_ZIP } elseif (Test-Path "$here\AVM-windows.zip") { "$here\AVM-windows.zip" } else { $null }
$avm = if ($env:AVM_INSTALL_DIR) { $env:AVM_INSTALL_DIR } else { Join-Path $env:USERPROFILE "avm" }
$avm = [IO.Path]::GetFullPath($avm).TrimEnd("\")
$app = Join-Path $avm "app"
$exe = Join-Path $app "avm_gui.exe"
if (-not [Environment]::Is64BitOperatingSystem) { Die "AVM needs 64-bit Windows." }
$home_ = Join-Path $env:LOCALAPPDATA "AVM"
New-Item -ItemType Directory -Force $home_ | Out-Null
$script:log = $null
if (-not $Check) {
    $script:log = Join-Path $home_ "install.log"
    $append = [bool]$env:AVM_INSTALL_LOG
    $env:AVM_INSTALL_LOG = $script:log
    try { Start-Transcript -Path $script:log -Append:$append | Out-Null } catch { $script:log = $null }
}
$have = if (Test-Path "$app\VERSION") { (Get-Content "$app\VERSION" -TotalCount 1).Trim() } else { $null }
$fetch = $Update -or $Restart -or -not (Test-Path $exe)

# ---------------------------------------------------------------- survey
if ($Check -or (-not $Yes -and -not $Restart)) {
    Write-Host "AVM installer for Windows"
    Write-Host "  AVM folder: $app"
    if ($have) { Write-Host "  installed:  AVM $have" } else { Write-Host "  installed:  no" }
    if ($fetch) {
        $from = if ($localZip) { $localZip } else { $zipUrl }
        Write-Host "  would get:  AVM-windows.zip (~100 MB) from $from"
    }
    Write-Host "  would set:  AVM.bat, and 'AVM' shortcuts on the Desktop and in the Start menu"
    Write-Host "  No admin rights are needed; nothing is installed outside your user folders."
    if ($Check) { exit 0 }
    $ans = Read-Host "`nInstall / change this? [y/N]"
    if ($ans -notmatch '^(y|yes)$') {
        Write-Host "Nothing changed."
        try { Stop-Transcript | Out-Null } catch { }
        exit 0
    }
}

# ---------------------------------------------------------------- 0. AVM
Say "0. AVM"
if (-not $fetch) {
    Write-Host "present: AVM $have ($app); -Update fetches the latest"
} else {
    if (Get-Process avm_gui -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
        Write-Host "closing the running AVM first..."
        Get-Process avm_gui | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    $zip = if ($localZip) { $localZip } else { Join-Path $env:TEMP "AVM-windows.zip" }
    if (-not $localZip) {
        Write-Host "downloading $zipUrl (~100 MB)..."
        try { Invoke-WebRequest $zipUrl -OutFile $zip } catch { Die "download failed: $zipUrl ($($_.Exception.Message))" }
    }
    # unpacked beside, then swapped in: a failed download or unpack leaves the old AVM
    $new = Join-Path $avm "app.new"
    if (Test-Path $new) { Remove-Item -Recurse -Force $new }
    try { Expand-Archive $zip -DestinationPath $new } catch { Die "couldn't unpack $zip ($($_.Exception.Message))" }
    if (-not (Test-Path "$new\avm_gui.exe")) { Die "the zip has no avm_gui.exe ($zip)" }
    if (Test-Path "$app\gui_logs") { Move-Item "$app\gui_logs" "$new\gui_logs" }    # the logs kept
    if (Test-Path $app) { Remove-Item -Recurse -Force $app }
    Move-Item $new $app
    if (-not $localZip) { Remove-Item $zip -ErrorAction SilentlyContinue }
    $have = (Get-Content "$app\VERSION" -TotalCount 1).Trim()
    Write-Host "installed AVM $have into $app"
}

# ---------------------------------------------------------------- 1. launcher
Say "1. Launcher"
# AVM.bat: where the shortcuts of older AVMs (the Python one) point -- now the C++ program
$bat = Join-Path $avm "AVM.bat"
Set-Content -Path $bat -Encoding ascii -Value @"
@echo off
rem AVM launcher (written by install_avm.bat)
start "AVM" /d "$app" "$exe" %*
"@
$ws = New-Object -ComObject WScript.Shell
# (AVM_NO_SHORTCUTS=1: none -- a test install that mustn't replace the real ones)
$shortcutDirs = if ($env:AVM_NO_SHORTCUTS -eq "1") { @() } else { @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs")) }
foreach ($dir in $shortcutDirs) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "AVM.lnk"))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $app
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "AVM -- Audio Video Modem"
    $lnk.Save()
}
Write-Host "'AVM' shortcuts on the Desktop and in the Start menu -> $exe"

Say "Done"
Write-Host "Start AVM from the 'AVM' shortcut."
Write-Host "Radios: PlutoSDR, LimeSDR, Airspy and Airspy HF+ drivers are included (RTL-SDR isn't)."
if ($script:log) { Write-Host "Install log (everything above): $script:log" }
if ($Restart) {
    # run from AVM's Update button: start AVM again, now up to date
    Write-Host "starting AVM..."
    Start-Process -FilePath $exe -WorkingDirectory $app
    Start-Sleep -Seconds 2
}
if ($script:log) { try { Stop-Transcript | Out-Null } catch { } }
exit 0
