@echo off
REM 招投标方案平台 - Windows 一键部署脚本
REM 功能：运行本地测试 → Docker 构建部署 → 性能分析

setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0.."
set "BACKEND_DIR=%PROJECT_DIR%\backend"
set "COMPOSE_FILE=%PROJECT_DIR%\docker-compose.yml"

echo ========================================
echo   招投标方案平台 - 自动化部署与测试
echo ========================================
echo.

REM 步骤 1: 本地测试
echo [1/4] 运行本地回归测试...
cd /d "%BACKEND_DIR%"
python -m pytest tests/test_chart_pipeline.py -v --tb=short -q
if errorlevel 1 (
    echo [ERROR] 图表管线测试失败，部署中止
    exit /b 1
)
echo.

REM 步骤 2: Docker 构建与部署
echo [2/4] Docker 构建与部署...
cd /d "%PROJECT_DIR%"
docker compose -f "%COMPOSE_FILE%" build --no-cache 2>nul
if errorlevel 1 (
    echo [WARN] 完整构建失败，尝试增量构建...
    docker compose -f "%COMPOSE_FILE%" build
)

docker compose -f "%COMPOSE_FILE%" down 2>nul
docker compose -f "%COMPOSE_FILE%" up -d

echo [3/4] 运行性能分析...
cd /d "%BACKEND_DIR%"
python scripts\regression_test.py

echo.
echo ========================================
echo   部署与测试完成
echo ========================================
