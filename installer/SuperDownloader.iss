; Inno Setup script for Super Downloader. Built by scripts\build.bat when Inno Setup 6 is
; installed (https://jrsoftware.org/isinfo.php), after PyInstaller made dist\SuperDownloader-folder.
; Installs for the current user only: no admin rights, no UAC prompt.

#define AppName "Super Downloader"
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{6C1E0B73-4B1F-4A34-9E53-5D2A1C7B8F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=dgnl.co
DefaultDirName={localappdata}\Programs\Super Downloader
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SuperDownloader-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\SuperDownloader.exe
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\SuperDownloader-folder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\SuperDownloader.exe"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\SuperDownloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SuperDownloader.exe"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
