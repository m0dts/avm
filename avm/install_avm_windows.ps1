# AVM installer for Windows 10/11 (64-bit). Run via install_avm.bat.
#
# On a new PC, install_avm.bat on its own is enough: it fetches this script,
# which downloads AVM from GitHub into %USERPROFILE%\avm.
#
# No admin rights needed; everything else goes under %LOCALAPPDATA%\AVM:
#   0. AVM itself: from GitHub, unless this script sits in / beside an AVM
#      folder already (a copied release folder). -Update fetches the latest.
#   1. conda: uses radioconda (preferred) / Miniforge / Anaconda if present,
#      else installs Miniforge (just for this user)
#   2. a private Python environment (conda-forge): numpy, scipy, numba, PyQt5,
#      pyqtgraph, opencv, av, sounddevice, SoapySDR + Pluto / Lime / RTL modules
#   3. ffmpeg: uses one already on PATH or in C:\ffmpeg\bin, else downloads it
#   4. AVM.bat launcher in the AVM folder + Desktop and Start-menu shortcuts
# Safe to re-run: done steps are skipped. "install_avm.bat -Check" only reports.
param([switch]$Check, [switch]$Update, [switch]$Restart)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say($m)  { Write-Host "`n== $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "XX $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- where things are
$repo = if ($env:AVM_REPO) { $env:AVM_REPO } else { "m0dts/avm" }
$branch = if ($env:AVM_BRANCH) { $env:AVM_BRANCH } else { "main" }
# AVM beside this script (a copied release folder: .\ or .\avm), else
# %USERPROFILE%\avm (or AVM_INSTALL_DIR), fetched from GitHub if not there yet
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path "$here\touch_gui.py") { $avm = $here }
elseif (Test-Path "$here\avm\touch_gui.py") { $avm = "$here\avm" }
elseif ($env:AVM_INSTALL_DIR) { $avm = $env:AVM_INSTALL_DIR }
else { $avm = Join-Path $env:USERPROFILE "avm" }
if (-not [Environment]::Is64BitOperatingSystem) { Die "AVM needs 64-bit Windows (numba has no 32-bit builds)." }
$home_ = Join-Path $env:LOCALAPPDATA "AVM"
$envDir = Join-Path $home_ "env"
$py = Join-Path $envDir "python.exe"
Write-Host "AVM folder : $avm"
Write-Host "Installs to: $home_"
New-Item -ItemType Directory -Force $home_ | Out-Null

# ---------------------------------------------------------------- 0. AVM from GitHub
Say "0. AVM program files"
if ((Test-Path "$avm\touch_gui.py") -and -not $Update) {
    Write-Host "present ($avm); -Update fetches the latest from GitHub"
} elseif ($Check) {
    Write-Host "would download github.com/$repo ($branch) into $avm"
} else {
    $url = if ($env:AVM_URL) { $env:AVM_URL } else { "https://github.com/$repo/archive/refs/heads/$branch.zip" }
    $zip = Join-Path $env:TEMP "avm-download.zip"
    $tmp = Join-Path $env:TEMP "avm-download"
    Write-Host "downloading $url"
    try { Invoke-WebRequest $url -OutFile $zip } catch { Die "download failed: $url ($($_.Exception.Message))" }
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive $zip -DestinationPath $tmp
    $src = Get-ChildItem $tmp -Recurse -Filter touch_gui.py | Select-Object -First 1
    if (-not $src) { Die "the download has no touch_gui.py ($url)" }
    # copy over the top: settings live in the user profile, gui_logs\ is kept
    New-Item -ItemType Directory -Force $avm | Out-Null
    Copy-Item -Path (Join-Path $src.DirectoryName "*") -Destination $avm -Recurse -Force
    Remove-Item -Recurse -Force $tmp; Remove-Item $zip
    Write-Host "installed AVM into $avm"
    # Carry on with the installer just downloaded, not this (older) copy:
    # otherwise its newer steps (e.g. fetching an ffmpeg with Codec2) would
    # only run next time. No -Update for it, so this happens once.
    # (Even when that's this same file: PowerShell has the old text in memory.)
    $newer = Join-Path $avm "install_avm_windows.ps1"
    if (Test-Path $newer) {
        Write-Host "continuing with the updated installer..."
        if ($Restart) { & powershell -NoProfile -ExecutionPolicy Bypass -File $newer -Restart }
        else { & powershell -NoProfile -ExecutionPolicy Bypass -File $newer }
        exit $LASTEXITCODE
    }
}
if (-not (Test-Path "$avm\touch_gui.py") -and -not $Check) { Die "touch_gui.py not found in $avm." }

# ---------------------------------------------------------------- 1. conda
Say "1. conda"
# radioconda first (an SDR-focused conda: often already there on a radio PC),
# wherever its installer put it; then any other conda, then conda on PATH
$condaCandidates = @(
    "$env:ProgramData\radioconda", "$env:USERPROFILE\radioconda", "$env:LOCALAPPDATA\radioconda",
    "$env:LOCALAPPDATA\Programs\radioconda", "C:\radioconda",
    "$env:USERPROFILE\miniforge3", "$env:LOCALAPPDATA\miniforge3", "$env:ProgramData\miniforge3",
    "$home_\miniforge3", "$env:USERPROFILE\anaconda3", "$env:USERPROFILE\miniconda3",
    "$env:ProgramData\anaconda3", "$env:ProgramData\miniconda3")
$onPath = Get-Command conda.exe -ErrorAction SilentlyContinue
if ($onPath) { $condaCandidates += (Split-Path -Parent (Split-Path -Parent $onPath.Source)) }
$condaRoot = $condaCandidates | Where-Object { Test-Path "$_\Scripts\conda.exe" } | Select-Object -First 1
if ($condaRoot -and $condaRoot -like "*radioconda*") { Write-Host "radioconda found" }
if ($condaRoot) {
    Write-Host "using $condaRoot"
} elseif ($Check) {
    Write-Host "none found (Miniforge would be installed)"
} else {
    $mf = Join-Path $home_ "miniforge3"
    $exe = Join-Path $env:TEMP "Miniforge3-Windows-x86_64.exe"
    Write-Host "downloading Miniforge..."
    Invoke-WebRequest "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe" -OutFile $exe
    Write-Host "installing Miniforge to $mf (a few minutes)..."
    Start-Process -Wait -FilePath $exe -ArgumentList "/InstallationType=JustMe", "/RegisterPython=0", "/AddToPath=0", "/S", "/D=$mf"
    Remove-Item $exe -ErrorAction SilentlyContinue
    if (-not (Test-Path "$mf\Scripts\conda.exe")) { Die "Miniforge install failed." }
    $condaRoot = $mf
}
# mamba is much faster at solving where it exists
$solver = @("$condaRoot\Scripts\mamba.exe", "$condaRoot\Library\bin\mamba.exe", "$condaRoot\condabin\mamba.bat") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $solver) { $solver = "$condaRoot\Scripts\conda.exe" }

# ---------------------------------------------------------------- 2. environment
Say "2. Python environment ($envDir)"
# headless OpenCV: the default build drags in Qt6 next to AVM's PyQt5
$pkgs = @("python=3.12", "numpy", "scipy", "numba", "pyqt=5", "pyqtgraph", "py-opencv=*=headless*", "av",
          "python-sounddevice", "soapysdr", "soapysdr-module-plutosdr", "soapysdr-module-lms7",
          "soapysdr-module-rtlsdr")
$imports = "numpy scipy numba cv2 av PyQt5 pyqtgraph sounddevice SoapySDR"
function Test-Env {
    if (-not (Test-Path $py)) { return $false }
    & $py -c "import $($imports -replace ' ', ', ')" 2>$null
    return ($LASTEXITCODE -eq 0)
}
if (Test-Env) {
    Write-Host "present, all modules import"
} elseif ($Check) {
    Write-Host "missing or incomplete"
} else {
    # package cache in our own folder: a radioconda in ProgramData isn't writable without admin
    $env:CONDA_PKGS_DIRS = Join-Path $home_ "pkgs"
    $verb = if (Test-Path $py) { "install" } else { "create" }
    Write-Host "conda $verb (downloads ~0.5-1 GB the first time; several minutes)..."
    & $solver $verb -y -p $envDir -c conda-forge --override-channels @pkgs
    if ($LASTEXITCODE -ne 0) { Die "conda $verb failed (see above)." }
    if (-not (Test-Env)) {
        & $py -c "import $($imports -replace ' ', ', ')"
        Die "environment created but some modules don't import (see above)."
    }
    Write-Host "done"
}

# ---------------------------------------------------------------- 3. ffmpeg
Say "3. ffmpeg"
# AVM needs ffmpeg with Opus and Codec2. Use the first ffmpeg found that has
# both; otherwise download gyan.dev's "full" build (it has both; the common
# BtbN builds lack Codec2) into %LOCALAPPDATA%\AVM\ffmpeg. It's a .7z, which
# Windows' own tar.exe (bsdtar) unpacks -- no 7-Zip needed.
function Get-FfEncoders($exe) {
    try { return ((& $exe -hide_banner -encoders 2>$null) -join "`n") } catch { return "" }
}
$candidates = @("$avm\ffmpeg\bin", "$home_\ffmpeg\bin", "C:\ffmpeg\bin")
$onPath = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($onPath) { $candidates += (Split-Path -Parent $onPath.Source) }
$ffDir = $null
$partial = $null   # an ffmpeg without Codec2, if that's all there is
foreach ($d in $candidates) {
    if (-not (Test-Path "$d\ffmpeg.exe")) { continue }
    $enc = Get-FfEncoders "$d\ffmpeg.exe"
    if ($enc -match "libcodec2" -and $enc -match "libopus") { $ffDir = $d; break }
    if (-not $partial) { $partial = $d }
}
if ($ffDir) {
    Write-Host "using $ffDir\ffmpeg.exe (Opus and Codec2)"
} elseif ($Check) {
    if ($partial) { Write-Host "$partial\ffmpeg.exe has no Codec2: the full build would be downloaded" }
    else { Write-Host "not found: the full build would be downloaded" }
} else {
    if ($partial) { Write-Host "$partial\ffmpeg.exe has no Codec2 -- getting the full build" }
    $arc = Join-Path $env:TEMP "ffmpeg-avm-full.7z"
    $tmp = Join-Path $home_ "ffmpeg_tmp"
    try {
        Write-Host "downloading ffmpeg full build (~170 MB, gyan.dev)..."
        Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z" -OutFile $arc
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        New-Item -ItemType Directory $tmp | Out-Null
        & "$env:SystemRoot\System32\tar.exe" -xf $arc -C $tmp
        if ($LASTEXITCODE -ne 0) { throw "tar couldn't unpack the .7z" }
        $exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
        if (-not $exe) { throw "no ffmpeg.exe in the download" }
        if (Test-Path "$home_\ffmpeg") { Remove-Item -Recurse -Force "$home_\ffmpeg" }
        Move-Item (Split-Path -Parent $exe.DirectoryName) "$home_\ffmpeg"
        $ffDir = "$home_\ffmpeg\bin"
        Write-Host "installed to $ffDir"
    } catch {
        Warn "couldn't get the full ffmpeg build ($($_.Exception.Message))"
        if ($partial) { $ffDir = $partial; Warn "using $partial\ffmpeg.exe: Opus only, no Codec2" }
        else { Die "no ffmpeg: AVM needs one. Put a 'full' build in C:\ffmpeg and re-run." }
    } finally {
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
        if (Test-Path $arc) { Remove-Item $arc -ErrorAction SilentlyContinue }
    }
}
if ($ffDir) {
    $enc = Get-FfEncoders "$ffDir\ffmpeg.exe"
    if ($enc -notmatch "libopus") { Warn "this ffmpeg has no Opus encoder: Opus audio won't work" }
    if ($enc -notmatch "libcodec2") { Warn "this ffmpeg has no Codec2 encoder: AVM offers Opus only" }
}

# ---------------------------------------------------------------- 4. launcher
Say "4. Launcher"
$bat = Join-Path $avm "AVM.bat"
$launcher = @"
@echo off
rem AVM launcher (written by install_avm.bat)
set "PATH=$ffDir;$envDir;$envDir\Library\bin;$envDir\Scripts;%PATH%"
cd /d "$avm"
if not exist gui_logs mkdir gui_logs
set "HF_RX_GUI_LOG=$avm\gui_logs\rx_session.log"
start "AVM" "$envDir\pythonw.exe" touch_gui.py %*
"@
if ($Check) {
    if (Test-Path $bat) { Write-Host "present: $bat" } else { Write-Host "missing" }
} else {
    Set-Content -Path $bat -Value $launcher -Encoding ascii
    $ws = New-Object -ComObject WScript.Shell
    $icon = "$env:SystemRoot\System32\shell32.dll,18"
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))) {
        $lnk = $ws.CreateShortcut((Join-Path $dir "AVM.lnk"))
        $lnk.TargetPath = $bat
        $lnk.WorkingDirectory = $avm
        $lnk.IconLocation = $icon
        $lnk.WindowStyle = 7   # minimised: the .bat's console only flashes
        $lnk.Description = "AVM -- Audio Video Modem"
        $lnk.Save()
    }
    Write-Host "AVM.bat + 'AVM' shortcuts on the Desktop and in the Start menu"
}

# ---------------------------------------------------------------- summary
Say "Radios SoapySDR can see now"
if (Test-Path $py) {
    $env:PATH = "$envDir;$envDir\Library\bin;$env:PATH"
    # errors only: the Pluto driver warns about search methods that don't
    # apply on Windows ("Unable to scan local: -19") -- harmless noise
    & $py -c "import SoapySDR; SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_ERROR); r = SoapySDR.Device.enumerate(); print('\n'.join('  ' + d['driver'] + ': ' + d['label'] for d in r) or '  (none found)')" 2>$null
}
Say "Done"
Write-Host "Start AVM from the 'AVM' shortcut (or AVM.bat in $avm)."
if ($Restart -and (Test-Path $bat)) {
    # run from AVM's Update button: start AVM again, now fully up to date
    Write-Host "restarting AVM..."
    Start-Process -FilePath $bat -WorkingDirectory $avm -WindowStyle Minimized
    Start-Sleep -Seconds 3
    exit 0
}
Write-Host "The first TX/RX start compiles the modem and codec (a minute or two); later starts are quick."
Write-Host "RTL-SDR on Windows needs the WinUSB driver (Zadig, zadig.akeo.ie) before it shows up."
