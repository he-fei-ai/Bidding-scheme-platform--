#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# check_v51_only.sh — V5.1 唯一路径 CI 门禁脚本
# 扫描禁用符号，检测 V2/V3 残留，接入 CI + pre-commit
#
# 用法: bash scripts/check_v51_only.sh [backend_dir]
# 通过 exit code 0/1 返回检查结果

set -e

BACKEND_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_DIR="$BACKEND_DIR/app"
ERRORS=0
FOUND=""

echo "=== V5.1 唯一路径门禁检查 ==="
echo "扫描目录: $APP_DIR"
echo ""

# 禁用符号列表
BANNED_PATTERNS=(
    "workflows_outline_v2v3"
    "generate_outline_v3"
    "generate_outline_one_shot"
    "generate_outline_step_by_step"
    "OUTLINE_STEP_SYSTEM"
    "OUTLINE_STEP_USER"
    "OUTLINE_CHILDREN_SYSTEM"
    "OUTLINE_CHILDREN_USER"
    "OUTLINE_CHILDREN_FRAMEWORK_SYSTEM"
    "OUTLINE_CHILDREN_FRAMEWORK_USER"
    "OUTLINE_SUPPLEMENT_SYSTEM"
    "OUTLINE_SUPPLEMENT_USER"
)

for pattern in "${BANNED_PATTERNS[@]}"; do
    FOUND=$(grep -rn --include="*.py" "$APP_DIR" -l "$pattern" 2>/dev/null || true)
    if [ -n "$FOUND" ]; then
        echo "❌ 禁用符号: $pattern"
        echo "   命中文件:"
        echo "$FOUND" | while read -r f; do echo "   - $f"; done
        ERRORS=$((ERRORS + 1))
        echo ""
    fi
done

# 检测 strategy == "v3" 分支判断
STRATEGY_BRANCH=$(grep -rn --include="*.py" "$APP_DIR" -A2 'strategy.*==.*"v3"' 2>/dev/null || true)
if [ -n "$STRATEGY_BRANCH" ]; then
    echo "❌ 检测到 strategy 分支判断 (可能引入 V3 路径)"
    echo "$STRATEGY_BRANCH"
    ERRORS=$((ERRORS + 1))
    echo ""
fi

# 检查 V5.1 是否为唯一入口
SSE_IMPORTS=$(grep -rn --include="*.py" "$APP_DIR" -l "from.*workflows_outline_v51 import\|from.*outline_common import" 2>/dev/null || true)
if [ -z "$SSE_IMPORTS" ]; then
    echo "⚠️  警告: 未找到 V5.1 或 outline_common 的导入 (检查 sse_handlers.py)"
    echo "   可能影响 V5.1 唯一路径机制"
fi

echo ""
echo "=== 检查结果 ==="
if [ $ERRORS -eq 0 ]; then
    echo "✅ 全部通过: V5.1 唯一路径已无残留"
    exit 0
else
    echo "❌ 发现 $ERRORS 项违规，提交被拒绝"
    echo "   请清理上述禁用符号后方可提交"
    exit 1
fi
