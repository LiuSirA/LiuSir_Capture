@echo off
rem Standalone QHY camera-capture app (extracted from softGUI). Run from this folder.
rem Sets the SDK DLL to this folder's copy, then launches the GUI.
setlocal
set "QHYCCD_DLL=%~dp0remoteupdate_deps\qhyccd.dll"
cd /d "%~dp0"
python capture_gui.py %*
endlocal
