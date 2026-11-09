; Πρόγραμμα εγκατάστασης Price Tracker (Inno Setup 6)
; Build:  iscc /DMyAppVersion=0.3.0 installer\PriceTracker.iss
; Εγκατάσταση ανά χρήστη (χωρίς δικαιώματα διαχειριστή), ώστε οι αυτόματες ενημερώσεις
; να γίνονται αθόρυβα. Τα δεδομένα (Έγγραφα\PriceTracker) ΔΕΝ σβήνονται στην απεγκατάσταση.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Price Tracker"
#define MyAppExe "PriceTracker.exe"

[Setup]
AppId={{6F3B2C1E-7A4D-4F0B-9C2E-5D8A1B3E7F42}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=Nikos-1
AppPublisherURL=https://github.com/Nikos-1/price-tracker
AppSupportURL=https://github.com/Nikos-1/price-tracker
AppUpdatesURL=https://github.com/Nikos-1/price-tracker/releases
DefaultDirName={autopf}\PriceTracker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=PriceTracker-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
#ifexist "Greek.isl"
Name: "greek"; MessagesFile: "Greek.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Εικονίδιο στην επιφάνεια εργασίας"; GroupDescription: "Επιπλέον:"
Name: "autostart"; Description: "Εκκίνηση μαζί με τα Windows (στο system tray)"; GroupDescription: "Επιπλέον:"; Flags: unchecked

[Files]
Source: "..\dist\PriceTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Φάκελος δεδομένων (Έγγραφα\PriceTracker)"; Filename: "{userdocs}\PriceTracker"
Name: "{group}\Απεγκατάσταση {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PriceTracker"; ValueData: """{app}\{#MyAppExe}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart
; Σβήνει την αυτόματη εκκίνηση αν την ενεργοποίησε η ίδια η εφαρμογή
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "PriceTracker"; Flags: uninsdeletevalue dontcreatekey

[Dirs]
Name: "{userdocs}\PriceTracker"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Άνοιγμα του {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExe}"; Flags: runhidden; RunOnceId: "KillApp"
