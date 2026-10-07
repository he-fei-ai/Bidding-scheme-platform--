@echo off
title Bid Assistant Platform v2.0
setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "DATA_DIR=%ROOT_DIR%data"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5175"
set "BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

REM ============================================================
REM  Sync port config to backend-ports.json (read by the Vite plugin)
REM ============================================================
python "%ROOT_DIR%scripts\sync_backend_ports.py" %BACKEND_PORT% %FRONTEND_PORT%

echo ========================================
echo   Bid Assistant Platform v2.0
echo ========================================
echo.

rem ==================== Step 1: Clean old processes ====================
echo [1/6] Cleaning old processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%BACKEND_PORT%.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
    echo   Killed old backend process (PID: %%a)
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%FRONTEND_PORT%.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
    echo   Killed old frontend process (PID: %%a)
)
timeout /t 1 /nobreak >nul
echo [OK] Old processes cleaned
echo.

rem ==================== Step 2: Check environment ====================
echo [2/6] Checking runtime environment...

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Error] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)
python --version 2>&1 | findstr "3." >nul
if %ERRORLEVEL% NEQ 0 (
    echo [Error] Please use Python 3.10 or higher
    pause
    exit /b 1
)
echo [OK] Python version OK

where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Warn] Node.js not found, will use pre-built frontend
    set "USE_BUILD=1"
) else (
    echo [OK] Node.js version OK
    set "USE_BUILD=0"
)
echo.

rem ==================== Step 3: Install dependencies ====================
echo [3/6] Installing dependencies...

echo   Installing backend dependencies...
cd /d "%ROOT_DIR%backend"
pip install -r requirements.txt -q
if %ERRORLEVEL% NEQ 0 (
    echo   Default source failed, trying mirror...
    pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [OK] Backend dependencies ready

if "%USE_BUILD%"=="0" (
    echo   Installing frontend dependencies...
    cd /d "%ROOT_DIR%frontend"
    if not exist "node_modules" (
        call npm install --silent 2>nul
        if !ERRORLEVEL! NEQ 0 (
            call npx pnpm install --silent 2>nul
        )
    )
    echo [OK] Frontend dependencies ready
) else (
    echo [Info] Skipping frontend dependencies (using pre-built files)
)
echo.

rem ==================== Step 4: Start backend ====================
echo [4/6] Starting backend service (port %BACKEND_PORT%)...
cd /d "%ROOT_DIR%"
set "DATA_DIR=%DATA_DIR%"
rem Use enhanced watchdog for auto-restart (v2.0)
start "BidAssistant-Backend-Watchdog" cmd /c "backend_autostart.bat"

echo   Waiting for backend to initialize...
set "retry_count=0"
set "max_retries=15"
:wait_backend
timeout /t 1 /nobreak >nul
set /a retry_count+=1
python -c "import urllib.request; urllib.request.urlopen('%BACKEND_URL%/api/v1/health', timeout=2)" 2>nul
if %ERRORLEVEL% NEQ 0 (
    if !retry_count! lss !max_retries! (
        goto wait_backend
    ) else (
        echo [Error] Backend startup timeout, please check logs
        pause
        exit /b 1
    )
)
echo [OK] Backend service ready
echo.

rem ==================== Step 5: Start frontend ====================
echo [5/6] Starting frontend service (port %FRONTEND_PORT%)...
cd /d "%ROOT_DIR%frontend"

if "%USE_BUILD%"=="0" (
    start "BidAssistant-Frontend" cmd /c "npx vite --host 127.0.0.1 --port %FRONTEND_PORT%"
) else (
    echo [Info] Starting static file server (with API proxy)...
    start "BidAssistant-Frontend-Static" cmd /c "python \"%ROOT_DIR%scripts\serve_frontend.py\" \"%ROOT_DIR%frontend\dist\" %FRONTEND_PORT% %BACKEND_URL%"
)

echo   Waiting for frontend to initialize...
timeout /t 3 /nobreak >nul
echo [OK] Frontend service started
echo.

rem ==================== Step 6: Open browser ====================
echo [6/6] Opening browser...
start "" "%FRONTEND_URL%"

echo.
echo ========================================
echo   Platform started successfully!
echo   Frontend: %FRONTEND_URL%
echo   Backend:  %BACKEND_URL%/docs (API docs)
echo   Data:     %DATA_DIR%
echo ========================================
echo.
echo Tip: Closing this window will NOT stop the services.
echo       To stop, close the backend and frontend terminal windows.
echo.
pause