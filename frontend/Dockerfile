# 前端服务 Dockerfile
# 使用Nginx静态文件服务器

# 构建阶段 - 编译前端
FROM node:20-alpine AS builder

WORKDIR /frontend

# 复制前端文件
COPY frontend/package*.json ./

# 安装构建依赖
RUN npm ci --prefer-offline || pnpm install

# 复制所有前端代码
COPY frontend/ .

# 构建生产版本
RUN npm run build

# 最终阶段 - 使用Nginx Serve
FROM nginx:1.25-alpine

# 移除默认配置文件
RUN rm -f /etc/nginx/default.conf

# 复制构建产物
COPY --from=builder /frontend/frontend/dist /usr/share/nginx/html

# 复制Nginx配置
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# 暴露端口
EXPOSE 80

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost/index.html || exit 1

# 启动Nginx
CMD ["nginx", "-g", "daemon off;"]
