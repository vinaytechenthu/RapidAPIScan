@echo off
rem RapidPulse server installer for Windows. Double-click it, or run from a command prompt with options:
rem   install-rapidpulse.bat -AllowFrom 10.0.0.0/16 -Webhook https://hooks.slack.com/services/...
rem   install-rapidpulse.bat -Uninstall
rem All options are those of deploy\install-server.ps1.

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-server.ps1" %*
echo.
pause
