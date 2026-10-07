@echo off
REM Celery Beat 定时任务调度器启动脚本
REM 
REM 使用方法:
REM   1. 确保 Redis 正在运行
REM   2. 运行此脚本启动定时任务调度器
REM   3. 常用命令:
REM      start_beat.bat       - 启动调度器
REM      start_worker.bat     - 启动工作进程
REM      start_all.bat        - 同时启动所有服务

echo ========================================
echo   招投标方案平台 - Celery Beat
echo ========================================
echo.

REM 检查 Python 环境
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请安装 Python 3.8+
    pause
    exit /b 1
)

echo [信息] 正在启动 Celery Beat...
echo [信息] 定时任务:
echo   - 每 1 小时清理旧的任务结果
echo.
echo [提示] 按 Ctrl+C 停止调度器
echo.

REM 激活虚拟环境（如果有）
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM 启动 Celery Beat
celery -A app.celery_app:celery_app beat --loglevel=info

pause
