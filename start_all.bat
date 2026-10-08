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
set "BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"
set "SERVE_SCRIPT=%ROOT_DIR%scripts\serve_frontend.py"
set "FRONTEND_DIST=%ROOT_DIR%frontend\dist"

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

REM 解析 python 绝对路径（供后续 start 命令安全引用，避免路径含引号/空格）
set "PYTHON_BIN="
for /f "delims=" %%p in ('where python 2^>nul') do (
    if not defined PYTHON_BIN set "PYTHON_BIN=%%p"
)
if not defined PYTHON_BIN set "PYTHON_BIN=python"

set "NODE_DIR="
set "USE_BUILD=1"
where node >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "USE_BUILD=0"
    echo [OK] Node.js in PATH
) else (
    echo [Warn] Node.js not in PATH, probing known install locations...
    call :FIND_NODE
    if defined NODE_DIR (
        set "USE_BUILD=0"
        echo [OK] Node.js found: !NODE_DIR!
    )
)
if "%USE_BUILD%"=="1" (
    echo [Warn] Node.js not found, will use pre-built frontend
    echo        源码改动需重新执行 npm run build 才会生效
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
    echo [Info] Skipping frontend dependencies, using pre-built files
)

REM 开发模式必须有 node_modules；缺失说明依赖没装上，直接回退静态服务，避免卡在 npm run dev
if "%USE_BUILD%"=="0" (
    if not exist "%ROOT_DIR%frontend\node_modules" (
        echo [Warn] frontend\node_modules not found, falling back to pre-built frontend
        set "USE_BUILD=1"
    )
)
echo.

REM ==================== Step 4: Start backend (with auto-restart) ====================
echo [4/5] Starting backend service (port %BACKEND_PORT%)...
cd /d "%ROOT_DIR%"

REM Start the backend watchdog in a separate window (v2.0 enhanced)
start "BidAssistant-Backend-Watchdog" cmd /c "backend_autostart.bat"

echo   Waiting for backend to initialize...
 set "retry_count=0"
 set "max_retries=60"
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
    echo [Info] Starting static file server with API proxy...
    call :START_STATIC_FRONTEND
)

set "FE_READY=0"
call :WAIT_FRONTEND
if "!FE_READY!"=="0" (
    if "%USE_BUILD%"=="0" (
        echo [Warn] Dev server not ready, falling back to static files: frontend\dist
        call :START_STATIC_FRONTEND
        call :WAIT_FRONTEND
    )
)

if "!FE_READY!"=="0" (
    echo [Error] Frontend failed to start on port %FRONTEND_PORT%
    echo         Check whether frontend\dist exists and no other program occupies the port.
    pause
    exit /b 1
)
echo [OK] Frontend service ready
echo.

REM ==================== Done ====================
echo ============================================================
echo   Platform started successfully!
echo.
echo   Frontend: %FRONTEND_URL%/
echo   Backend:  %BACKEND_URL%/docs
echo   Crash log: %LOG_DIR%\backend_crash_log.txt
echo   Watchdog log: %LOG_DIR%\watchdog.log
echo.
echo   Tip: backend crashes restart automatically
echo   Tip: closing this window will NOT stop the services
echo ============================================================
echo.

start "" "%FRONTEND_URL%/"
pause
exit /b 0

REM ==================== Subroutines ====================

REM ---- 在常见安装位置中查找 Node.js（PATH 里没有时也能用起来）----
:FIND_NODE
set "NODE_DIR="
for %%d in (
    "%ProgramFiles%\nodejs"
    "%ProgramFiles(x86)%\nodejs"
    "%LOCALAPPDATA%\Programs\nodejs"
    "%ProgramFiles%\Microsoft Visual Studio\18\Community\MSBuild\Microsoft\VisualStudio\NodeJs"
    "%ProgramFiles(x86)%\Microsoft Visual Studio\18\Community\MSBuild\Microsoft\VisualStudio\NodeJs"
    "%APPDATA%\TRAE SOLO CN\ModularData\ai-agent\vm\tools\node"
) do (
    if not defined NODE_DIR (
        if exist "%%~d\node.exe" set "NODE_DIR=%%~d"
    )
)
if defined NODE_DIR set "PATH=%NODE_DIR%;%PATH%"
exit /b 0

REM ---- 启动静态前端服务（含 /api 反向代理）----
REM 注意：cmd 下 \" 会被当作转义引号，导致 python 把整串当成一个路径参数，
REM 因此这里用「外层再包一层引号」的写法，保证每个参数被正确识别。
:START_STATIC_FRONTEND
if not exist "%FRONTEND_DIST%\index.html" (
    echo [Error] 未找到前端构建产物: %FRONTEND_DIST%\index.html
    exit /b 1
)
start "BidAssistant-Frontend-Static" cmd /c ""%PYTHON_BIN%" "%SERVE_SCRIPT%" "%FRONTEND_DIST%" %FRONTEND_PORT% %BACKEND_URL%"
exit /b 0

REM ---- 轮询等待前端端口可访问（最多 30 秒）----
:WAIT_FRONTEND
set "FE_READY=0"
for /l %%i in (1,1,30) do (
    timeout /t 1 /nobreak >nul
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:%FRONTEND_PORT%/', timeout=2)" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        set "FE_READY=1"
        goto :FE_READY_DONE
    )
)
:FE_READY_DONE
exit /b 0
