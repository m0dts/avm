# package_windows.ps1 -- AVM's Windows release: one zip that runs on any
# 64-bit Windows 10/11 PC with nothing else installed (no Python, no conda).
#
#   powershell -ExecutionPolicy Bypass -File gui\package_windows.ps1
#
# Builds the GUI and the library for any x86-64 CPU (AVM_NATIVE off: the
# usual build is tuned to this PC's) in build-release\, then gathers in
# dist\AVM\:
#   avm_gui.exe + the Qt, FFmpeg and Codec2 DLLs (windeployqt / the build),
#   c++\avm.dll (the modem library: where the GUI looks for it),
#   SoapySDR.dll + the radio modules (SoapySDR\modules0.8: Pluto, Lime,
#     Airspy, Airspy HF+; not RTL-SDR, see below) and their DLLs, from the conda-forge
#     environment the Python installer made (%LOCALAPPDATA%\AVM\env, or -SoapyEnv),
#   VERSION;
# and zips it as dist\AVM-windows.zip -- the asset install_avm_windows.ps1
# downloads from the GitHub release (releases/latest/download/AVM-windows.zip).
param([string]$SoapyEnv = (Join-Path $env:LOCALAPPDATA "AVM\env"))
$ErrorActionPreference = "Stop"
$gui = $PSScriptRoot
$avm = Split-Path -Parent $gui
$dist = Join-Path $avm "dist"
$out = Join-Path $dist "AVM"

# ---- build (portable)
$env:AVM_BUILD_DIR = "build-release"
$env:AVM_CMAKE_ARGS = "-DAVM_NATIVE=OFF"
& cmd /c (Join-Path $gui "build.bat")
if ($LASTEXITCODE -ne 0) { throw "the build failed" }
$built = Join-Path $gui "build-release"

# ---- gather
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out, "$out\c++", "$out\SoapySDR\modules0.8" | Out-Null
# the program and what windeployqt / the build put beside it (not the build's own files)
Get-ChildItem $built | Where-Object {
    $_.Name -notmatch '^(CMakeFiles|CMakeCache\.txt|cmake_install\.cmake|build\.ninja|\.ninja_.*|.*_autogen|third_party|CPack.*|avm_gui\.exe\.manifest)$'
} | Copy-Item -Destination $out -Recurse -Force
Copy-Item (Join-Path $avm "c++\avm.dll") "$out\c++\"
Copy-Item (Join-Path $avm "VERSION") $out
# SoapySDR, its modules, and every DLL they need (objdump's import lists,
# followed to the end; Windows' own and what Qt's deployment already has are skipped)
$bin = Join-Path $SoapyEnv "Library\bin"
$mods = Join-Path $SoapyEnv "Library\lib\SoapySDR\modules0.8"
if (-not (Test-Path "$bin\SoapySDR.dll")) { throw "no SoapySDR in $SoapyEnv (run the Python installer once, or -SoapyEnv)" }
# (no RTL-SDR: left out of the bundle for now -- its library, librtlsdr, is
# GPL-2.0-or-later, so compatible with AVM's GPL-3.0 if it's wanted later)
$moduleNames = "PlutoSDRSupport", "LMS7Support", "airspySupport", "airspyhfSupport"
foreach ($m in $moduleNames) { Copy-Item "$mods\$m.dll" "$out\SoapySDR\modules0.8\" }
$objdump = Join-Path $env:USERPROFILE "Qt\Tools\mingw1310_64\bin\objdump.exe"
$queue = New-Object System.Collections.Queue
$queue.Enqueue("$bin\SoapySDR.dll")
foreach ($m in $moduleNames) { $queue.Enqueue("$mods\$m.dll") }
$seen = @{}
while ($queue.Count) {
    $f = $queue.Dequeue()
    foreach ($line in (& $objdump -p $f | Select-String "DLL Name: (.+)$")) {
        $d = $line.Matches[0].Groups[1].Value.Trim()
        if ($seen.ContainsKey($d.ToLower())) { continue }
        $seen[$d.ToLower()] = $true
        if (Test-Path (Join-Path $out $d)) { continue }          # (already there: Qt's deployment)
        $src = Join-Path $bin $d
        if (-not (Test-Path $src)) { continue }                  # Windows' own
        Copy-Item $src $out
        $queue.Enqueue($src)
    }
}
# the Microsoft C++ runtime the conda-forge DLLs were built with (a PC without
# the Visual C++ redistributable has none)
foreach ($d in "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll") {
    if ((Test-Path "$bin\$d") -and -not (Test-Path "$out\$d")) { Copy-Item "$bin\$d" $out }
}

# ---- licences: AVM's own (GPL-3.0), and every bundled component's, with
# THIRD-PARTY.txt saying what each is, its licence and where its source is
$lic = Join-Path $out "LICENSES"
New-Item -ItemType Directory -Force $lic | Out-Null
Copy-Item (Join-Path $avm "LICENSE") (Join-Path $out "LICENSE.txt")
$third = @()
# the conda-forge DLLs: each from its package, with the package's own licence files
$pkgs = Join-Path (Split-Path -Parent $SoapyEnv) "pkgs"
$srcUrl = @{
    "soapysdr" = "https://github.com/pothosware/SoapySDR"; "soapysdr-module-plutosdr" = "https://github.com/pothosware/SoapyPlutoSDR"
    "soapysdr-module-lms7" = "https://github.com/myriadrf/LimeSuite"; "liblimesuite" = "https://github.com/myriadrf/LimeSuite"
    "soapysdr-module-airspy" = "https://github.com/pothosware/SoapyAirspy"; "soapysdr-module-airspyhf" = "https://github.com/pothosware/SoapyAirspyHF"
    "libairspy" = "https://github.com/airspy/airspyone_host"; "libairspyhf" = "https://github.com/airspy/airspyhf"
    "libiio-c" = "https://github.com/analogdevicesinc/libiio"; "libad9361-iio-c" = "https://github.com/analogdevicesinc/libad9361-iio"
    "libusb" = "https://github.com/libusb/libusb"; "libxml2" = "https://gitlab.gnome.org/GNOME/libxml2"
    "libxml2-16" = "https://gitlab.gnome.org/GNOME/libxml2"; "icu" = "https://github.com/unicode-org/icu"
    "vc14_runtime" = "https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files"
    "libiconv" = "https://www.gnu.org/software/libiconv/"; "zstd" = "https://github.com/facebook/zstd"
    "libwinpthread" = "https://www.mingw-w64.org/"; "ucrt" = "https://learn.microsoft.com/cpp/windows/universal-crt-deployment"
    "vc" = "https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files" }
$fromConda = @{}
$bundled = @(Get-ChildItem $out -Filter *.dll) + @(Get-ChildItem "$out\SoapySDR\modules0.8" -Filter *.dll)
foreach ($dll in $bundled) {
    $rel = if ($dll.DirectoryName -like "*modules0.8") { "Library\lib\SoapySDR\modules0.8\$($dll.Name)" } else { "Library\bin\$($dll.Name)" }
    $pkg = Get-ChildItem $pkgs -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName $rel) } |
        Select-Object -First 1
    if (-not $pkg -or $fromConda.ContainsKey($pkg.Name)) { continue }
    $fromConda[$pkg.Name] = $true
    $m = [regex]::Match($pkg.Name, '^(.+?)-(\d[^-]*)-[^-]+$')
    $name = $m.Groups[1].Value; $ver = $m.Groups[2].Value
    $licDir = Join-Path $pkg.FullName "info\licenses"
    if (Test-Path $licDir) { Copy-Item $licDir (Join-Path $lic $name) -Recurse -Force }
    $files = (Get-ChildItem (Join-Path $lic $name) -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) -join ", "
    $third += [pscustomobject]@{ Name = $name; Version = $ver; Licence = "see LICENSES\$name ($files)"; Source = $srcUrl[$name] }
}
# the rest: Qt, FFmpeg, Codec2, Opus, miniaudio, the MinGW runtime
$lt = Join-Path $gui "licenses"
New-Item -ItemType Directory -Force "$lic\qt", "$lic\ffmpeg", "$lic\codec2", "$lic\opus", "$lic\mingw-runtime" | Out-Null
Copy-Item "$lt\lgpl-3.0.txt", "$lt\gpl-3.0.txt" "$lic\qt\"
Copy-Item (Join-Path $gui "third_party\ffmpeg\LICENSE.txt") "$lic\ffmpeg\"
Copy-Item "$lt\lgpl-2.1.txt" "$lic\ffmpeg\"
Copy-Item (Join-Path $gui "third_party\codec2\COPYING") "$lic\codec2\"
Copy-Item (Join-Path $gui "third_party\opus\COPYING") "$lic\opus\"
$mingwLic = Join-Path $env:USERPROFILE "Qt\Tools\mingw1310_64\licenses"
foreach ($d in "gcc", "winpthreads", "mingw-w64") {
    if (Test-Path "$mingwLic\$d") { Copy-Item "$mingwLic\$d" "$lic\mingw-runtime\$d" -Recurse -Force }
}
$qtVer = (Get-Item (Join-Path $out "Qt6Core.dll")).VersionInfo.ProductVersion
$ffVer = (Get-Content (Join-Path $gui "third_party\ffmpeg\VERSION") -ErrorAction SilentlyContinue) -join ""
$third += [pscustomobject]@{ Name = "Qt"; Version = $qtVer; Licence = "LGPL-3.0 (LICENSES\qt): the Qt6*.dll and plugins, unmodified, replaceable"; Source = "https://download.qt.io/official_releases/qt/" }
$third += [pscustomobject]@{ Name = "FFmpeg (LGPL build, BtbN)"; Version = $ffVer; Licence = "LGPL-2.1+ (LICENSES\ffmpeg)"; Source = "https://github.com/BtbN/FFmpeg-Builds ; https://ffmpeg.org/download.html" }
$third += [pscustomobject]@{ Name = "Codec2"; Version = ""; Licence = "LGPL-2.1 (LICENSES\codec2)"; Source = "https://github.com/drowe67/codec2" }
$third += [pscustomobject]@{ Name = "Opus (built into avm_gui.exe)"; Version = ""; Licence = "BSD (LICENSES\opus)"; Source = "https://opus-codec.org/" }
$third += [pscustomobject]@{ Name = "miniaudio (built into avm_gui.exe)"; Version = ""; Licence = "public domain / MIT-0"; Source = "https://miniaud.io/" }
$third += [pscustomobject]@{ Name = "MinGW-w64 GCC runtime (libstdc++, libgcc, winpthreads)"; Version = ""; Licence = "GPL-3.0 with the GCC Runtime Library Exception; MIT / BSD (LICENSES\mingw-runtime)"; Source = "https://www.mingw-w64.org/" }
$txt = @("AVM -- Audio Video Modem $(Get-Content (Join-Path $avm 'VERSION'))",
         "Copyright the AVM authors. Licensed under the GNU General Public License v3.0 (LICENSE.txt).",
         "Source: https://github.com/m0dts/avm",
         "",
         "This package also contains the following components, each under its own licence",
         "(texts in LICENSES\). Their source code is available from the addresses below.",
         "Not included: RTL-SDR support (SoapyRTLSDR / librtlsdr); add it yourself if needed.",
         "")
foreach ($t in $third | Sort-Object Name) {
    $txt += "$($t.Name) $($t.Version)".Trim()
    $txt += "    Licence: $($t.Licence)"
    if ($t.Source) { $txt += "    Source:  $($t.Source)" }
    $txt += ""
}
Set-Content -Path (Join-Path $out "THIRD-PARTY.txt") -Value $txt -Encoding utf8

# ---- zip
$zip = Join-Path $dist "AVM-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "AVM $(Get-Content (Join-Path $avm 'VERSION')) for Windows: $zip ($mb MB)"
