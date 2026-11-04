@echo off
cd /d "%~dp0"
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\modelseek.cmd"

if /i "%~1"=="remove" (
    del "%TARGET%" >nul 2>nul
    echo modelseek removed from startup.
    pause
    goto :eof
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo Run start.bat first - it creates the .venv environment.
    pause
    goto :eof
)

> "%TARGET%" echo @echo off
>> "%TARGET%" echo cd /d "%~dp0"
>> "%TARGET%" echo for /f "usebackq eol=# tokens=1,* delims==" %%%%A in (".env") do if not "%%%%B"=="" set "%%%%A=%%%%B"
>> "%TARGET%" echo start "" /min ".venv\Scripts\pythonw.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8088

echo modelseek will start in the background when you sign in to Windows.
echo Open http://127.0.0.1:8088 in a browser. To undo: autostart.bat remove
pause
