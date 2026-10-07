@echo off
REM 招投标方案平台 - 一键启动所有服务
REM 
REM 使用方法:
REM   运行此脚本将自动启动:
REM   1. Redis 服务器（如果未运行）
REM   2. Celery Worker（异步任务处理）
REM   3. Celery Beat（定时任务调度）

echo ========================================
echo   招投标方案平台 - 一键启动
echo ========================================
echo.

REM 步骤 1: 启动 Redis
echo [步骤 1/3] 检查 Redis 服务...
where redis-server >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Redis，请先安装:
    echo   winget install Redis.Redis
    echo   或 choco install redis
    echo.
    pause
    exit /b 1
)

redis-cli ping >nul 2>&1
if errorlevel 1 (
    echo [信息] Redis 未运行，正在启动...
    start "Redis Server" redis-server --port 6379 --daemonize yes
    timeout /t 2 /nobreak >nul
    echo [信息] Redis 已启动
) else (
    echo [信息] Redis 已在运行
)
echo.

REM 步骤 2: 启动 Celery Beat（定时任务）
echo [步骤 2/3] 启动 Celery Beat (定时任务调度)...
start "Celery Beat" cmd /c "cd /d %~dp0 && start_beat.bat"
timeout /t 1 /nobreak >nul
echo [信息] Celery Beat 已启动
echo.

REM 步骤 3: 启动 Celery Worker
echo [步骤 3/3] 启动 Celery Worker (异步任务处理)...
start "Celery Worker" cmd /c "cd /d %~dp0 && start_worker.bat"
timeout /t 1 /nobreak >nul
echo [信息] Celery Worker 已启动
echo.

echo ========================================
echo   所有服务已启动
echo ========================================
echo.
echo 服务状态检查:
echo   Redis:     redis-cli ping
echo   Worker:    查看 "Celery Worker" 窗口
echo   Beat:      查看 "Celery Beat" 窗口
echo.
echo Flower 监控面板（可选）:
echo   运行: start_celery.bat flower
echo   访问: http://localhost:5555
echo.
pause
