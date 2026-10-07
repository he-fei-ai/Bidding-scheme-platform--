@echo off
REM Redis 服务器启动脚本 (Windows)
REM 
REM 使用方法:
REM   1. 确保已安装 Redis (winget install Redis.Redis 或 choco install redis)
REM   2. 运行此脚本启动 Redis 服务
REM   3. Redis 将在默认端口 6379 上运行

echo ========================================
echo   招投标方案平台 - Redis 服务器
echo ========================================
echo.

where redis-server >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 redis-server
    echo.
    echo 请先安装 Redis:
    echo   方式 1 (推荐): winget install Redis.Redis
    echo   方式 2:        choco install redis
    echo   方式 3:        手动下载 https://github.com/microsoftarchive/redis/releases
    echo   方式 4:        使用 WSL2: sudo apt-get install -y redis-server
    echo.
    pause
    exit /b 1
)

echo [信息] Redis 已安装，正在启动...
echo [信息] 端口: 6379
echo.

REM 尝试以后台服务方式启动
start "Redis Server" redis-server --port 6379 --daemonize yes

timeout /t 2 /nobreak >nul

echo [信息] Redis 服务已启动
echo [信息] 使用以下命令测试连接:
echo   redis-cli ping
echo.
echo Redis 数据库分配:
echo   DB 0: 应用缓存
echo   DB 1: Celery Broker (消息队列)
echo   DB 2: Celery Backend (结果存储)
echo.
pause
