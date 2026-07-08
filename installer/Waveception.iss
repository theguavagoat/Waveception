#define MyAppName "Waveception"
#define MyAppVersion "7.3.0"
#define MyAppPublisher "Phoenix Code"
#define MyAppExe "waveception7.py"

[Setup]
AppId={{7B2C4B54-0D59-4FA1-A1F0-F19B9C8C7539}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Waveception
DefaultGroupName=Waveception
DisableProgramGroupPage=yes
OutputDir=..
OutputBaseFilename=WaveceptionSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Waveception

[Dirs]
Name: "{commonappdata}\Waveception"; Permissions: users-modify

[Files]
Source: "..\app\waveception7.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\waveception_config.template.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\door_camera_map.template.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\securos_camera_map.template.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\securos_waveception_bridge.js"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\open_config.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\run_console.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\start_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\stop_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\install_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\scripts\uninstall_service.bat"; DestDir: "{app}"; Flags: ignoreversion
#ifexist "..\assets\waveception.ico"
Source: "..\assets\waveception.ico"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
#ifexist "..\assets\waveception.ico"
Name: "{group}\Open Waveception Config"; Filename: "{app}\open_config.bat"; WorkingDir: "{app}"; IconFilename: "{app}\waveception.ico"
#else
Name: "{group}\Open Waveception Config"; Filename: "{app}\open_config.bat"; WorkingDir: "{app}"
#endif
Name: "{group}\Run Waveception Console"; Filename: "{app}\run_console.bat"; WorkingDir: "{app}"
Name: "{group}\Start Waveception Service"; Filename: "{app}\start_service.bat"; WorkingDir: "{app}"
Name: "{group}\Stop Waveception Service"; Filename: "{app}\stop_service.bat"; WorkingDir: "{app}"
Name: "{group}\Uninstall Waveception"; Filename: "{uninstallexe}"
#ifexist "..\assets\waveception.ico"
Name: "{commondesktop}\Waveception Config"; Filename: "{app}\open_config.bat"; WorkingDir: "{app}"; IconFilename: "{app}\waveception.ico"
#else
Name: "{commondesktop}\Waveception Config"; Filename: "{app}\open_config.bat"; WorkingDir: "{app}"
#endif

[Run]
Filename: "{cmd}"; Parameters: "/C if not exist ""{commonappdata}\Waveception\waveception_config.json"" copy ""{app}\waveception_config.template.json"" ""{commonappdata}\Waveception\waveception_config.json"""; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/C if not exist ""{commonappdata}\Waveception\door_camera_map.json"" copy ""{app}\door_camera_map.template.json"" ""{commonappdata}\Waveception\door_camera_map.json"""; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/C if not exist ""{commonappdata}\Waveception\securos_camera_map.json"" copy ""{app}\securos_camera_map.template.json"" ""{commonappdata}\Waveception\securos_camera_map.json"""; Flags: runhidden waituntilterminated
Filename: "python"; Parameters: "-m pip install -r ""{app}\requirements.txt"""; Description: "Install Python requirements"; Flags: waituntilterminated
Filename: "python"; Parameters: """{app}\waveception7.py"" --config"; Description: "Configure Waveception"; Flags: waituntilterminated
Filename: "{app}\install_service.bat"; Description: "Install and start Waveception service"; Flags: waituntilterminated

[UninstallRun]
Filename: "{app}\uninstall_service.bat"; RunOnceId: "WaveceptionServiceUninstall"; Flags: waituntilterminated runhidden

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  if not Exec('python', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Python was not found. Install Python first, then run Waveception Setup again.', mbError, MB_OK);
    Result := False;
    exit;
  end;

  Result := True;
end;
