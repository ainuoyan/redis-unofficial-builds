@echo off
setlocal DisableDelayedExpansion
rem ASCII only: PowerShell owns localized output and temporary UTF-8 encoding.
set "redis_lang=en"
set "redis_help="

:parse
if "%~1"=="" goto run
if /i "%~1"=="--help" goto help
if /i not "%~1"=="--lang" goto usage
if /i "%~2"=="en" (
    set "redis_lang=en"
) else if /i "%~2"=="zh" (
    set "redis_lang=zh"
) else (
    goto usage
)
shift
shift
goto parse

:help
set "redis_help=-Help"
shift
goto parse

:run
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-Redis.ps1" -Lang %redis_lang% -FromBatch -Purge -ConfirmPurge %redis_help%
exit /b %errorlevel%

:usage
echo Usage: Purge-Redis.bat [--lang en^|zh] [--help]
exit /b 2
