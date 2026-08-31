#ifndef MyAppVersion
  #define MyAppVersion "2.0.0"
#endif
#ifndef MyFileVersion
  #define MyFileVersion "2.0.0.0"
#endif
#ifndef MySourceDir
  #define MySourceDir "..\build\desktop\QuizForge"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "..\build\installer"
#endif

[Setup]
AppId={{A63C2B5C-5B3A-4DCE-8FC5-8F2E0EE258A4}
AppName=QuizForge
AppVersion={#MyAppVersion}
AppVerName=QuizForge {#MyAppVersion}
AppPublisher=QuizForge
VersionInfoVersion={#MyFileVersion}
VersionInfoProductVersion={#MyFileVersion}
DefaultDirName={localappdata}\Programs\QuizForge
DefaultGroupName=QuizForge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#MyOutputDir}
OutputBaseFilename=QuizForge-{#MyAppVersion}-Setup
SetupIconFile=..\assets\quizforge.ico
UninstallDisplayIcon={app}\QuizForge.exe
LicenseFile=..\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}\licenses"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\licenses\preview-license.txt"
Type: files; Name: "{app}\licenses\THIRD_PARTY_NOTICES-preview.md"
Type: files; Name: "{app}\_internal\assets\cloud_entitlement_public_key.pem"
Type: files; Name: "{app}\_internal\assets\license_public_key.pem"
Type: files; Name: "{app}\_internal\static\js\activation.js"

[Icons]
Name: "{group}\QuizForge"; Filename: "{app}\QuizForge.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\QuizForge"; Filename: "{app}\QuizForge.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\QuizForge.exe"; Description: "启动 QuizForge"; Flags: nowait postinstall skipifsilent
