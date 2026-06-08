@echo off
setlocal

cd /d "%~dp0"

if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=18180"
if "%MEDIA_DIRS%"=="" set "MEDIA_DIRS=%CD%\sample-media"
if "%TRASH_DIR%"=="" set "TRASH_DIR=%CD%\trash"
if "%APP_DATA_DIR%"=="" set "APP_DATA_DIR=%CD%\data\local-console"
if "%SUBTITLE_BACKEND_URL%"=="" set "SUBTITLE_BACKEND_URL=http://127.0.0.1:18181"

set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if "%PYTHON_EXE%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if "%PYTHON_EXE%"=="" (
  echo Python was not found. Please install dependencies first.
  pause
  exit /b 1
)

if not exist "sample-media" (
  echo sample-media was not found. Creating demo files...
  powershell -ExecutionPolicy Bypass -File ".\make_sample_media.ps1"
)

echo.
echo Media Toolbox local console
echo URL: http://%HOST%:%PORT%
echo MEDIA_DIRS: %MEDIA_DIRS%
echo TRASH_DIR: %TRASH_DIR%
echo BACKEND: %SUBTITLE_BACKEND_URL%
echo.

"%PYTHON_EXE%" -m uvicorn app.main:app --host "%HOST%" --port "%PORT%"

pause
