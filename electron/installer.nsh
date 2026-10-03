; Custom NSIS steps for electron-builder (nsis.include in package.json).

; "Start with Windows" (Settings → General) writes HKCU\…\Run\NoType, and
; Task Manager may add an on/off flag under StartupApproved. Remove both on
; a real uninstall; an update keeps them because the exe path stays the same.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "NoType"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "NoType"
  ${endIf}
!macroend
