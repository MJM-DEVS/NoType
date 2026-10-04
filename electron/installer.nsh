; Custom NSIS steps for electron-builder (nsis.include in package.json).

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
