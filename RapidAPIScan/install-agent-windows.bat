@echo off
rem RapidPulse agent installer for Windows servers.
rem   1. Fill in rapidpulse-agent.conf (next to this file).
rem   2. Double-click this file and accept the Administrator prompt.
rem Remove the agent:  install-agent-windows.bat -Uninstall
rem Use another settings file:  install-agent-windows.bat -ConfigFile D:\path\my-agent.conf

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b
)

set "CONF=%~dp0rapidpulse-agent.conf"
echo %* | findstr /i /c:"-ConfigFile" >nul && set "CONF="
if defined CONF (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-agent.ps1" -ConfigFile "%CONF%" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\install-agent.ps1" %*
)
echo.
pause
