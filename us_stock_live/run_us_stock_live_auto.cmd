@echo off
setlocal

cd /d "%~dp0.."

set "MODE=%~1"
if "%MODE%"=="" set "MODE=preview"

set "PROFILE_JSON=%~2"
if "%PROFILE_JSON%"=="" set "PROFILE_JSON=%~dp0configs\active_profile.json"
set "LIMIT_BUFFER_PCT=%~3"
if "%LIMIT_BUFFER_PCT%"=="" set "LIMIT_BUFFER_PCT=0.10"
set "BOOTSTRAP_POLICY=%~4"
set "SKIP_CACHE_UPDATE=%~5"
set "FILL_WAIT_SECONDS=%~6"
if "%FILL_WAIT_SECONDS%"=="" set "FILL_WAIT_SECONDS=20"
set "FILL_RETRY_COUNT=%~7"
if "%FILL_RETRY_COUNT%"=="" set "FILL_RETRY_COUNT=2"
if /I "%BOOTSTRAP_POLICY%"=="skip-cache" (
  set "BOOTSTRAP_POLICY="
  set "SKIP_CACHE_UPDATE=skip-cache"
)

set "RUN_ARGS="
if /I "%MODE%"=="preview" set "RUN_ARGS="
if /I "%MODE%"=="live" set "RUN_ARGS=--execute --confirm-live --auto-finalize --limit-buffer-pct %LIMIT_BUFFER_PCT% --continue-after-sell --continue-after-buy --fill-wait-seconds %FILL_WAIT_SECONDS% --fill-retry-count %FILL_RETRY_COUNT%"

if /I not "%MODE%"=="preview" if /I not "%MODE%"=="live" (
  echo Invalid mode: %MODE%
  echo Usage: run_us_stock_live_auto.cmd [preview^|live] [profile_json] [limit_buffer_pct] [bootstrap_policy] [skip-cache] [fill_wait_seconds] [fill_retry_count]
  endlocal & exit /b 2
)

if not "%PROFILE_JSON%"=="" set "RUN_ARGS=%RUN_ARGS% --profile-json "%PROFILE_JSON%""
if not "%BOOTSTRAP_POLICY%"=="" set "RUN_ARGS=%RUN_ARGS% --bootstrap-policy %BOOTSTRAP_POLICY%"
if /I "%SKIP_CACHE_UPDATE%"=="skip-cache" set "RUN_ARGS=%RUN_ARGS% --skip-cache-update"

set "LOG_DIR=%~dp0.cache\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\run_us_stock_live_auto_%MODE%.log"

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')"`) do set "RUN_TS=%%I"

>> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo [%RUN_TS%] mode=%MODE%
if not "%PROFILE_JSON%"=="" >> "%LOG_FILE%" echo profile_json=%PROFILE_JSON%
if /I "%MODE%"=="live" >> "%LOG_FILE%" echo limit_buffer_pct=%LIMIT_BUFFER_PCT%
if /I "%MODE%"=="live" >> "%LOG_FILE%" echo continue_after_sell=true
if /I "%MODE%"=="live" >> "%LOG_FILE%" echo continue_after_buy=true
if /I "%MODE%"=="live" >> "%LOG_FILE%" echo fill_wait_seconds=%FILL_WAIT_SECONDS%
if /I "%MODE%"=="live" >> "%LOG_FILE%" echo fill_retry_count=%FILL_RETRY_COUNT%
if /I "%SKIP_CACHE_UPDATE%"=="skip-cache" >> "%LOG_FILE%" echo skip_cache_update=true
if not "%BOOTSTRAP_POLICY%"=="" >> "%LOG_FILE%" echo bootstrap_policy=%BOOTSTRAP_POLICY%
py us_stock_live\trading\run_auto_rebalance.py %RUN_ARGS% >> "%LOG_FILE%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [exit_code=%EXITCODE%]
py us_stock_live\performance\snapshot_account.py --profile-json "%PROFILE_JSON%" --source "run_us_stock_live_auto_%MODE%" --run-exit-code %EXITCODE% >> "%LOG_FILE%" 2>&1
set "SNAPSHOT_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [snapshot_exit_code=%SNAPSHOT_EXITCODE%]
py live_common\notify_live_result.py --system-label "US Stock" --runner us --mode "%MODE%" --profile-json "%PROFILE_JSON%" --exit-code %EXITCODE% --snapshot-exit-code %SNAPSHOT_EXITCODE% --run-ts "%RUN_TS%" --log-file "%LOG_FILE%" >> "%LOG_FILE%" 2>&1
set "NOTIFY_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [notify_exit_code=%NOTIFY_EXITCODE%]
>> "%LOG_FILE%" echo.

endlocal & exit /b %EXITCODE%
