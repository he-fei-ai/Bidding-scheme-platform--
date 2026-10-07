@echo off
REM 招投标方案平台 - 完整启动脚本
REM 
REM 使用方法:
REM   1. 确保已安装 Redis (运行 start_redis.bat)
REM   2. 运行此脚本启动 Celery Worker
REM   3. Worker 将自动处理异步任务

echo ========================================
echo   招投标方案平台 - 启动 Celery Worker
echo ========================================
echo.

REM 检查 Python 环境
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请安装 Python 3.8+
    pause
    exit /b 1
)

echo [信息] Python 版本检查通过
echo.

REM 检查 Redis 连接
echo [信息] 检查 Redis 连接...
where redis-cli >nul 2>&1
if errorlevel 1 (
    echo [警告] 未找到 redis-cli，跳过连接检查
    echo [提示] 如需使用异步任务功能，请先安装并启动 Redis
) else (
    redis-cli ping >nul 2>&1
    if errorlevel 1 (
        echo [警告] Redis 未响应，请确保 Redis 正在运行
        echo [提示] 运行 start_redis.bat 启动 Redis
        echo.
    ) else (
        echo [信息] Redis 连接正常
    )
)
echo.

REM 激活虚拟环境（如果有）
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo [信息] 虚拟环境已激活
)
echo.

echo [信息] 正在启动 Celery Worker...
echo [信息] Broker: redis://localhost:6379/1
echo [信息] Backend: redis://localhost:6379/2
echo.
echo [提示] 按 Ctrl+C 停止 Worker
echo.

REM 启动 Celery Worker
celery -A app.celery_app:celery_app worker ^
    --loglevel=info ^
    --concurrency=4 ^
    --pool=solo ^
    --hostname=worker@%%COMPUTERNAME%% ^
    --without-heartbeat ^
    --without-gossip ^
    --without-mingle ^
    --optimization=fair

pause
