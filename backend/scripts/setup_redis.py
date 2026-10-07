"""Redis 安装和配置脚本

自动检测并安装 Redis，配置 Python redis 客户端。
支持 Windows、macOS、Linux。
"""

import os
import platform
import subprocess
import sys
from pathlib import Path


def install_python_package(package: str):
    """安装 Python 包"""
    print(f"正在安装 Python 包: {package}...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"✓ {package} 安装成功")
        return True
    except Exception as e:
        print(f"✗ {package} 安装失败: {e}")
        return False


def check_redis_installed() -> bool:
    """检查 Redis 是否已安装"""
    system = platform.system()
    
    if system == "Windows":
        try:
            result = subprocess.run(
                ["redis-server", "--version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    else:
        try:
            result = subprocess.run(
                ["which", "redis-server"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False


def install_redis_windows():
    """在 Windows 上安装 Redis"""
    print("\n=== Windows Redis 安装选项 ===")
    print("1. 使用 Chocolatey (推荐): choco install redis")
    print("2. 使用 Winget: winget install Redis.Redis")
    print("3. 手动下载: https://github.com/microsoftarchive/redis/releases")
    print("4. 使用 WSL2 运行 Redis")
    print()
    
    choice = input("请选择安装方式 (1-4)，或按 Enter 跳过: ").strip()
    
    if choice == "1":
        try:
            subprocess.check_call(["choco", "install", "redis", "-y"])
            print("✓ Redis 通过 Chocolatey 安装成功")
            return True
        except Exception as e:
            print(f"✗ Chocolatey 安装失败: {e}")
    elif choice == "2":
        try:
            subprocess.check_call(["winget", "install", "--id", "Redis.Redis", "-e"])
            print("✓ Redis 通过 Winget 安装成功")
            return True
        except Exception as e:
            print(f"✗ Winget 安装失败: {e}")
    elif choice == "3":
        print("请手动从 https://github.com/microsoftarchive/redis/releases 下载并安装")
        input("安装完成后按 Enter 继续...")
    elif choice == "4":
        print("请在 WSL2 中运行: sudo apt-get update && sudo apt-get install -y redis-server")
        input("安装完成后按 Enter 继续...")
    
    return False


def install_redis_macos():
    """在 macOS 上安装 Redis"""
    print("\n=== macOS Redis 安装 ===")
    
    if shutil.which("brew"):
        try:
            subprocess.check_call(["brew", "install", "redis"])
            subprocess.check_call(["brew", "services", "start", "redis"])
            print("✓ Redis 通过 Homebrew 安装并启动成功")
            return True
        except Exception as e:
            print(f"✗ Homebrew 安装失败: {e}")
    else:
        print("请先安装 Homebrew: /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
    
    return False


def install_redis_linux():
    """在 Linux 上安装 Redis"""
    print("\n=== Linux Redis 安装 ===")
    
    system_info = platform.platform()
    
    if "Ubuntu" in system_info or "Debian" in system_info:
        try:
            subprocess.check_call(["sudo", "apt-get", "update"])
            subprocess.check_call(["sudo", "apt-get", "install", "-y", "redis-server"])
            subprocess.check_call(["sudo", "systemctl", "enable", "redis-server"])
            subprocess.check_call(["sudo", "systemctl", "start", "redis-server"])
            print("✓ Redis 安装并启动成功")
            return True
        except Exception as e:
            print(f"✗ APT 安装失败: {e}")
    elif "CentOS" in system_info or "RHEL" in system_info or "Fedora" in system_info:
        try:
            subprocess.check_call(["sudo", "yum", "install", "-y", "redis"])
            subprocess.check_call(["sudo", "systemctl", "enable", "redis"])
            subprocess.check_call(["sudo", "systemctl", "start", "redis"])
            print("✓ Redis 安装并启动成功")
            return True
        except Exception as e:
            print(f"✗ YUM 安装失败: {e}")
    else:
        print("请根据您的 Linux 发行版手动安装 Redis")
        print("Ubuntu/Debian: sudo apt-get install redis-server")
        print("CentOS/RHEL: sudo yum install redis")
        print("Fedora: sudo dnf install redis")
    
    return False


def start_redis():
    """尝试启动 Redis 服务器"""
    system = platform.system()
    
    try:
        if system == "Windows":
            # Windows 上尝试直接启动
            result = subprocess.run(
                ["redis-server", "--daemonize", "yes"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        elif system == "Darwin":
            # macOS
            result = subprocess.run(
                ["brew", "services", "start", "redis"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        else:
            # Linux
            result = subprocess.run(
                ["sudo", "systemctl", "start", "redis-server"],
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode != 0:
                # 尝试无 sudo
                result = subprocess.run(
                    ["systemctl", "start", "redis-server"],
                    capture_output=True,
                    timeout=10,
                    text=True,
                )
            return result.returncode == 0
    except Exception as e:
        print(f"启动 Redis 失败: {e}")
        return False


def test_redis_connection() -> bool:
    """测试 Redis 连接"""
    try:
        import redis
        from app.config import REDIS_URL
        
        client = redis.from_url(REDIS_URL, socket_timeout=5)
        client.ping()
        print("✓ Redis 连接测试成功")
        return True
    except ImportError:
        print("✗ redis Python 包未安装")
        return False
    except Exception as e:
        print(f"✗ Redis 连接失败: {e}")
        return False


def create_env_file():
    """创建 .env 文件（如果不存在）"""
    env_path = Path(__file__).parent.parent / ".env"
    
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "REDIS_URL" in content:
            print("✓ .env 文件中已有 REDIS_URL 配置")
            return
    
    redis_url = "redis://localhost:6379/0"
    
    env_content = f"""# Redis 配置
REDIS_URL={redis_url}
REDIS_DEFAULT_EXPIRE=3600
"""
    
    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8")
        if "REDIS_URL" not in existing:
            env_content = existing.rstrip() + "\n\n" + env_content
    
    env_path.write_text(env_content, encoding="utf-8")
    print(f"✓ 已更新 .env 文件: {env_path}")


def main():
    print("=" * 60)
    print("Redis 安装和配置工具")
    print("=" * 60)
    
    system = platform.system()
    print(f"当前系统: {system} {platform.release()}")
    print()
    
    # 步骤 1: 检查 Redis 是否已安装
    if check_redis_installed():
        print("✓ Redis 服务器已安装")
    else:
        print("✗ Redis 服务器未安装")
        
        if system == "Windows":
            install_redis_windows()
        elif system == "Darwin":
            install_redis_macos()
        else:
            install_redis_linux()
    
    # 步骤 2: 启动 Redis
    print("\n--- 启动 Redis ---")
    if not start_redis():
        print("⚠ Redis 启动失败，请手动启动")
        print(f"  Windows: redis-server --daemonize yes")
        print(f"  macOS: brew services start redis")
        print(f"  Linux: sudo systemctl start redis-server")
    
    # 步骤 3: 安装 Python redis 包
    print("\n--- 安装 Python 依赖 ---")
    install_python_package("redis>=5.0.0")
    
    # 步骤 4: 测试连接
    print("\n--- 测试 Redis 连接 ---")
    if test_redis_connection():
        print("\n✅ Redis 配置完成！")
    else:
        print("\n⚠ Redis 连接测试失败")
        print("请确保 Redis 服务器正在运行，默认端口 6379")
        print("可以稍后通过 '/api/cache/health' 端点检查状态")
    
    # 步骤 5: 创建 .env 文件
    print("\n--- 环境配置 ---")
    create_env_file()
    
    print("\n" + "=" * 60)
    print("配置完成！重启应用以启用 Redis 缓存功能")
    print("=" * 60)


if __name__ == "__main__":
    import shutil
    main()
