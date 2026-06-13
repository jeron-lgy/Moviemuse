@echo off
setlocal

cd /d "%~dp0\..\.."

set "WHISPER_MODEL=large-v3"
set "WHISPER_MODEL_DIR=%CD%\data\local-backend\whisper-models"
set "WHISPER_DEVICE=cuda"
set "WHISPER_COMPUTE_TYPE=float16"
set "SUBTITLE_MAX_WORKERS=1"
set "HOST=0.0.0.0"
set "PORT=18181"
set "SUBTITLE_PATH_MAP="
set "COMPUTE_NODE_ONLY=1"

if not exist "%WHISPER_MODEL_DIR%" mkdir "%WHISPER_MODEL_DIR%"

set "PORT_PID="
for /f %%P in ('powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){$c.OwningProcess}"') do set "PORT_PID=%%P"
if not "%PORT_PID%"=="" (
  echo.
  echo Port %PORT% is already in use by PID %PORT_PID%.
  echo Stop the old backend first:
  echo   taskkill /PID %PORT_PID% /F
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    python -m venv .venv
  )
)

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python virtual environment was not created. Please install Python 3.12 and retry.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "from uvicorn.config import Config; from uvicorn.server import Server; import faster_whisper" >nul 2>nul
if errorlevel 1 (
  echo Installing Windows backend dependencies...
  echo If the network is slow, set HTTP_PROXY and HTTPS_PROXY to http://127.0.0.1:7897 before running this file.
  "%PYTHON_EXE%" -m pip install --upgrade pip
  "%PYTHON_EXE%" -m pip install --upgrade --force-reinstall -r requirements.txt
)

echo.
echo Windows 5090 subtitle backend
echo Backend URL: shown as LAN URL after the server starts
echo Health: shown after the server starts
echo Web UI: disabled on Windows worker
echo Manage settings from the Unraid subtitle console.
echo Model folder: %WHISPER_MODEL_DIR%
echo Path map: controlled by MovieMuse console
echo.
echo Make sure Windows can open paths sent by the console, for example: \\UNRAID\media
echo.

call ".\start_local_backend.bat"
