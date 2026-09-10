@echo off
setlocal
rem Run as Administrator. The PowerShell entry point validates package trust.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-Redis.ps1"
set "redis_exit_code=%errorlevel%"
echo.
if not "%redis_exit_code%"=="0" echo Operation failed with exit code %redis_exit_code%.
pause
exit /b %redis_exit_code%
