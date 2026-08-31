@echo off
setlocal

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

set "MODE=%~2"
if "%MODE%"=="" set "MODE=preview"

set "MINUTE=%~3"
if "%MINUTE%"=="" set "MINUTE=2"

set "FORCE_ARG="
if /I "%ACTION%"=="install" set "FORCE_ARG=-Force"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_live_scheduler_v2.ps1" -Action "%ACTION%" -Mode "%MODE%" -Minute %MINUTE% %FORCE_ARG%
exit /b %ERRORLEVEL%
