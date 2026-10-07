"""健康检查单元测试"""
import pytest
import asyncio
import time
import sys
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

# 添加backend目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))

from app.health_check import (
    HealthCheckManager,
    DatabaseHealthChecker,
    RedisHealthChecker,
    ServiceStatus,
    HealthCheckResult
)


class TestDatabaseHealthChecker:
    """数据库健康检查测试"""
    
    @pytest.mark.asyncio
    async def test_check_healthy_database(self, tmp_path):
        """测试正常数据库检查"""
        # 创建临时数据库文件
        db_file = tmp_path / "test.db"
        db_file.touch()
        
        checker = DatabaseHealthChecker(str(db_file))
        result = await checker.check()
        
        assert result.status == ServiceStatus.HEALTHY
        assert result.service_name == "database"
        assert result.response_time_ms > 0
    
    @pytest.mark.asyncio
    async def test_check_missing_database(self, tmp_path):
        """测试缺失数据库检查"""
        checker = DatabaseHealthChecker(str(tmp_path / "nonexistent.db"))
        result = await checker.check()
        
        assert result.status == ServiceStatus.STOPPED
        assert "not found" in result.message.lower()


class TestRedisHealthChecker:
    """Redis健康检查测试"""
    
    @pytest.mark.asyncio
    async def test_check_redis_connection_failed(self):
        """测试Redis连接失败"""
        checker = RedisHealthChecker(host='localhost', port=59999)
        result = await checker.check()
        
        # 由于端口59999不可用，应该返回stopped或unknown
        assert result.status in [ServiceStatus.STOPPED, ServiceStatus.UNKNOWN]
    
    @pytest.mark.asyncio
    async def test_check_without_password_param(self):
        """测试不使用password参数"""
        checker = RedisHealthChecker(host='localhost', port=6379)
        # 这个测试取决于Redis是否运行
        result = await checker.check()
        # 只验证不会抛出异常
        assert result is not None
        assert result.service_name == "redis"


class TestHealthCheckManager:
    """健康检查管理器测试"""
    
    @pytest.mark.asyncio
    async def test_get_overall_status(self):
        """测试获取整体状态"""
        manager = HealthCheckManager({
            'check_interval': 1,
            'database_path': ':memory:',
            'redis': {'host': 'localhost', 'port': 59999},
            'celery_broker': 'redis://localhost:59999/0',
            'api_base_url': 'http://localhost:9999'
        })
        
        status = await manager.get_overall_status()
        
        assert 'overall_status' in status
        assert 'services' in status
        assert 'timestamp' in status
    
    @pytest.mark.asyncio
    async def test_start_stop_monitoring(self):
        """测试启动和停止监控"""
        manager = HealthCheckManager({'check_interval': 0.1})
        
        await manager.start_monitoring()
        assert manager._is_running
        
        await manager.stop_monitoring()
        assert not manager._is_running


class TestHealthCheckResult:
    """健康检查结果测试"""
    
    def test_to_dict(self):
        """测试转换为字典"""
        result = HealthCheckResult(
            service_name="test",
            status=ServiceStatus.HEALTHY,
            message="All good",
            response_time_ms=123.45,
            metadata={"key": "value"}
        )
        
        d = result.to_dict()
        
        assert d['service'] == "test"
        assert d['status'] == "healthy"
        assert d['message'] == "All good"
        assert d['response_time_ms'] == 123.45
        assert d['metadata'] == {"key": "value"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
