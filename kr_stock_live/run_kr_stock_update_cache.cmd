@echo off
setlocal

set "PYTHONUTF8=1"

cd /d "%~dp0.."

set "PROFILE_JSON=%~1"
if "%PROFILE_JSON%"=="" set "PROFILE_JSON=%~dp0configs\kr_etf_cat24_rank9_top2_w8020_breadth45_isa.json"

set "LOG_DIR=%~dp0.cache\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\run_kr_stock_update_cache.log"

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')"`) do set "RUN_TS=%%I"

>> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo [%RUN_TS%] task=update_cache
>> "%LOG_FILE%" echo profile_json=%PROFILE_JSON%
py kr_stock_live\data\update_cache.py --profile-json "%PROFILE_JSON%" >> "%LOG_FILE%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [exit_code=%EXITCODE%]
py live_common\notify_task_result.py --system-label "KR ETF" --task-label "cache_update" --exit-code %EXITCODE% --run-ts "%RUN_TS%" --log-file "%LOG_FILE%" >> "%LOG_FILE%" 2>&1
set "NOTIFY_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [notify_exit_code=%NOTIFY_EXITCODE%]
>> "%LOG_FILE%" echo.

endlocal & exit /b %EXITCODE%
