@echo off
net session >nul 2>nul
if not %errorlevel%==0 (
  echo Requesting administrator permission...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

netsh advfirewall firewall add rule name="Media Toolbox Backend 18181" dir=in action=allow protocol=TCP localport=18181
echo.
echo Firewall rule added for TCP 18181.
pause
