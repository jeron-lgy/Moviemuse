@echo off
setlocal

cd /d "%~dp0"

if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=18180"
if "%MEDIA_DIRS%"=="" set "MEDIA_DIRS=sample-media"
if "%TRASH_DIR%"=="" set "TRASH_DIR=trash"
if "%APP_DATA_DIR%"=="" set "APP_DATA_DIR=data"

set "PYTHON_EXE="

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
)

where python >nul 2>nul
if "%PYTHON_EXE%"=="" (
  if not errorlevel 1 set "PYTHON_EXE=python"
)

if "%PYTHON_EXE%"=="" (
  if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  )
)

if "%PYTHON_EXE%"=="" (
  echo Python was not found. Please install Python or run this from Codex Desktop.
  pause
  exit /b 1
)

if exist ".venv\Lib\site-packages\nvidia\cublas\bin" (
  set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cublas\bin;%PATH%"
)
if exist ".venv\Lib\site-packages\nvidia\cudnn\bin" (
  set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cudnn\bin;%PATH%"
)
if exist ".venv\Lib\site-packages\nvidia\cuda_nvrtc\bin" (
  set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cuda_nvrtc\bin;%PATH%"
)

if not exist "sample-media" (
  echo sample-media was not found. Creating demo files...
  powershell -ExecutionPolicy Bypass -File ".\make_sample_media.ps1"
)

echo.
echo Media toolbox server
echo URL: http://%HOST%:%PORT%
echo MEDIA_DIRS: %MEDIA_DIRS%
echo TRASH_DIR: %TRASH_DIR%
echo Subtitles UI: http://%HOST%:%PORT%/subtitles
echo.
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" -m uvicorn app.main:app --host "%HOST%" --port "%PORT%"

pause
