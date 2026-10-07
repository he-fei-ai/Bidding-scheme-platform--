@echo off
REM Celery Worker 启动脚本 (Windows)
REM 
REM 使用方法:
REM   1. 确保 Redis 正在运行 (默认 localhost:6379)
REM   2. 运行此脚本启动 Celery Worker
REM   3. Worker 将自动处理异步任务

echo ========================================
echo   招投标方案平台 - Celery Worker
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
echo [信息] 正在启动 Celery Worker...
echo [信息] Broker: redis://localhost:6379/0
echo [信息] Backend: sqlite:///data/celery_results.sqlite
echo.

REM 激活虚拟环境（如果有）
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM 启动 Celery Worker
celery -A app.tasks.celery_app worker ^
    --loglevel=info ^
    --concurrency=4 ^
    --pool=solo ^
    --hostname=worker@%%COMPUTERNAME%% ^
    --without-heartbeat ^
    --without-gossip ^
    --without-mingle ^
    --optimization=fair

pause
