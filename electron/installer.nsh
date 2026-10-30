; Custom NSIS steps for electron-builder (nsis.include in package.json).

; backend.exe (PyInstaller) runs next to NoType.exe. It quits by itself once
; NoType.exe is gone (its stdin closes), yet a busy or hung one can outlive
; it and keep files under resources\backend locked, so the update or
; uninstall fails. Whether electron-builder's own "app is running" check
; also ends such an orphan is not verified, so this does not rely on it.
;
; So when no NoType.exe from this install folder runs, give a leftover
; backend.exe from the same folder 5 s to exit, then end it. Matching is by
; full path, so a backend.exe of any other program is never touched. A
; running NoType.exe is left to electron-builder's own check, which comes
; later; ending its backend now would only make NoType restart it.
;
; In the update flow NoType.exe is usually still shutting down when the new
; installer starts, so the step that helps there is customUnInit of the
; installed version. It takes effect only once that version already has
; this uninstaller, i.e. from the second update after this change.
;
; The folder reaches PowerShell through an environment variable rather than
; the command line, so quotes in the path can't break the command. Uses only
; the built-in System and nsExec plugins; if PowerShell is missing or fails,
; nothing changes compared to before. PowerShell only starts when the
; backend exists, so a first install doesn't wait for it.
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
  ${If} ${FileExists} "$INSTDIR\resources\backend\backend.exe"
    Push $0
    System::Call 'Kernel32::SetEnvironmentVariable(t "NOTYPE_INSTDIR", t "$INSTDIR")'
    nsExec::Exec `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "${NOTYPE_STOP_BACKEND_PS}"`
    Pop $0
    Pop $0
    ; Don't pass the variable on to NoType when "run after finish" starts it.
    System::Call 'Kernel32::SetEnvironmentVariable(t "NOTYPE_INSTDIR", p 0)'
  ${EndIf}
!macroend

; Installer start: $INSTDIR is the existing install folder (update or
; reinstall over it). On a first install there is no backend.exe yet.
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
