@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PYTHON_EXE="
set "EMBEDDED_SITE_PACKAGES="
set "EMBEDDED_SITE_PACKAGES_REL="
set "PORTABLE_PYTHON=0"
set "ROOT_DIR=%CD%"
if exist "python\python.exe" (
  set "PYTHON_EXE=python\python.exe"
  set "PORTABLE_PYTHON=1"
)
if "%PYTHON_EXE%"=="" if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

if "%PYTHON_EXE%"=="" (
  if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3.12 -m venv .venv
    if errorlevel 1 (
      python -m venv .venv
    )
  )
  if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
)

if "%PYTHON_EXE%"=="" (
  echo.
  echo Python was not found. Use a package with bundled portable Python, or install Python 3.12 and run this file again.
  echo.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "from uvicorn.config import Config; from uvicorn.server import Server; import faster_whisper" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo Windows worker dependencies are already installed.
  echo.
  exit /b 0
)

set "REQ_FILE=requirements.txt"
if exist "requirements-windows-worker.lock.txt" set "REQ_FILE=requirements-windows-worker.lock.txt"

if exist "wheels" (
  echo.
  echo Installing Windows worker dependencies from bundled wheels...
  if "%PORTABLE_PYTHON%"=="1" (
    call :prepare_portable_target
    if not exist "!EMBEDDED_SITE_PACKAGES!" mkdir "!EMBEDDED_SITE_PACKAGES!"
    if exist "pip.pyz" (
      "%PYTHON_EXE%" "%CD%\pip.pyz" install --no-index --find-links "%CD%\wheels" -r "%REQ_FILE%" --target "!EMBEDDED_SITE_PACKAGES!"
    ) else (
      echo pip.pyz was not found in the portable package.
      exit /b 1
    )
  ) else (
    "%PYTHON_EXE%" -m pip install --no-index --find-links "%CD%\wheels" -r "%REQ_FILE%"
  )
) else (
  echo.
  echo Bundled wheels folder was not found. Installing from internet...
  echo If the network is slow, set HTTP_PROXY and HTTPS_PROXY to http://127.0.0.1:7897 before running this file.
  if "%PORTABLE_PYTHON%"=="1" (
    if not exist "pip.pyz" (
      echo Portable Python needs bundled wheels and pip.pyz.
      exit /b 1
    )
    call :prepare_portable_target
    if not exist "!EMBEDDED_SITE_PACKAGES!" mkdir "!EMBEDDED_SITE_PACKAGES!"
    "%PYTHON_EXE%" "%CD%\pip.pyz" install -r "%REQ_FILE%" --target "!EMBEDDED_SITE_PACKAGES!"
  ) else (
    "%PYTHON_EXE%" -m pip install --upgrade pip
    "%PYTHON_EXE%" -m pip install -r "%REQ_FILE%"
  )
)

if errorlevel 1 (
  echo.
  echo Dependency installation failed.
  echo.
  pause
  exit /b 1
)

if "%PORTABLE_PYTHON%"=="1" (
  call :activate_portable_target
  if errorlevel 1 exit /b 1
)

echo.
echo Windows worker dependencies installed.
echo.

exit /b 0

:prepare_portable_target
for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMddHHmmss"') do set "DEPS_STAMP=%%D"
if "%DEPS_STAMP%"=="" set "DEPS_STAMP=new"
set "EMBEDDED_SITE_PACKAGES_REL=Lib\media-toolbox-packages-%DEPS_STAMP%"
set "EMBEDDED_SITE_PACKAGES=%CD%\python\%EMBEDDED_SITE_PACKAGES_REL%"
echo Installing into %EMBEDDED_SITE_PACKAGES_REL%
exit /b 0

:activate_portable_target
if "%EMBEDDED_SITE_PACKAGES_REL%"=="" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pth=Join-Path $env:ROOT_DIR 'python\python312._pth'; $current=Join-Path $env:ROOT_DIR 'python\current-deps-path.txt'; @('python312.zip','.', '..', $env:EMBEDDED_SITE_PACKAGES_REL, 'Lib\site-packages', 'import site') | Set-Content -LiteralPath $pth -Encoding ASCII; Set-Content -LiteralPath $current -Value $env:EMBEDDED_SITE_PACKAGES_REL -Encoding ASCII"
if errorlevel 1 (
  echo Failed to update portable Python dependency path.
  exit /b 1
)
exit /b 0
