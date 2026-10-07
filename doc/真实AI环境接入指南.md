# 真实 AI 环境接入指南
# ====================
# 如何从 MockProvider 切换到真实 AI 环境进行测试

# 一、MockProvider 接口定义
# MockOutlineProvider 必须实现以下方法（位于 test_outline_integration.py:222）：
class MockOutlineProvider:
    """响应式 Mock 实现 OutlineProvider 协议"""
    def __init__(self, responses: list): ...
    async def call(self, prompt: str, **kwargs) -> str:
        """返回下一条预设响应"""
        ...

# workflows.py 中 generate_outline_v3 的 provider 参数类型为 Any，
# 只要实现 call(prompt, **kwargs) -> str 接口即可

# 二、真实 AI Provider 接口（OpenAI 兼容）
# 位于 backend/app/services/ai/providers/openai_compatible.py

from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.services.ai.providers.deepseek import DeepSeekProvider

# 三、切换到真实 AI 环境的步骤

# 步骤 1：配置 AI Provider
# 在 backend/.env 中添加（或修改）：
# OPENAI_API_KEY=your_key_here
# OPENAI_BASE_URL=https://api.openai.com/v1   # 或其他兼容端点
# AI_MODEL=gpt-4o-mini                        # 默认模型
#
# 使用 DeepSeek（国内推荐，速度快、成本低）：
# DEEPSEEK_API_KEY=your_deepseek_key
#
# 使用通义千问（阿里云）：
# QWEN_API_KEY=your_qwen_key

# 步骤 2：修改测试代码
# 将 test_e2e_verification.py 或 test_outline_integration.py 中的：
#
#   provider = MockOutlineProvider(responses)
#
# 替换为：
#
#   from app.services.ai.ai_provider_service import AIProviderService
#   from app.services.ai.provider_factory import create_outline_provider
#
#   # 方式A：使用配置好的全局 provider（推荐）
#   provider = await create_outline_provider("outline")
#
#   # 方式B：直接实例化（指定模型和端点）
#   provider = OpenAICompatibleProvider(
#       api_key="sk-xxx",
#       base_url="https://api.deepseek.com/v1",  # DeepSeek
#       # base_url="https://api.openai.com/v1",   # OpenAI
#       # base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",  # 通义千问
#       model="deepseek-chat",                    # DeepSeek V3
#       # model="gpt-4o-mini",                     # OpenAI
#       max_tokens=4096,
#       temperature=0.3,
#       timeout=180,
#       max_retries=2,
#   )

# 步骤 3：完整调用示例
"""
import asyncio
import json
import sys
sys.path.insert(0, "J:/编程/招投标方案平台/backend")

from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS
from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider

async def main():
    # 替换为真实 provider
    provider = OpenAICompatibleProvider(
        api_key="sk-xxx",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        max_tokens=4096,
        temperature=0.3,
        timeout=180,
        max_retries=2,
    )

    # 无需预置 responses，真实 AI 会实时生成
    result = await generate_outline_v3(
        provider=provider,
        summary="某市城市轨道交通5号线机电系统集成项目",
        scoring_points=json.dumps([
            {"point": "BIM技术应用", "weight": 10},
            {"point": "绿色施工", "weight": 8},
            {"point": "智能化系统", "weight": 12},
        ]),
        industry_profile="城市轨道交通",
        tender_context=open("J:/编程/招投标方案平台/doc/模拟大招标文件_测试数据.json", encoding="utf-8").read(),
        reference_outline="",
        project_scope="城市轨道交通5号线机电系统集成",
        project_info={"name": "某市城市轨道交通5号线机电系统集成项目"},
        progress_callback=None,
        phase_callback=None,
    )
    print(f"生成完成，{len(result)} 个一级章节")

asyncio.run(main())
"""

# 四、各模型配置参考

# 1. DeepSeek V3（推荐，国内访问快，成本低）
# base_url = "https://api.deepseek.com/v1"
# model = "deepseek-chat"
# max_tokens = 8192
# temperature = 0.3
#
# 2. 通义千问 Qwen-Max（阿里云）
# base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# model = "qwen-max"
# max_tokens = 8192
# temperature = 0.3
#
# 3. Kimi（Moonshot）
# base_url = "https://api.moonshot.cn/v1"
# model = "kimi-k2.5"
# max_tokens = 262144  # 长上下文支持
# temperature = 0.3
#
# 4. OpenAI GPT-4o-mini
# base_url = "https://api.openai.com/v1"
# model = "gpt-4o-mini"
# max_tokens = 4096
# temperature = 0.3

# 五、真实环境测试注意事项

# 1. 响应队列长度
#    MockProvider 需要 34 个响应（1补充+2 V6.0+3分析+28章）
#    真实 AI 每次调用实时生成，无需预置响应

# 2. 调用次数估算
#    - generate_supplement_chapters: 1次
#    - V6.0排序: 2次（最多2次重试）
#    - V3.6分析: 3次（BOQ/工界/工法，并发执行）
#    - fill_children: 28次
#    总计: ~34次 AI 调用
#
#    DeepSeek V3 费用估算: 约 0.03元/次 × 34 ≈ 1元
#    GPT-4o-mini 费用估算: 约 0.15元/次 × 34 ≈ 5元

# 3. 超时设置
#    真实 AI 单次调用可能需 5-30秒
#    建议 timeout=180s，max_retries=2

# 4. 提示词缓存（prompt cache warmup）
#    系统已实现"先串行后并发"的缓存优化：
#    - Phase1（串行）：28章先顺序填充，建立缓存
#    - Phase2（并发）：V3.6三项分析并发执行
#    真实环境下单次生成耗时约 2-5 分钟

# 5. 日志输出
#    真实环境运行后，日志会写入 logs/backend.log
#    包含完整的 LOG_JSON 格式事件，可接入 ELK 分析

# 六、最小可运行测试脚本
# 保存为: backend/tests/run_real_ai_test.py
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))

from app.services.ai.workflows import generate_outline_v3, FIXED_28_CHAPTERS
from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

async def main():
    provider = OpenAICompatibleProvider(
        api_key="sk-xxx",  # 替换为你的 API Key
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        max_tokens=4096,
        temperature=0.3,
        timeout=180,
        max_retries=2,
    )

    tender_context = (Path(__file__).parent.parent.parent / "doc" / "模拟大招标文件_测试数据.json").read_text(encoding="utf-8")

    result = await generate_outline_v3(
        provider=provider,
        summary="某市城市轨道交通5号线机电系统集成项目",
        scoring_points=json.dumps([
            {"point": "BIM技术应用", "weight": 10},
            {"point": "绿色施工", "weight": 8},
            {"point": "智能化系统", "weight": 12},
            {"point": "工期保障", "weight": 7},
            {"point": "质量管理体系", "weight": 10},
            {"point": "安全管理", "weight": 8},
            {"point": "造价控制", "weight": 5},
            {"point": "售后服务", "weight": 5},
        ]),
        industry_profile="城市轨道交通",
        tender_context=tender_context,
        reference_outline="",
        project_scope="城市轨道交通5号线机电系统集成，含通风空调、消防、给排水、低压配电、照明、综合监控、站台门等系统",
        project_info={"name": "某市城市轨道交通5号线机电系统集成项目"},
        progress_callback=None,
        phase_callback=None,
    )
    print(f"✅ 生成完成：{len(result)} 个一级章节")
    # 保存结果
    import datetime
    output_path = Path(__file__).parent.parent.parent / "doc" / f"生成的目录_{datetime.date.today()}.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 结果已保存: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
"""
