; Custom NSIS steps for electron-builder (nsis.include in package.json).

; backend.exe (PyInstaller) runs next to NoType.exe, but electron-builder's
; "app is running" check only closes NoType.exe. The backend quits by itself
; once NoType.exe is gone (its stdin closes), yet a busy or hung one keeps
; files under resources\backend locked and the update or uninstall fails.
;
; So when no NoType.exe from this install folder runs, give a leftover
; backend.exe from the same folder 5 s to exit, then end it. Matching is by
; full path, so a backend.exe of any other program is never touched. A
; running NoType.exe is left to electron-builder's own check, which comes
; later; ending its backend now would only make NoType restart it.
;
; The folder reaches PowerShell through an environment variable rather than
; the command line, so quotes in the path can't break the command. Uses only
; the built-in System and nsExec plugins; if PowerShell is missing or fails,
; nothing changes compared to before.
!define NOTYPE_STOP_BACKEND_PS "\
  $$dir = $$env:NOTYPE_INSTDIR; if (-not $$dir) { exit }; \
  $$app = Join-Path $$dir 'NoType.exe'; \
  $$backend = Join-Path $$dir 'resources\backend\backend.exe'; \
  $$procs = @(Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath }); \
  if ($$procs | Where-Object { $$_.ExecutablePath -eq $$app }) { exit }; \
  $$ids = @($$procs | Where-Object { $$_.ExecutablePath -eq $$backend } | ForEach-Object { $$_.ProcessId }); \
  if ($$ids.Count -eq 0) { exit }; \
  Wait-Process -Id $$ids -Timeout 5 -ErrorAction SilentlyContinue; \
  Stop-Process -Id $$ids -Force -ErrorAction SilentlyContinue; \
  Wait-Process -Id $$ids -Timeout 5 -ErrorAction SilentlyContinue"

!macro notypeStopLeftoverBackend
  Push $0
  System::Call 'Kernel32::SetEnvironmentVariable(t "NOTYPE_INSTDIR", t "$INSTDIR")'
  nsExec::Exec `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "${NOTYPE_STOP_BACKEND_PS}"`
  Pop $0
  Pop $0
!macroend

; Installer start: $INSTDIR is the existing install folder (update or
; reinstall over it). Nothing to do on a first install.
!macro customInit
  !insertmacro notypeStopLeftoverBackend
!macroend

; Uninstaller start, also when an update runs the old version's uninstaller
; silently after NoType.exe was closed.
!macro customUnInit
  !insertmacro notypeStopLeftoverBackend
!macroend

; "Start with Windows" (Settings → General) writes HKCU\…\Run\NoType, and
; Task Manager may add an on/off flag under StartupApproved. Remove both on
; a real uninstall; an update keeps them because the exe path stays the same.
;
; The installer also leaves a copy of itself (about 1 GB) in
; %LOCALAPPDATA%\notype-updater, so a later update downloads only what
; changed; downloaded updates wait there as well. Updates need it, a real
; uninstall removes it.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "NoType"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "NoType"
    RMDir /r "$LOCALAPPDATA\notype-updater"
  ${endIf}
!macroend
