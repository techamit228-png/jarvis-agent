; ============================================================
;  Установщик JARVIS. Собирается программой Inno Setup Compiler
;  (бесплатно): https://jrsoftware.org/isinfo.php
;
;  Как собрать готовый JarvisSetup.exe:
;  1) Сначала запусти build_exe.bat — он создаст dist\Jarvis.exe
;  2) Если есть иконка — положи её рядом как icon.ico
;     (можно конвертировать .png в .ico на любом бесплатном сайте)
;  3) Открой этот файл (setup.iss) в Inno Setup Compiler
;  4) Нажми Build -> Compile (или F9)
;  5) Готовый установщик появится в папке Output\JarvisSetup.exe
;     Его можно отправлять кому угодно — Python не нужен.
; ============================================================

#define MyAppName "Jarvis"
#define MyAppVersion "1.0"
#define MyAppExeName "Jarvis.exe"

[Setup]
AppId={{8F2B6C6E-4C7C-4E77-9B62-JARVIS0001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=JarvisSetup
Compression=lzma
SolidCompression=yes
; Иконка подключается только если файл icon.ico реально есть рядом —
; так сборка не падает, пока иконки ещё нет.
#ifexist "icon.ico"
SetupIconFile=icon.ico
#endif
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Раскомментируй, если хочешь скопировать иконку отдельным файлом внутрь папки установки:
; Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent
