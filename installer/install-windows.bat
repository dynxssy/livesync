@echo off
rem LiveSync installer for Windows. Double-click this file.
setlocal
set "HERE=%~dp0.."
if "%LIVESYNC_USER_LIBRARY%"=="" (set "UL=%USERPROFILE%\Documents\Ableton\User Library") else (set "UL=%LIVESYNC_USER_LIBRARY%")
echo Installing LiveSync into: %UL%
if exist "%UL%\Remote Scripts\LiveSync" rmdir /s /q "%UL%\Remote Scripts\LiveSync"
mkdir "%UL%\Remote Scripts\LiveSync" 2>nul
mkdir "%UL%\Presets\Audio Effects\Max Audio Effect" 2>nul
xcopy /e /i /y /q "%HERE%\remote_script\LiveSync" "%UL%\Remote Scripts\LiveSync" >nul
for /d /r "%UL%\Remote Scripts\LiveSync" %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
copy /y "%HERE%\m4l\LiveSync.amxd" "%UL%\Presets\Audio Effects\Max Audio Effect\LiveSync.amxd" >nul
mkdir "%USERPROFILE%\Documents\LiveSync" 2>nul
echo.
echo Done. Next:
echo   1. Restart Ableton Live.
echo   2. Options ^> Preferences ^> Link, Tempo ^& MIDI ^> Control Surface: choose LiveSync (Input/Output: None).
echo   3. Drop "LiveSync" (Browser ^> Audio Effects ^> Max Audio Effect) on your Master track,
echo      or open http://127.0.0.1:47010 in your web browser.
pause
