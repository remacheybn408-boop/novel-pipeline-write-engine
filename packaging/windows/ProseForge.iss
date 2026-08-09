; ProseForge Windows installer (Inno Setup 6, Unicode).
; Build from a machine with Inno Setup installed:
;   ISCC.exe packaging\windows\ProseForge.iss
; This file is UTF-8 with BOM so the Chinese UI strings compile correctly.
;
; Contract:
;   payload   = artifacts\native\windows\ProseForge\ (proseforge.exe + _internal\)
;   data dir  = {localappdata}\ProseForge  (per-user; NEVER deleted on uninstall)
;   upgrade   = stop app -> backup with OLD binary -> replace -> migrate with NEW binary
#define MyAppVersion "1.5.0"

[Setup]
AppId={{9F4E2C7A-1B6D-4E3A-A5C8-2D7F0B9E6A41}}
AppName=ProseForge
AppVersion={#MyAppVersion}
AppPublisher=ProseForge
DefaultDirName={autopf}\ProseForge
DefaultGroupName=ProseForge
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Uninstallable=yes
SourceDir=..\..
OutputDir=artifacts\native\windows
OutputBaseFilename=ProseForge-{#MyAppVersion}-windows-setup

[Dirs]
; Per-user data directory; survives uninstalls and upgrades.
Name: "{localappdata}\ProseForge"
Name: "{localappdata}\ProseForge\logs"

[Files]
Source: "artifacts\native\windows\ProseForge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; Installer helper scripts must live in {app}: [Run]/[UninstallRun] reference them there.
Source: "packaging\windows\service_install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "packaging\windows\service_uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ProseForge Web"; Filename: "{app}\proseforge.exe"; Parameters: "web --data-dir ""{localappdata}\ProseForge"""
Name: "{group}\卸载 ProseForge"; Filename: "{uninstallexe}"

[Tasks]
Name: "autostart"; Description: "注册开机自启（用户登录时启动 ProseForge Web，写入 HKCU Run 键）"; GroupDescription: "附加任务:"; Flags: unchecked

[Run]
Filename: "{app}\proseforge.exe"; Parameters: "web --data-dir ""{localappdata}\ProseForge"""; Description: "立即启动 ProseForge Web（http://127.0.0.1:8000）"; Flags: postinstall nowait skipifsilent unchecked
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service_install.ps1"" -Executable ""{app}\proseforge.exe"" -DataDir ""{localappdata}\ProseForge"""; StatusMsg: "正在注册开机自启..."; Flags: runhidden waituntilterminated; Tasks: autostart

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service_uninstall.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAutostartTask"

[UninstallDelete]
; Logs only. The data root {localappdata}\ProseForge must never be touched.
Type: filesandordirs; Name: "{localappdata}\ProseForge\logs"

[Code]
function UserDataDir: String;
begin
  Result := ExpandConstant('{localappdata}\ProseForge');
end;

{ Stop any running ProseForge instance (web server holds file locks on the
  binaries being replaced). Never blocks install or uninstall. }
procedure StopRunningApp;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM proseforge.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ Pre-install: stop the app, then back up user data with the OLD binary.
  A failed backup only warns; it must not block the upgrade. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OldBinary: String;
  ResultCode: Integer;
begin
  Result := '';
  StopRunningApp;
  OldBinary := ExpandConstant('{app}\proseforge.exe');
  if FileExists(OldBinary) then
  begin
    if (not Exec(OldBinary,
                 'backup create --source "' + UserDataDir + '" --root "' + UserDataDir + '\backups"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      MsgBox('升级前自动备份失败，安装仍将继续。'#13#10 +
             '建议先手动备份数据目录：' + UserDataDir,
             mbInformation, MB_OK);
  end;
end;

{ Post-install: run data migrations with the NEW binary. On failure point the
  user at the pre-upgrade backup so they can roll back data and old binary. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if (not Exec(ExpandConstant('{app}\proseforge.exe'),
                 'upgrade --data-dir "' + UserDataDir + '"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      MsgBox('ProseForge 数据迁移未完成。'#13#10 +
             '升级前备份保存在 ' + UserDataDir + '\backups，'#13#10 +
             '可恢复备份数据并重新安装旧版本以回滚。',
             mbInformation, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningApp;
end;
