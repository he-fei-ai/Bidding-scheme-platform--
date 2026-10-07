@echo off
REM 启动前端开发服务器并自动打开浏览器
REM 
REM 前端地址: http://localhost:5173

echo ========================================
echo   招投标方案平台 - 前端开发服务器
echo ========================================
echo.

cd /d "%~dp0"

REM 检查 Node.js
node --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

echo [信息] Node.js 版本检查通过
echo [信息] 正在启动前端开发服务器...
echo [信息] 端口: 5173
echo [信息] 地址: http://localhost:5173
echo.

REM 启动 Vite 并自动打开浏览器
start "Bid Assistant Frontend" cmd /c "echo 正在打开浏览器... && timeout /t 3 /nobreak >nul && start http://localhost:5173 && vite --host 0.0.0.0 --port 5173"
