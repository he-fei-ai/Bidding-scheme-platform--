# 后端服务 Dockerfile
# 基于Python 3.12 Alpine 轻量级镜像

FROM python:3.12-alpine

WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

# 安装系统依赖（含 curl 用于健康检查）
RUN apk update && apk add --no-cache \
    gcc \
    musl-dev \
    redis-dev \
    curl \
    && rm -rf /var/cache/apk/*

# 复制依赖文件
COPY backend/requirements.txt .
COPY backend/requirements-dev.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-dev.txt

# 复制后端代码
COPY backend/ .

# 暴露端口
EXPOSE ${PORT}

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}"]
