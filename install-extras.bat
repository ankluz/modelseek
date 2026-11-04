@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run start.bat first - it creates the .venv environment.
    pause
    goto :eof
)

echo [modelseek] Installing optional modules...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-extra.txt || goto :error

echo [modelseek] Downloading Chromium for browser mode (~150 MB)...
".venv\Scripts\python.exe" -m playwright install chromium || goto :error

echo.
echo Done. Enable what you need in Settings - Sources / Translation (no restart needed).
pause
goto :eof

:error
echo Installation failed.
pause
