@echo off
REM ============================================================
REM  Bid Assistant Platform - Backend auto-restart watchdog
REM  Monitors the backend process and restarts it after a crash,
REM  with a restart cap to prevent restart storms.
REM ============================================================

title BidAssistant Backend Watchdog (v2.0)

setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "BACKEND_DIR=%ROOT_DIR%backend"
set "LOG_DIR=%ROOT_DIR%logs"
set "PYTHON_EXEC=python"
set "BACKEND_PORT=8000"
set "RESTART_DELAY=3"
set "MAX_RESTARTS_PER_HOUR=10"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ============================================================
REM  Sync shared port config (read by the Vite plugin, single source)
REM ============================================================
python "%ROOT_DIR%scripts\sync_backend_ports.py" %BACKEND_PORT% 5175

set "CRASH_LOG=%LOG_DIR%\backend_crash_log.txt"
set "WATCHDOG_LOG=%LOG_DIR%\watchdog.log"
set "BACKEND_STDOUT=%LOG_DIR%\backend_stdout.log"
set "BACKEND_STDERR=%LOG_DIR%\backend_stderr.log"
set "HEALTH_LOG=%LOG_DIR%\health_check.log"

set "TOTAL_RESTARTS=0"
set "HOUR_START_COUNT=0"
set "LAST_RESET_TIME=%date% %time%"

echo ============================================================
echo   Backend watchdog v2.0 starting...
echo   Crash log: %CRASH_LOG%
echo   Watchdog log: %WATCHDOG_LOG%
echo   Press Ctrl+C to stop the watchdog
echo ============================================================
echo.
echo [%date% %time%] ===== watchdog started ===== >> "%WATCHDOG_LOG%"

:WATCHDOG_LOOP

    REM Check health state (wait until backend is ready)
    call :WAIT_BACKEND_READY

    if !ERRORLEVEL! NEQ 0 (
        echo [%date% %time%] Backend not ready, trying to start... >> "%WATCHDOG_LOG%"
        goto :START_BACKEND
    )

    REM Health monitoring loop
    set "HEALTH_CHECK_COUNT=0"
    :HEALTH_LOOP
        timeout /t 30 /nobreak >nul
        set /a HEALTH_CHECK_COUNT+=1

        REM Every 10 checks (5 min) do a full health confirmation
        if !HEALTH_CHECK_COUNT! GEQ 10 (
            call :CHECK_BACKEND_HEALTH
            if !ERRORLEVEL! NEQ 0 (
                echo [%date% %time%] Backend not responding, restarting... >> "%WATCHDOG_LOG%"
                echo [%date% %time%] [health check failed] No response on port %BACKEND_PORT%, restarting >> "%HEALTH_LOG%"
                goto :RESTART_BACKEND
            )
            set "HEALTH_CHECK_COUNT=0"
        )

        REM Check whether the process is still alive
        for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%BACKEND_PORT%.*LISTENING"') do (
            set "PID_CHECK=%%p"
        )
        if not defined PID_CHECK (
            echo [%date% %time%] Backend process disappeared, restarting... >> "%WATCHDOG_LOG%"
            goto :RESTART_BACKEND
        )
        set "PID_CHECK="
    goto :HEALTH_LOOP

:START_BACKEND
    echo [%date% %time%] Starting backend service (port %BACKEND_PORT%)... >> "%WATCHDOG_LOG%"
    echo [%date% %time%] Starting backend service...

    cd /d "%BACKEND_DIR%"

    REM Trim old logs (keep the last 1000 lines)
    if exist "%BACKEND_STDOUT%" (
        powershell -Command "Get-Content '%BACKEND_STDOUT%' | Select-Object -Last 1000 | Out-File '%BACKEND_STDOUT%' -Encoding UTF8" 2>nul
    )
    if exist "%BACKEND_STDERR%" (
        powershell -Command "Get-Content '%BACKEND_STDERR%' | Select-Object -Last 1000 | Out-File '%BACKEND_STDERR%' -Encoding UTF8" 2>nul
    )

    REM Start the backend (start /b avoids a new window); output goes to log files
    start /b /wait %PYTHON_EXEC% -m uvicorn app.main:app --host 127.0.0.1 --port %BACKEND_PORT% --log-level info --no-access-log 1>"%BACKEND_STDOUT%" 2>"%BACKEND_STDERR%"
    set "EXIT_CODE=%ERRORLEVEL%"

    timeout /t 1 /nobreak >nul

    if %EXIT_CODE% NEQ 0 (
        call :LOG_CRASH %EXIT_CODE%
        call :INC_RESTARTS
        if !TOTAL_RESTARTS! GTR %MAX_RESTARTS_PER_HOUR% (
            echo [%date% %time%] Warning: restart limit reached (%MAX_RESTARTS_PER_HOUR% per hour), pausing auto-restart >> "%WATCHDOG_LOG%"
            echo [Warning] Restart limit reached, check %CRASH_LOG%
            timeout /t 60 /nobreak >nul
            set "TOTAL_RESTARTS=0"
        )
        echo [%date% %time%] Restarting in %RESTART_DELAY% seconds... >> "%WATCHDOG_LOG%"
        echo [Restart] Restarting in %RESTART_DELAY% seconds...
        timeout /t %RESTART_DELAY% /nobreak >nul
    ) else (
        echo [%date% %time%] Backend exited normally (exit code: %EXIT_CODE%) >> "%WATCHDOG_LOG%"
    )

    goto WATCHDOG_LOOP

:RESTART_BACKEND
    REM Force-kill the old process
    for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%BACKEND_PORT%.*LISTENING"') do (
        taskkill /F /PID %%p >nul 2>&1
    )
    timeout /t 2 /nobreak >nul

    goto START_BACKEND

:WAIT_BACKEND_READY
    set "WAIT_COUNT=0"
    set "WAIT_MAX=30"
    :WAIT_LOOP
        for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%BACKEND_PORT%.*LISTENING"') do (
            set "FOUND_PID=%%p"
        )
        if defined FOUND_PID (
            REM Check the health endpoint
            %PYTHON_EXEC% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%BACKEND_PORT%/api/v1/health', timeout=3)" 2>nul
            if !ERRORLEVEL! EQU 0 (
                exit /b 0
            )
        )
        set "FOUND_PID="
        timeout /t 1 /nobreak >nul
        set /a WAIT_COUNT+=1
        if !WAIT_COUNT! LSS !WAIT_MAX! goto :WAIT_LOOP
        exit /b 1

:CHECK_BACKEND_HEALTH
    %PYTHON_EXEC% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%BACKEND_PORT%/api/v1/health', timeout=5)" 2>nul
    exit /b !ERRORLEVEL!

:LOG_CRASH
    echo [%date% %time%] *** Backend crash detected! Exit code: %1 *** >> "%CRASH_LOG%"
    echo [%date% %time%] --- Crash details --- >> "%CRASH_LOG%"

    if exist "%BACKEND_STDERR%" (
        echo [stderr last 30 lines:] >> "%CRASH_LOG%"
        powershell -Command "Get-Content '%BACKEND_STDERR%' -Tail 30" >> "%CRASH_LOG%" 2>nul
        echo. >> "%CRASH_LOG%"
    )
    if exist "%BACKEND_STDOUT%" (
        echo [stdout last 30 lines:] >> "%CRASH_LOG%"
        powershell -Command "Get-Content '%BACKEND_STDOUT%' -Tail 30" >> "%CRASH_LOG%" 2>nul
        echo. >> "%CRASH_LOG%"
    )
    echo [exit code: %1] >> "%CRASH_LOG%"
    echo. >> "%CRASH_LOG%"
    exit /b

:INC_RESTARTS
    set /a TOTAL_RESTARTS+=1
    echo [%date% %time%] Restart count: !TOTAL_RESTARTS! >> "%WATCHDOG_LOG%"
    exit /b
