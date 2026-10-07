@echo off
REM Celery Worker 启动脚本 (Windows)
REM 用法: start_celery.bat [worker^|beat^|flower]

cd /d "%~dp0"

set CELERY_APP=app.celery_app:celery_app

if "%1"=="worker" (
    echo 启动 Celery Worker...
    celery -A %CELERY_APP% worker --loglevel=info --pool=solo --concurrency=4
) else if "%1"=="beat" (
    echo 启动 Celery Beat (定时任务)...
    celery -A %CELERY_APP% beat --loglevel=info
) else if "%1"=="flower" (
    echo 启动 Flower (监控面板)...
    celery -A %CELERY_APP% flower --port=5555
) else (
    echo 用法:
    echo   start_celery.bat worker     - 启动工作进程
    echo   start_celery.bat beat       - 启动定时任务调度器
    echo   start_celery.bat flower     - 启动监控面板
    echo.
    echo 默认启动 worker
    echo.
    celery -A %CELERY_APP% worker --loglevel=info --pool=solo --concurrency=4
)
