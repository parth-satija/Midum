; ============================================================================
;  Midum web installer (stub)
;
;  A tiny setup.exe: it contains no app files. When run it downloads the
;  latest release zip from GitHub, verifies its SHA-256, extracts it, and
;  creates shortcuts + an uninstaller entry. Because it always fetches
;  "releases/latest", this installer never needs rebuilding for a new version.
;
;  Requires Inno Setup 6.3+ (download pages need 6.1+, "x64compatible" 6.3+).
;  Build:  ISCC.exe /DAppVersion=1.2.3 midum-web.iss
; ============================================================================

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName       "Midum"
#define AppPublisher  "Parth Satija"
#define AppURL        "https://github.com/parth-satija/Midum"
#define AppExeName    "Midum.exe"
#define PayloadBase   "https://github.com/parth-satija/Midum/releases/latest/download/"
#define PayloadZip    "Midum-windows-x64.zip"

[Setup]
AppId={{D3D36B31-C203-4C51-9D81-6E93EBA362B3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
; Per-user install by default: no admin/UAC prompt, and Midum can update
; itself later. The user can still choose "all users" from the dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=Midum-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
#ifexist "..\..\gui\assets\icon.ico"
SetupIconFile=..\..\gui\assets\icon.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "tesseract"; Description: "Install Tesseract OCR (recommended: lets Midum read and click on-screen text)"; GroupDescription: "Optional components:"; Check: NeedTesseract

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{localappdata}\Microsoft\WindowsApps\winget.exe"; Parameters: "install --id UB-Mannheim.TesseractOCR -e --silent --accept-source-agreements --accept-package-agreements"; StatusMsg: "Installing Tesseract OCR (this can take a minute)..."; Flags: runhidden waituntilterminated; Tasks: tesseract; Check: HasWinget
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Files extracted by the code section below aren't in [Files], so the
; uninstaller needs to be told to remove the install folder. User data in
; %LOCALAPPDATA%\Midum is handled separately (the user is asked).
[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  PayloadUrl  = '{#PayloadBase}{#PayloadZip}';
  ChecksumUrl = '{#PayloadBase}{#PayloadZip}.sha256';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D3D36B31-C203-4C51-9D81-6E93EBA362B3}_is1';

var
  DownloadPage: TDownloadWizardPage;
  PayloadPath: String;
  CurrentUrl: String;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

{ ---- optional Tesseract ---------------------------------------------------- }

function TesseractInstalled: Boolean;
begin
  Result :=
    FileExists('C:\Program Files\Tesseract-OCR\tesseract.exe') or
    (FileSearch('tesseract.exe', GetEnv('PATH')) <> '');
end;

function NeedTesseract: Boolean;
begin
  Result := not TesseractInstalled;
end;

function HasWinget: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Microsoft\WindowsApps\winget.exe'));
end;

{ ---- running-app handling -------------------------------------------------- }

function MidumIsRunning: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq {#AppExeName}" | find /I "{#AppExeName}"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if MidumIsRunning then
  begin
    { In /SILENT mode the default answer (Yes) is used, so silent upgrades work. }
    if SuppressibleMsgBox('Midum is currently running and must be closed to continue. Close it now?',
         mbConfirmation, MB_YESNO, IDYES) = IDYES then
    begin
      Exec(ExpandConstant('{cmd}'), '/C taskkill /IM {#AppExeName} /T /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Sleep(1500);
    end
    else
      Result := 'Please close Midum and run the installer again.';
  end;
end;

{ ---- download (+ checksum) ------------------------------------------------- }

function ReadExpectedHash(var Hash: String): Boolean;
var
  Raw: AnsiString;
begin
  Result := False;
  if LoadStringFromFile(ExpandConstant('{tmp}\payload.sha256'), Raw) then
  begin
    Hash := Lowercase(Copy(Trim(String(Raw)), 1, 64));
    Result := Length(Hash) = 64;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ExpectedHash: String;
begin
  if CurPageID <> wpReady then
  begin
    Result := True;
    Exit;
  end;

  Result := False;
  DownloadPage.Show;
  try
    try
      { 1) tiny checksum file published next to the zip }
      CurrentUrl := ChecksumUrl;
      DownloadPage.Clear;
      DownloadPage.Add(ChecksumUrl, 'payload.sha256', '');
      DownloadPage.Download;

      if not ReadExpectedHash(ExpectedHash) then
        RaiseException('Could not read the checksum for the latest Midum release.');

      { 2) the app itself, verified against that checksum }
      CurrentUrl := PayloadUrl;
      DownloadPage.Clear;
      DownloadPage.Add(PayloadUrl, '{#PayloadZip}', ExpectedHash);
      DownloadPage.Download;

      PayloadPath := ExpandConstant('{tmp}\{#PayloadZip}');
      Result := True;
    except
      if DownloadPage.AbortedByUser then
        Log('Download aborted by user.')
      else if Pos('404', GetExceptionMessage) > 0 then
        SuppressibleMsgBox(
          'Midum could not be downloaded: the file was not found (HTTP 404).' + #13#10#13#10 +
          CurrentUrl + #13#10#13#10 +
          'This usually means the latest GitHub release has no ' + '{#PayloadZip}' + ' attached yet (it is still ' +
          'building, was published as a draft/pre-release, or the repository is private). ' +
          'Try again in a few minutes, or download the release manually from {#AppURL}/releases.',
          mbCriticalError, MB_OK, IDOK)
      else
        SuppressibleMsgBox(
          'Midum could not be downloaded:' + #13#10#13#10 + GetExceptionMessage + #13#10#13#10 +
          CurrentUrl + #13#10#13#10 +
          'Check your internet connection and try again, or download the release manually from {#AppURL}/releases.',
          mbCriticalError, MB_OK, IDOK);
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

{ ---- extract ---------------------------------------------------------------- }

function Extract(const Zip, Dest: String): Boolean;
var
  ResultCode: Integer;
begin
  { bsdtar ships with Windows 10 1803+ and handles zip archives quickly. }
  Result := Exec(ExpandConstant('{sys}\tar.exe'), '-xf "' + Zip + '" -C "' + Dest + '"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if not Result then
  begin
    Log('tar extraction failed; falling back to PowerShell Expand-Archive.');
    Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath ''' + Zip +
      ''' -DestinationPath ''' + Dest + ''' -Force"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  end;
end;

procedure RecordInstalledVersion;
var
  VersionFile, Version: AnsiString;
  Root: Integer;
begin
  if LoadStringFromFile(ExpandConstant('{app}\VERSION'), VersionFile) then
  begin
    Version := Trim(String(VersionFile));
    if IsAdminInstallMode then Root := HKLM else Root := HKCU;
    if Version <> '' then
      RegWriteStringValue(Root, UninstallKey, 'DisplayVersion', String(Version));
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir: String;
begin
  if CurStep = ssInstall then
  begin
    AppDir := ExpandConstant('{app}');
    WizardForm.StatusLabel.Caption := 'Extracting Midum...';
    WizardForm.ProgressGauge.Style := npbstMarquee;
    try
      { Upgrade: remove the previous program files, but only if this folder
        really is a Midum install (never wipe an arbitrary folder). }
      if FileExists(AppDir + '\{#AppExeName}') then
      begin
        DelTree(AppDir + '\_internal', True, True, True);
        DeleteFile(AppDir + '\{#AppExeName}');
      end;
      ForceDirectories(AppDir);

      if not Extract(PayloadPath, AppDir) then
        RaiseException('Extracting the Midum download failed.');
      if not FileExists(AppDir + '\{#AppExeName}') then
        RaiseException('The download did not contain {#AppExeName}. Please try again.');
    finally
      WizardForm.ProgressGauge.Style := npbstNormal;
    end;
  end
  else if CurStep = ssPostInstall then
    RecordInstalledVersion;
end;

{ ---- uninstall: offer to remove user data ---------------------------------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    DataDir := GetEnv('LOCALAPPDATA') + '\Midum';
    if DirExists(DataDir) then
      if MsgBox('Also delete your Midum data (chats, memory, skills, settings and logs)?' + #13#10#13#10 +
                'Your API keys file (JarvisSecrets) is not touched either way.' + #13#10 +
                'Choose No to keep your data for a future reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
