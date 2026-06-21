@echo off
setlocal

cd /d "%~dp0"

if "%HOST%"=="" set "HOST=127.0.0.1"
if "%PORT%"=="" set "PORT=18181"
if "%MEDIA_DIRS%"=="" set "MEDIA_DIRS=%CD%\sample-media"
if "%TRASH_DIR%"=="" set "TRASH_DIR=%CD%\trash"
if "%APP_DATA_DIR%"=="" set "APP_DATA_DIR=%CD%\data\local-backend"
if "%WHISPER_MODEL_DIR%"=="" set "WHISPER_MODEL_DIR=%APP_DATA_DIR%\whisper-models"
if "%WHISPER_MODEL%"=="" set "WHISPER_MODEL=large-v3"
if "%WHISPER_DEVICE%"=="" set "WHISPER_DEVICE=cuda"
if "%WHISPER_COMPUTE_TYPE%"=="" set "WHISPER_COMPUTE_TYPE=float16"
if "%SUBTITLE_MAX_WORKERS%"=="" set "SUBTITLE_MAX_WORKERS=1"
if "%COMPUTE_NODE_ONLY%"=="" set "COMPUTE_NODE_ONLY=1"

if not exist "%WHISPER_MODEL_DIR%" mkdir "%WHISPER_MODEL_DIR%"

set "PYTHON_EXE="
if exist "python\python.exe" set "PYTHON_EXE=python\python.exe"
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

if exist ".venv\Lib\site-packages\nvidia\cublas\bin" set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cublas\bin;%PATH%"
if exist ".venv\Lib\site-packages\nvidia\cudnn\bin" set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cudnn\bin;%PATH%"
if exist ".venv\Lib\site-packages\nvidia\cuda_nvrtc\bin" set "PATH=%CD%\.venv\Lib\site-packages\nvidia\cuda_nvrtc\bin;%PATH%"
set "PORTABLE_DEPS_REL="
if exist "python\current-deps-path.txt" set /p PORTABLE_DEPS_REL=<"python\current-deps-path.txt"
if not "%PORTABLE_DEPS_REL%"=="" (
  if exist "%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cublas\bin" set "PATH=%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cublas\bin;%PATH%"
  if exist "%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cudnn\bin" set "PATH=%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cudnn\bin;%PATH%"
  if exist "%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cuda_nvrtc\bin" set "PATH=%CD%\python\%PORTABLE_DEPS_REL%\nvidia\cuda_nvrtc\bin;%PATH%"
)
if exist "python\Lib\site-packages\nvidia\cublas\bin" set "PATH=%CD%\python\Lib\site-packages\nvidia\cublas\bin;%PATH%"
if exist "python\Lib\site-packages\nvidia\cudnn\bin" set "PATH=%CD%\python\Lib\site-packages\nvidia\cudnn\bin;%PATH%"
if exist "python\Lib\site-packages\nvidia\cuda_nvrtc\bin" set "PATH=%CD%\python\Lib\site-packages\nvidia\cuda_nvrtc\bin;%PATH%"

echo.
echo Media Toolbox local compute backend
echo URL: http://%HOST%:%PORT%
echo Web UI: disabled on compute worker
echo Manage settings from the Unraid subtitle console.
echo WHISPER: %WHISPER_MODEL% / %WHISPER_DEVICE% / %WHISPER_COMPUTE_TYPE%
echo MODELS: %WHISPER_MODEL_DIR%
echo DATA: %APP_DATA_DIR%
echo.

"%PYTHON_EXE%" "%CD%\run_worker.py"

pause
