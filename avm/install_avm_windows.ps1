# AVM installer for Windows 10/11 (64-bit). Run via install_avm.bat.
#
# On a new PC, install_avm.bat on its own is enough: it fetches this script,
# which downloads AVM's installer (AVM-setup.exe) -- nothing else to install
# (no Python, no conda, no admin rights) -- and runs it, unattended, into
# %USERPROFILE%\avm\app:
#   the installer from GitHub's latest release, or one beside this script, or
#   AVM_SETUP=<file>; AVM_RELEASE=<tag> for a particular release (a test build).
#   -Update fetches it again; -Restart (AVM's Update button, the old Python
#   AVM's too) also, then starts AVM.
# The installer writes AVM.bat (where older AVMs' shortcuts point) and the
# 'AVM' shortcuts (AVM_NO_SHORTCUTS=1: none). Safe to re-run. "install_avm.bat
# -Check" only reports. It first shows what it would do and asks (not with -Yes
# or -Restart). Everything it prints also goes to %LOCALAPPDATA%\AVM\install.log.
param([switch]$Check, [switch]$Update, [switch]$Restart, [switch]$Yes)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say($m)  { Write-Host "`n== $m" -ForegroundColor Cyan }
function Die($m)  {
    Write-Host "XX $m" -ForegroundColor Red
    if ($script:log) { Write-Host "Install log: $script:log"; try { Stop-Transcript | Out-Null } catch { } }
    exit 1
}

# ---------------------------------------------------------------- where things are
$repo = if ($env:AVM_REPO) { $env:AVM_REPO } else { "m0dts/avm" }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$setupUrl = if ($env:AVM_RELEASE) { "https://github.com/$repo/releases/download/$($env:AVM_RELEASE)/AVM-setup.exe" }
            else { "https://github.com/$repo/releases/latest/download/AVM-setup.exe" }
$localSetup = if ($env:AVM_SETUP) { $env:AVM_SETUP } elseif (Test-Path "$here\AVM-setup.exe") { "$here\AVM-setup.exe" } else { $null }
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
        $from = if ($localSetup) { $localSetup } else { $setupUrl }
        Write-Host "  would get:  AVM-setup.exe (~75 MB) from $from, and run it"
    } else {
        Write-Host "  nothing to do (-Update fetches the latest)"
    }
    Write-Host "  No admin rights are needed; nothing is installed outside your user folders."
    if ($Check -or -not $fetch) { if ($script:log) { try { Stop-Transcript | Out-Null } catch { } }; exit 0 }
    $ans = Read-Host "`nInstall / change this? [y/N]"
    if ($ans -notmatch '^(y|yes)$') {
        Write-Host "Nothing changed."
        try { Stop-Transcript | Out-Null } catch { }
        exit 0
    }
}

# ---------------------------------------------------------------- AVM
Say "AVM"
if (-not $fetch) {
    Write-Host "present: AVM $have ($app); -Update fetches the latest"
} else {
    if (Get-Process avm_gui -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
        Write-Host "closing the running AVM first..."
        Get-Process avm_gui | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    $setup = if ($localSetup) { $localSetup } else { Join-Path $env:TEMP "AVM-setup.exe" }
    if (-not $localSetup) {
        Write-Host "downloading $setupUrl (~75 MB)..."
        try { Invoke-WebRequest $setupUrl -OutFile $setup } catch { Die "download failed: $setupUrl ($($_.Exception.Message))" }
    }
    Write-Host "installing..."
    $setupArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=`"$app`"")
    if ($env:AVM_NO_SHORTCUTS -eq "1") { $setupArgs += @("/NOICONS", "/TASKS=`"`"") }
    $p = Start-Process -FilePath $setup -ArgumentList $setupArgs -Wait -PassThru
    if (-not $localSetup) { Remove-Item $setup -ErrorAction SilentlyContinue }
    if ($p.ExitCode -ne 0) { Die "the installer failed (exit code $($p.ExitCode))" }
    if (-not (Test-Path $exe)) { Die "the installer left no $exe" }
    $have = (Get-Content "$app\VERSION" -TotalCount 1).Trim()
    Write-Host "installed AVM $have into $app"
}

Say "Done"
Write-Host "Start AVM from the 'AVM' shortcut."
Write-Host "Radios: PlutoSDR, LimeSDR, HackRF, RTL-SDR, Airspy and Airspy HF+ drivers are included."
Write-Host "A HackRF or RTL-SDR also needs the WinUSB driver once (Zadig, zadig.akeo.ie); a Pluto, Analog Devices'"
Write-Host "PlutoSDR-M2k USB drivers (or use it by its network address, 192.168.2.1)."
if ($script:log) { Write-Host "Install log (everything above): $script:log" }
if ($Restart) {
    # run from AVM's Update button: start AVM again, now up to date
    Write-Host "starting AVM..."
    Start-Process -FilePath $exe -WorkingDirectory $app
    Start-Sleep -Seconds 2
}
if ($script:log) { try { Stop-Transcript | Out-Null } catch { } }
exit 0
