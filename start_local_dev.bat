@echo off
setlocal

cd /d "%~dp0"

if "%CONSOLE_PORT%"=="" set "CONSOLE_PORT=18180"
if "%BACKEND_PORT%"=="" set "BACKEND_PORT=18181"
if "%DEV_HOST%"=="" set "DEV_HOST=127.0.0.1"

if "%MEDIA_DIRS%"=="" set "MEDIA_DIRS=%CD%\sample-media"
if "%TRASH_DIR%"=="" set "TRASH_DIR=%CD%\trash"

if not exist "sample-media" (
  echo sample-media was not found. Creating demo files...
  powershell -ExecutionPolicy Bypass -File ".\make_sample_media.ps1"
)

echo.
echo Starting Media Toolbox local development mode...
echo Console: http://%DEV_HOST%:%CONSOLE_PORT%
echo Backend: http://%DEV_HOST%:%BACKEND_PORT%
echo.
echo Close the two opened windows to stop both services.
echo.

start "Media Toolbox Backend %BACKEND_PORT%" cmd /k "cd /d ""%CD%"" && set HOST=%DEV_HOST%&& set PORT=%BACKEND_PORT%&& set MEDIA_DIRS=%MEDIA_DIRS%&& set TRASH_DIR=%TRASH_DIR%&& set APP_DATA_DIR=%CD%\data\local-backend&& call start_local_backend.bat"
timeout /t 2 /nobreak >nul
start "Media Toolbox Console %CONSOLE_PORT%" cmd /k "cd /d ""%CD%"" && set HOST=%DEV_HOST%&& set PORT=%CONSOLE_PORT%&& set MEDIA_DIRS=%MEDIA_DIRS%&& set TRASH_DIR=%TRASH_DIR%&& set APP_DATA_DIR=%CD%\data\local-console&& set SUBTITLE_BACKEND_URL=http://%DEV_HOST%:%BACKEND_PORT%&& call start_local_console.bat"

echo Open http://%DEV_HOST%:%CONSOLE_PORT%
pause
