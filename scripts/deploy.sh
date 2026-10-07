#!/bin/bash
# 招投标方案平台 - 一键部署脚本
# 功能：构建 Docker 镜像 → 推送 → 部署 → 运行回归测试

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ============================================================
# 步骤 1: 运行本地回归测试
# ============================================================
run_local_tests() {
    log_info "=== 步骤 1: 运行本地回归测试 ==="
    cd "$PROJECT_DIR/backend"

    # 运行图表管线测试
    if ! python -m pytest tests/test_chart_pipeline.py -v --tb=short -q; then
        log_error "图表管线测试失败，部署中止"
    fi

    # 运行全量渲染器测试
    if [ -f "tests/test_all_renderers_v2.py" ]; then
        if ! python -m pytest tests/test_all_renderers_v2.py -v --tb=short -q; then
            log_warn "渲染器 v2 测试有失败项，请检查"
        fi
    fi

    log_info "本地测试完成"
}

# ============================================================
# 步骤 2: Docker 构建与部署
# ============================================================
deploy_docker() {
    log_info "=== 步骤 2: Docker 构建与部署 ==="
    cd "$PROJECT_DIR"

    # 构建镜像
    log_info "构建 Docker 镜像..."
    docker compose -f "$COMPOSE_FILE" build --no-cache || {
        log_warn "完整构建失败，尝试增量构建..."
        docker compose -f "$COMPOSE_FILE" build
    }

    # 停止旧容器
    log_info "停止旧服务..."
    docker compose -f "$COMPOSE_FILE" down || true

    # 启动新服务
    log_info "启动新服务..."
    docker compose -f "$COMPOSE_FILE" up -d

    # 等待健康检查
    log_info "等待服务就绪..."
    for service in backend frontend redis; do
        for i in $(seq 1 12); do
            if docker compose -f "$COMPOSE_FILE" ps | grep -q "$service.*Up"; then
                log_info "  $service 就绪"
                break
            fi
            sleep 5
        done
    done

    log_info "部署完成"
}

# ============================================================
# 步骤 3: 容器内回归测试
# ============================================================
run_container_tests() {
    log_info "=== 步骤 3: 容器内回归测试 ==="
    cd "$PROJECT_DIR/backend"

    docker compose -f "$COMPOSE_FILE" run --rm backend \
        python -m pytest tests/test_chart_pipeline.py -v --tb=short -q || {
        log_warn "容器内测试有失败项"
    }
}

# ============================================================
# 步骤 4: 运行性能分析
# ============================================================
run_performance_test() {
    log_info "=== 步骤 4: 运行性能分析 ==="
    cd "$PROJECT_DIR/backend"
    python scripts/regression_test.py
}

# ============================================================
# 主流程
# ============================================================
main() {
    log_info "========================================"
    log_info "  招投标方案平台 - 自动化部署与测试"
    log_info "========================================"

    run_local_tests
    deploy_docker
    run_container_tests
    run_performance_test

    log_info "========================================"
    log_info "  部署与测试完成"
    log_info "========================================"
}

main "$@"
