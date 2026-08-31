@echo off
setlocal

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

set "MODE=%~2"
if "%MODE%"=="" set "MODE=preview"

set "EVERY_MINUTES=%~3"
if "%EVERY_MINUTES%"=="" set "EVERY_MINUTES=5"

set "FORCE_ARG="
if /I "%ACTION%"=="install" set "FORCE_ARG=-Force"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_us_stock_live_scheduler.ps1" -Action "%ACTION%" -Mode "%MODE%" -EveryMinutes %EVERY_MINUTES% %FORCE_ARG%
exit /b %ERRORLEVEL%
