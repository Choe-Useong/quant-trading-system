@echo off
setlocal

cd /d "%~dp0.."

set "MODE=%~1"
if "%MODE%"=="" set "MODE=preview"

set "EXECUTION_CONFIG=configs\examples\live_portfolio_v2.example.json"
set "LIVE_MARKETS=KRW-BTC,KRW-ETH,KRW-SOL,KRW-XRP"
set "LIVE_CANDLES=20000"
set "LIVE_DATA_DIR=data\upbit_live"
set "LIVE_CANDLE_DIR=data\upbit_live\minutes\60"
set "LIVE_CACHE_DIR=data\upbit_live_cache\60_4core"
set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\run_live_job_v2_%MODE%.log"

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')"`) do set "RUN_TS=%%I"

>> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo [%RUN_TS%] mode=%MODE% config=%EXECUTION_CONFIG%
>> "%LOG_FILE%" echo update live candles markets=%LIVE_MARKETS% candles=%LIVE_CANDLES%
py scripts\upbit_minute_collector.py --unit 60 --out-dir "%LIVE_DATA_DIR%" --markets "%LIVE_MARKETS%" --candles %LIVE_CANDLES% --exclude-warnings --merge-existing --drop-incomplete >> "%LOG_FILE%" 2>&1
set "UPDATE_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [candle_update_exit_code=%UPDATE_EXITCODE%]
if not "%UPDATE_EXITCODE%"=="0" (
  >> "%LOG_FILE%" echo abort: candle update failed
  py live\performance\snapshot_account.py --execution-config-json "%EXECUTION_CONFIG%" --source "run_live_job_v2_%MODE%_candle_update_failed" --run-exit-code %UPDATE_EXITCODE% >> "%LOG_FILE%" 2>&1
  set "SNAPSHOT_EXITCODE=%ERRORLEVEL%"
  >> "%LOG_FILE%" echo [snapshot_exit_code=%SNAPSHOT_EXITCODE%]
  endlocal & exit /b %UPDATE_EXITCODE%
)

>> "%LOG_FILE%" echo build live cache candle_dir=%LIVE_CANDLE_DIR% cache_dir=%LIVE_CACHE_DIR%
py scripts\build_upbit_research_cache.py --candle-dir "%LIVE_CANDLE_DIR%" --out-dir "%LIVE_CACHE_DIR%" >> "%LOG_FILE%" 2>&1
set "CACHE_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [cache_build_exit_code=%CACHE_EXITCODE%]
if not "%CACHE_EXITCODE%"=="0" (
  >> "%LOG_FILE%" echo abort: cache build failed
  py live\performance\snapshot_account.py --execution-config-json "%EXECUTION_CONFIG%" --source "run_live_job_v2_%MODE%_cache_build_failed" --run-exit-code %CACHE_EXITCODE% >> "%LOG_FILE%" 2>&1
  set "SNAPSHOT_EXITCODE=%ERRORLEVEL%"
  >> "%LOG_FILE%" echo [snapshot_exit_code=%SNAPSHOT_EXITCODE%]
  endlocal & exit /b %CACHE_EXITCODE%
)

py live\run_live_job_v2.py --mode %MODE% --execution-config-json "%EXECUTION_CONFIG%" >> "%LOG_FILE%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [exit_code=%EXITCODE%]
py live\performance\snapshot_account.py --execution-config-json "%EXECUTION_CONFIG%" --source "run_live_job_v2_%MODE%" --run-exit-code %EXITCODE% >> "%LOG_FILE%" 2>&1
set "SNAPSHOT_EXITCODE=%ERRORLEVEL%"
>> "%LOG_FILE%" echo [snapshot_exit_code=%SNAPSHOT_EXITCODE%]
>> "%LOG_FILE%" echo.

endlocal & exit /b %EXITCODE%
