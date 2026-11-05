@echo off
cd /d "%~dp0"

if "%MODELSEEK_PORT%"=="" set "MODELSEEK_PORT=8088"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto :nopython

if not exist ".venv\Scripts\python.exe" (
    echo [modelseek] Creating virtual environment...
    %PY% -m venv .venv || goto :nopython
)

echo [modelseek] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :error

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%B"=="" set "%%A=%%B"

echo [modelseek] http://127.0.0.1:%MODELSEEK_PORT%   (Ctrl+C to stop)
if not "%MODELSEEK_NO_BROWSER%"=="1" start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%MODELSEEK_PORT%"
".venv\Scripts\python.exe" -m app.serve
goto :eof

:nopython
echo Python 3.10+ not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
goto :eof

:error
echo Failed to install dependencies.
pause
