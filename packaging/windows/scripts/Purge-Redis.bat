@echo off
setlocal
echo This removes Redis programs, configuration, data and logs.
"%SystemRoot%\System32\choice.exe" /C YN /N /M "Continue? [Y/N] "
if errorlevel 2 exit /b 0
rem Run as Administrator. The PowerShell entry point validates package trust.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-Redis.ps1" -Purge
set "redis_exit_code=%errorlevel%"
echo.
if not "%redis_exit_code%"=="0" echo Operation failed with exit code %redis_exit_code%.
pause
exit /b %redis_exit_code%
