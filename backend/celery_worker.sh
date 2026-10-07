#!/bin/bash
# Celery Worker 启动脚本 (Linux/Mac)
#
# 使用方法:
#   1. 确保 Redis 正在运行 (默认 localhost:6379)
#   2. 运行此脚本启动 Celery Worker
#   3. Worker 将自动处理异步任务

echo "========================================"
echo "  招投标方案平台 - Celery Worker"
echo "========================================"
echo ""

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python3，请安装 Python 3.8+"
    exit 1
fi

echo "[信息] Python 版本检查通过"
echo "[信息] 正在启动 Celery Worker..."
echo "[信息] Broker: redis://localhost:6379/0"
echo "[信息] Backend: db+sqlite:///data/celery_results.sqlite"
echo ""

# 激活虚拟环境（如果有）
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# 启动 Celery Worker
celery -A app.tasks.celery_app worker \
    --loglevel=info \
    --concurrency=4 \
    --pool=prefork \
    --hostname=worker@$(hostname) \
    --without-heartbeat \
    --without-gossip \
    --without-mingle \
    --optimization=fair
