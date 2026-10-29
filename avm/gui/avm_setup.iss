; AVM installer for Windows (Inno Setup 6), built by package_windows.ps1:
;   ISCC /DAppVersion=1.1.0 /DSrcDir=..\dist\AVM /DOutDir=..\dist avm_setup.iss
; Per-user, no admin rights, into %USERPROFILE%\avm\app -- the same place as
; install_avm_windows.ps1 (and so the Update button) uses, so either can update
; the other's install. The logs (app\gui_logs) and settings are kept.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\dist\AVM"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif

[Setup]
AppId={{6F0C2D1E-4A7B-4B8E-9C61-A5D7E3B2F190}
AppName=AVM
AppVersion={#AppVersion}
AppVerName=AVM {#AppVersion}
AppPublisher=M0DTS
AppPublisherURL=https://github.com/m0dts/avm
AppComments=Audio Video Modem
DefaultDirName={%USERPROFILE}\avm\app
DisableDirPage=yes
DefaultGroupName=AVM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=AVM-setup
UninstallDisplayIcon={app}\avm_gui.exe
LicenseFile={#SrcDir}\LICENSE.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Tasks]
Name: desktopicon; Description: "Desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; the previous version's program files (not gui_logs): nothing stale left behind
Type: files; Name: "{app}\*.dll"
Type: files; Name: "{app}\*.exe"
Type: filesandordirs; Name: "{app}\c++"
Type: filesandordirs; Name: "{app}\SoapySDR"
Type: filesandordirs; Name: "{app}\LICENSES"
Type: filesandordirs; Name: "{app}\platforms"
Type: filesandordirs; Name: "{app}\styles"
Type: filesandordirs; Name: "{app}\imageformats"
Type: filesandordirs; Name: "{app}\iconengines"
Type: filesandordirs; Name: "{app}\generic"
Type: filesandordirs; Name: "{app}\networkinformation"
Type: filesandordirs; Name: "{app}\tls"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\AVM"; Filename: "{app}\avm_gui.exe"; WorkingDir: "{app}"; Comment: "AVM -- Audio Video Modem"
Name: "{userdesktop}\AVM"; Filename: "{app}\avm_gui.exe"; WorkingDir: "{app}"; Comment: "AVM -- Audio Video Modem"; Tasks: desktopicon

[Run]
Filename: "{app}\avm_gui.exe"; WorkingDir: "{app}"; Description: "Start AVM"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\..\AVM.bat"

[Code]
// AVM.bat: where the shortcuts of older AVMs (the Python one) point
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\..\AVM.bat'),
      '@echo off' + #13#10 + 'rem AVM launcher (written by the AVM installer)' + #13#10 +
      'start "AVM" /d "' + ExpandConstant('{app}') + '" "' + ExpandConstant('{app}\avm_gui.exe') + '" %*' + #13#10, False);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Radios: PlutoSDR, LimeSDR, HackRF, RTL-SDR, Airspy and Airspy HF+ drivers are included. ' +
      'A HackRF or RTL-SDR also needs the WinUSB driver once (Zadig, zadig.akeo.ie); a Pluto, Analog Devices'' ' +
      'PlutoSDR-M2k USB drivers (or use it by its network address, 192.168.2.1).';
end;
