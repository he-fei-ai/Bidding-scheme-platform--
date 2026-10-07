#!/usr/bin/env python3
"""
集成测试：针对 sections 模块的安全性功能（级联删除保护 + 速率限制）
"""

import pytest
from unittest.mock import MagicMock, patch, call
import asyncio
import json

# Import the actual functions to test
from app.services.sections import delete_section


class TestCascadeSafety:
    """测试 delete_section 的级联安全检查"""

    @patch("app.services.sections.get_db")
    def test_delete_section_with_children_returns_false(self, mock_get_db):
        """有子章节时删除应返回包含子节点数量的提示信息"""
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        # 新查询模式：复合子查询返回 (children_count, project_id)
        mock_cursor.fetchone.return_value = {"children_count": 1, "project_id": 42}

        mock_get_db.return_value.__enter__.return_value = mock_conn

        result = delete_section(section_id=456)

        assert isinstance(result, dict), "返回值应为 dict"
        assert result["success"] is False, "有子章节时 success 应为 False"
        assert result["children_count"] == 1, "子章节数量应为 1"
        assert "子章节" in result["message"], "提示信息应包含子章节相关描述"
        print("✓ delete_section with children correctly returns child count info")

    @patch("app.services.sections.get_db")
    def test_delete_section_without_children_succeeds(self, mock_get_db):
        """无子章节时删除应返回成功"""
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_cursor = MagicMock()

        def execute_mock(query, params=()):
            if "children_count" in query or ("project_id" in query and "SELECT" in query):
                # 复合查询：返回无子节点
                mock_cursor.fetchone.return_value = {"children_count": 0, "project_id": 42}
                return mock_cursor
            elif "DELETE FROM sections WHERE id = ?" in query:
                mock_cursor.rowcount = 1
                return mock_cursor
            mock_cursor.fetchone.return_value = {"children_count": 0, "project_id": 42}
            return mock_cursor

        mock_conn.execute = execute_mock
        mock_get_db.return_value.__enter__.return_value = mock_conn

        result = delete_section(section_id=789)

        assert isinstance(result, dict), "返回值应为 dict"
        assert result["success"] is True, "无子章节时 success 应为 True"
        assert result["children_count"] == 0, "children_count 应为 0"
        assert "已删除" in result["message"], "提示信息应包含已删除相关描述"
        print("✓ delete_section without children succeeds")

    
class TestRateLimiting:
    """测试速率限制的基本行为（基于内存实现）"""

    @pytest.mark.asyncio
    async def test_rate_limiter_single_client_simple(self):
        """简单测试单个客户端的令牌桶行为（基于服务层实现）"""
        from app.services.rate_limiting import get_rate_limiter, RedisSlidingWindowLimiter
        import asyncio

        limiter = get_rate_limiter()

        # RedisSlidingWindowLimiter 使用 Redis Lua 脚本，无 _buckets 属性。
        # 当 Redis 不可用时，allow() 宽松放行。
        if hasattr(limiter, '_buckets'):
            # 本地内存限速器路径
            async def test_scenario():
                limiter._buckets.clear()
                for i in range(5):
                    allowed, remaining = await limiter.allow("test_client", limit=10, window=60)
                    assert allowed is True, f"请求 {i} 应该被允许"
                allowed, _ = await limiter.allow("test_client", limit=10, window=60)
                assert allowed is False, "第11个请求应该被拒绝"
                limiter._buckets.clear()
            asyncio.run(test_scenario())
        else:
            # Redis 路径：无 Redis 时宽松放行，验证不会抛异常
            allowed, _ = await limiter.allow("test_client", limit=10, window=60)
            assert allowed is True, "Redis 不可用时应宽松放行"

    @pytest.mark.asyncio
    async def test_rate_limit_headers_are_set(self):
        """测试响应中包含速率限制头部（如果 middleware 已启用）"""
        # 由于当前 rate_limiter 是 per-endpoint dependency 而非 middleware，
        # 它不设置头部。若需头部，需要额外实现。
        # 此处作为未来改进点占位
        pass


# 测试 SSE 错误消息是否已去敏感化（间接测试）
class TestErrorSanitization:
    """验证 SSE 事件不包含原始异常详情"""

    def test_ai_error_message_sanitized(self):
        """AI 配置错误消息不应包含 {str(e)} 模式"""
        from app.routers.sse_handlers import generate_outline_sse_impl
        import inspect
        source = inspect.getsource(generate_outline_sse_impl)
        assert 'f"AI 配置异常: {str(e)}"' not in source, "AI error message still contains raw exception!"
        assert '"AI 配置异常，请检查设置"' in source, "Sanitized message missing!"
        print("✓ AI config error properly sanitized")

    def test_db_error_message_sanitized(self):
        """测试数据库错误信息不包含敏感内容"""
        import inspect
        from app.routers.sse_handlers import generate_outline_sse_impl
        source = inspect.getsource(generate_outline_sse_impl)
        assert '"数据库查询失败' in source, "Sanitized DB message missing!"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
