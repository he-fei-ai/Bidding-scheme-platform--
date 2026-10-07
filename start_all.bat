@echo off
REM ============================================================
REM  Bid Assistant Platform - Unified launcher
REM  Starts backend and frontend services together.
REM  The backend watchdog auto-restarts it after a crash.
REM ============================================================

title BidAssistant Platform

setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "DATA_DIR=%ROOT_DIR%data"
set "LOG_DIR=%ROOT_DIR%logs"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5175"

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ============================================================
REM  Sync shared port config (read by the Vite plugin, single source)
REM ============================================================
python "%ROOT_DIR%scripts\sync_backend_ports.py" %BACKEND_PORT% %FRONTEND_PORT%

echo ============================================================
echo   Bid Assistant Platform v2.0
echo   Started at: %date% %time%
echo ============================================================
echo.

REM ==================== Step 1: Clean old processes ====================
echo [1/5] Cleaning old processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%BACKEND_PORT%.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%FRONTEND_PORT%.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul
echo [OK] Old processes cleaned
echo.

REM ==================== Step 2: Check environment ====================
echo [2/5] Checking runtime environment...

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Error] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%v"
echo [OK] Python: %PYTHON_VERSION%
python --version 2>&1 | findstr "3." >nul
if %ERRORLEVEL% NEQ 0 (
    echo [Error] Please use Python 3.10 or higher
    pause
    exit /b 1
)

where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Warn] Node.js not found, will use pre-built frontend
    set "USE_BUILD=1"
) else (
    echo [OK] Node.js installed
    set "USE_BUILD=0"
)
echo.

REM ==================== Step 3: Install dependencies ====================
echo [3/5] Checking dependencies...

echo   Checking backend dependencies...
cd /d "%ROOT_DIR%backend"
pip show fastapi >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   Installing backend dependencies...
    pip install -r requirements.txt -q
)
echo [OK] Backend dependencies ready

if "%USE_BUILD%"=="0" (
    echo   Checking frontend dependencies...
    cd /d "%ROOT_DIR%frontend"
    if not exist "node_modules" (
        echo   Installing frontend dependencies...
        if exist "pnpm-lock.yaml" (
            pnpm install --silent 2>nul
        ) else (
            npm install --silent 2>nul
        )
    )
    echo [OK] Frontend dependencies ready
) else (
    echo [Info] Skipping frontend dependencies (using pre-built files)
)
echo.

REM ==================== Step 4: Start backend (with auto-restart) ====================
echo [4/5] Starting backend service (port %BACKEND_PORT%)...
cd /d "%ROOT_DIR%"

REM Start the backend watchdog in a separate window (v2.0 enhanced)
start "BidAssistant-Backend-Watchdog" cmd /c "backend_autostart.bat"

echo   Waiting for backend to initialize...
set "retry_count=0"
set "max_retries=20"
:wait_backend
timeout /t 1 /nobreak >nul
set /a retry_count+=1
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%BACKEND_PORT%/api/v1/health', timeout=2)" 2>nul
if %ERRORLEVEL% NEQ 0 (
    if !retry_count! lss !max_retries! (
        goto wait_backend
    ) else (
        echo [Error] Backend startup timeout, check %LOG_DIR%\watchdog.log
        pause
        exit /b 1
    )
)
echo [OK] Backend service ready
echo.

REM ==================== Step 5: Start frontend ====================
echo [5/5] Starting frontend service (port %FRONTEND_PORT%)...
cd /d "%ROOT_DIR%frontend"

if "%USE_BUILD%"=="0" (
    start "BidAssistant-Frontend" cmd /c "npm run dev"
) else (
    echo [Info] Starting static file server (with API proxy)...
    start "BidAssistant-Frontend-Static" cmd /c "python \"%ROOT_DIR%scripts\serve_frontend.py\" \"%ROOT_DIR%frontend\dist\" %FRONTEND_PORT% %BACKEND_URL%"
)

echo   Waiting for frontend to initialize...
timeout /t 3 /nobreak >nul
echo [OK] Frontend service started
echo.

REM ==================== Done ====================
echo ============================================================
echo   Platform started successfully!
echo.
echo   Frontend: http://localhost:%FRONTEND_PORT%/
echo   Backend:  http://localhost:%BACKEND_PORT%/docs
echo   Crash log: %LOG_DIR%\backend_crash_log.txt
echo   Watchdog log: %LOG_DIR%\watchdog.log
echo.
echo   Tip: backend crashes restart automatically
echo   Tip: closing this window will NOT stop the services
echo ============================================================
echo.

start "" "http://localhost:%FRONTEND_PORT%/"
pause
