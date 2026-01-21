@echo off
set "TARGET_SCRIPT=%~dp0run_secure.bat"
set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Start_Production_Server.lnk"

echo [SETUP] Creating startup shortcut for: "%TARGET_SCRIPT%"

:: Use PowerShell to create a proper .lnk shortcut in the Startup folder
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%TARGET_SCRIPT%'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Auto-start Production Server'; $s.Save()"

echo.
echo [SUCCESS] Shortcut created in Windows Startup folder.
echo The server will now open automatically every time you log in.
echo.
pause