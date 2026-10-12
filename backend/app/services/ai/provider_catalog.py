"""厂商元数据单一正本（D-1，2026-10-06）

此前默认厂商 / URL / 模型元数据分散在 4 处，改一个默认值要动多个文件：

  - ``app/config.py``                       AGNES_BASE_URL / AGNES_MODEL
  - ``app/routers/ai_config.py``            BUILTIN_DEFAULT_PROVIDER / KNOWN_PROVIDERS /
                                            PROVIDER_MODEL_METADATA / _get_china_accessible_providers
  - ``app/services/ai/provider_factory.py``  _FALLBACK_PROVIDERS
  - ``app/services/ai/image_providers/*``    图像默认厂商（agnes-image-2.1-flash 等）

本模块是**唯一正本**：路由层与工厂层都从这里取数据，各处保留原有模块级名字
（``KNOWN_PROVIDERS`` / ``PROVIDER_MODEL_METADATA`` / ``_FALLBACK_PROVIDERS``）
作为别名，既有测试与调用方零改动。

注意：本模块只放**元数据**（名称/URL/模型/并发额度），绝不内联任何 API Key；
密钥一律来自环境变量或数据库加密列。
"""

from __future__ import annotations

from ...config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL

# ──────────────────────────────────────────────────────────────────────
# 默认厂商（文本 / 图像）
# ──────────────────────────────────────────────────────────────────────
#: 内置默认文本厂商（曾散落于 routers/ai_config.py 与 services/ai/provider_factory.py）
DEFAULT_TEXT_PROVIDER: dict = {
    "name": "agnes",
    "base_url": AGNES_BASE_URL,
    "model": AGNES_MODEL,
    "type": "openai_compatible",
    "readonly": False,
    "is_default": True,
}

#: 内置默认图像厂商（曾硬编码于 get_image_model_config / image_providers/service.py）
DEFAULT_IMAGE_PROVIDER: dict = {
    "provider": "agnes",
    "base_url": "https://api.agnes-ai.cn/v1",
    "model": "agnes-image-2.1-flash",
}


def default_image_base_url() -> str:
    return DEFAULT_IMAGE_PROVIDER["base_url"]


def default_image_model() -> str:
    return DEFAULT_IMAGE_PROVIDER["model"]


# ──────────────────────────────────────────────────────────────────────
# 已知厂商平台（/ai/providers 与配置页下拉列表）
# ──────────────────────────────────────────────────────────────────────
#: 已知文本厂商平台。首项为内置默认厂商（由 DEFAULT_TEXT_PROVIDER 派生，
#: 保证「改默认厂商只需改一处」）。
KNOWN_PROVIDERS: list[dict] = [
    dict(DEFAULT_TEXT_PROVIDER),
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "type": "openai_compatible"},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "type": "deepseek"},
    {"name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "type": "openai_compatible"},
    {"name": "阿里云百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "type": "openai_compatible"},
    {"name": "智谱AI (GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4", "type": "openai_compatible"},
    {"name": "百度千帆", "base_url": "https://qianfan.baidubce.com/v2", "type": "openai_compatible"},
    {"name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "type": "openai_compatible"},
    {"name": "字节跳动豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "type": "openai_compatible"},
    {"name": "MiniMax", "base_url": "https://api.minimax.chat/v1", "type": "openai_compatible"},
    {"name": "Moonshot (Kimi)", "base_url": "https://api.moonshot.cn/v1", "type": "openai_compatible"},
    {"name": "零一万物 Yi", "base_url": "https://api.lingyiwanwu.com/v1", "type": "openai_compatible"},
    {"name": "百川智能", "base_url": "https://api.baichuan-ai.com/v1", "type": "openai_compatible"},
    {"name": "阶跃星辰 StepFun", "base_url": "https://api.stepfun.com/v1", "type": "openai_compatible"},
    {"name": "昆仑万维天工", "base_url": "https://api.tiangong.cn/v1", "type": "openai_compatible"},
    {"name": "商汤日日新（SenseTime）", "base_url": "https://token.sensenova.cn/v1", "type": "openai_compatible"},
    {"name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "type": "anthropic_compatible"},
    {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "type": "openai_compatible"},
    {"name": "科大讯飞 Spark (MaaS)", "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2", "type": "openai_compatible"},
    {"name": "讯飞星辰 MaaS (Token Plan)", "base_url": "https://maas-token-api.cn-huabei-1.xf-yun.com/v2", "type": "openai_compatible"},
    {"name": "NVIDIA NIM API", "base_url": "https://integrate.api.nvidia.com/v1", "type": "openai_compatible"},
    {"name": "华为云盘古", "base_url": "https://pangu.cn-north-4.myhuaweicloud.com/v1", "type": "openai_compatible"},
    {"name": "商汤日日新", "base_url": "https://api.sensechao.com/v1", "type": "openai_compatible"},
    {"name": "中科院紫东太初", "base_url": "https://api.taichu.com/v1", "type": "openai_compatible"},
    {"name": "中国移动九天", "base_url": "https://jiutian.10086.cn/v1", "type": "openai_compatible"},
    {"name": "中国电信星辰", "base_url": "https://xingchen.ctyun.cn/v1", "type": "openai_compatible"},
    {"name": "好未来 (MathGPT)", "base_url": "https://www.xueersi.com/api/mathgpt/v1", "type": "openai_compatible"},
    {"name": "网易有道", "base_url": "https://ai.youdao.com/v1", "type": "openai_compatible"},
    {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "type": "openai_compatible"},
    {"name": "Together AI", "base_url": "https://api.together.xyz/v1", "type": "openai_compatible"},
    {"name": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1", "type": "openai_compatible"},
    {"name": "Mistral AI", "base_url": "https://api.mistral.ai/v1", "type": "openai_compatible"},
    {"name": "xAI (Grok)", "base_url": "https://api.x.ai/v1", "type": "openai_compatible"},
    {"name": "Perplexity", "base_url": "https://api.perplexity.ai", "type": "openai_compatible"},
    {"name": "快手万顷 (StreamLake / KAT-Coder)", "base_url": "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints", "type": "openai_compatible"},
    {"name": "Cohere", "base_url": "https://api.cohere.com/v1", "type": "openai_compatible"},
    {"name": "AI21 Labs (Jamba)", "base_url": "https://api.ai21.com/studio/v1", "type": "openai_compatible"},
    {"name": "自定义兼容 OpenAI 接口", "base_url": "", "type": "openai_compatible"},
]


def known_providers_public() -> list[dict]:
    """对外暴露的厂商列表（剔除任何 api_key 字段）。

    BUG-FIX（安全 P0 历史）：``/ai/providers`` 曾直接序列化含内联密钥的字典，
    等于把平台主密钥明文吐给调用方。这里做结构性防御而非依赖调用方自觉。
    """
    return [{k: v for k, v in p.items() if k != "api_key"} for p in KNOWN_PROVIDERS]


def find_known_provider(provider_name: str) -> dict | None:
    """按厂商名查元数据（大小写与空白不敏感）。"""
    if not provider_name:
        return None
    target = provider_name.strip().lower()
    for p in KNOWN_PROVIDERS:
        if str(p.get("name", "")).strip().lower() == target:
            return p
    return None


def find_default_base_url_for_provider(provider_name: str) -> str:
    """按厂商名（或 type 名）反查默认 base_url。

    G-2 修复配套（2026-10-11）：`POST /ai/test` 允许省略 base_url，此时回落到
    厂商目录里的默认 base_url；未收录则返回空串，由调用方决定是否报错。
    查找顺序：精确匹配 name → 精确匹配 type → 大小写宽松匹配。
    """
    if not provider_name:
        return ""
    target = provider_name.strip().lower()
    if not target:
        return ""
    for p in KNOWN_PROVIDERS:
        if str(p.get("name", "")).strip().lower() == target:
            return p.get("base_url", "")
    for p in KNOWN_PROVIDERS:
        if str(p.get("type", "")).strip().lower() == target:
            return p.get("base_url", "")
    return ""


# ──────────────────────────────────────────────────────────────────────
# 国内可访问推荐厂商（WAF 拦截/超时时的建议列表）
# ──────────────────────────────────────────────────────────────────────
CHINA_ACCESSIBLE_PROVIDERS: list[dict] = [
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "type": "deepseek", "models": ["deepseek-chat", "deepseek-reasoner"], "reason": "国内可直接访问，性价比高"},
    {"name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "type": "openai_compatible", "models": ["deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1"], "reason": "国内可直接访问，支持多模型"},
    {"name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "type": "openai_compatible", "models": ["hunyuan-large", "hunyuan-standard"], "reason": "腾讯云原生，国内直连"},
    {"name": "阿里云百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "type": "openai_compatible", "models": ["qwen-max", "qwen-plus", "qwen-turbo"], "reason": "阿里云直连，稳定可靠"},
    {"name": "智谱AI (GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4", "type": "openai_compatible", "models": ["glm-4-plus", "glm-4", "glm-4-flash"], "reason": "智谱AI直连，国内可用"},
]


def china_accessible_providers() -> list[dict]:
    """返回国内可直接访问的推荐提供商列表（返回副本，避免调用方就地改坏正本）。"""
    return [dict(p) for p in CHINA_ACCESSIBLE_PROVIDERS]


# ──────────────────────────────────────────────────────────────────────
# 模型元数据（max_tokens / 并发额度）
# ──────────────────────────────────────────────────────────────────────
PROVIDER_MODEL_METADATA: dict[str, dict[str, dict]] = {
    "deepseek": {
        "deepseek-v4-pro": {"max_tokens": 384000, "concurrency": 10},
        "deepseek-v4-flash": {"max_tokens": 384000, "concurrency": 20},
        "deepseek-chat": {"max_tokens": 384000, "concurrency": 20},
        "deepseek-reasoner": {"max_tokens": 384000, "concurrency": 5},
    },
    "openai_compatible": {
        "gpt-4o": {"max_tokens": 16384, "concurrency": 10},
        "gpt-4o-mini": {"max_tokens": 16384, "concurrency": 20},
        "gpt-4.1": {"max_tokens": 32768, "concurrency": 10},
        "gpt-4.1-mini": {"max_tokens": 32768, "concurrency": 20},
        "gpt-4-turbo": {"max_tokens": 4096, "concurrency": 10},
        "gpt-4": {"max_tokens": 4096, "concurrency": 10},
        "gpt-3.5-turbo": {"max_tokens": 4096, "concurrency": 20},
    },
    "siliconflow": {
        "deepseek-ai/DeepSeek-V4-Pro": {"max_tokens": 384000, "concurrency": 5},
        "deepseek-ai/DeepSeek-V4-Flash": {"max_tokens": 384000, "concurrency": 10},
        "deepseek-ai/DeepSeek-R1": {"max_tokens": 16384, "concurrency": 3},
        "deepseek-ai/DeepSeek-V3": {"max_tokens": 16384, "concurrency": 5},
        "Qwen/Qwen3.5-4B": {"max_tokens": 65536, "concurrency": 20},
        "Qwen/Qwen3-8B": {"max_tokens": 131072, "concurrency": 20},
    },
    "dashscope": {
        "qwen3-max": {"max_tokens": 131072, "concurrency": 5},
        "qwen3.5-plus": {"max_tokens": 131072, "concurrency": 10},
        "qwen3.6-flash": {"max_tokens": 131072, "concurrency": 20},
        "qwen-turbo": {"max_tokens": 131072, "concurrency": 20},
        "qwen-long": {"max_tokens": 65536, "concurrency": 5},
        "qwen3.5-omni-plus": {"max_tokens": 65536, "concurrency": 5},
    },
    "zhipu": {
        "glm-4-plus": {"max_tokens": 16384, "concurrency": 5},
        "glm-4": {"max_tokens": 8192, "concurrency": 10},
        "glm-4-flash": {"max_tokens": 8192, "concurrency": 20},
        "glm-4-long": {"max_tokens": 256000, "concurrency": 3},
        "glm-5": {"max_tokens": 131072, "concurrency": 5},
    },
    "baidu": {
        "ernie-5.0": {"max_tokens": 65536, "concurrency": 5},
        "ernie-5.1": {"max_tokens": 65536, "concurrency": 5},
        "ernie-4.5-turbo-128k": {"max_tokens": 12288, "concurrency": 10},
        "ernie-4.0": {"max_tokens": 2048, "concurrency": 10},
    },
    "hunyuan": {
        "hy3": {"max_tokens": 131072, "concurrency": 5},
        "hunyuan-2.0-thinking": {"max_tokens": 65536, "concurrency": 5},
        "hunyuan-2.0-instruct": {"max_tokens": 16384, "concurrency": 10},
        "hunyuan-turbo": {"max_tokens": 16384, "concurrency": 20},
        "hunyuan-lite": {"max_tokens": 16384, "concurrency": 30},
    },
    "volcengine": {
        "Doubao-Seed-2.0-Pro": {"max_tokens": 16384, "concurrency": 5},
        "Doubao-Seed-2.0-Code": {"max_tokens": 16384, "concurrency": 5},
        "Doubao-Seed-2.0-Lite": {"max_tokens": 8192, "concurrency": 20},
        "Doubao-Seed-2.0-Mini": {"max_tokens": 4096, "concurrency": 30},
    },
    "stepfun": {
        "step-3.5-flash": {"max_tokens": 131072, "concurrency": 10},
        "step-1-32k": {"max_tokens": 16384, "concurrency": 5},
        "step-1-8k": {"max_tokens": 4096, "concurrency": 5},
    },
    "moonshot": {
        "kimi-k3": {"max_tokens": 65536, "concurrency": 3},
        "kimi-k2.6": {"max_tokens": 262144, "concurrency": 3},
        "kimi-k2": {"max_tokens": 16384, "concurrency": 5},
        "kimi-k2-turbo": {"max_tokens": 16384, "concurrency": 10},
        "moonshot-v1-128k": {"max_tokens": 16384, "concurrency": 5},
    },
    "minimax": {
        "minimax-m3": {"max_tokens": 131072, "concurrency": 5},
        "minimax-m2.7": {"max_tokens": 131072, "concurrency": 5},
        "minimax-m2.5": {"max_tokens": 131072, "concurrency": 10},
        "minimax-01": {"max_tokens": 65536, "concurrency": 3},
    },
    "lingyi": {
        "yi-large": {"max_tokens": 16384, "concurrency": 5},
        "yi-spark": {"max_tokens": 8192, "concurrency": 10},
    },
    "baichuan": {
        "Baichuan4-Turbo": {"max_tokens": 131072, "concurrency": 5},
        "Baichuan3-Turbo": {"max_tokens": 32768, "concurrency": 10},
    },
}


def find_model_metadata(provider: str, model: str) -> dict | None:
    """按厂商（大小写/下划线容错）+ 模型名查元数据，未命中返回 None。

    F-8 历史修复的行为在此固化：厂商名与模型名都做大小写容错，
    避免「厂商 /models 回传大小写与元数据表键不一致」导致永远匹配失败。
    """
    if not model:
        return None
    pid = (provider or "").lower().replace("_", "")
    matched: str | None = None
    for key in PROVIDER_MODEL_METADATA:
        if key.replace("_", "") in pid or pid in key.replace("_", ""):
            matched = key
            break
    if not matched:
        return None
    table = PROVIDER_MODEL_METADATA[matched]
    meta = table.get(model)
    if meta:
        return meta
    return {k.lower(): v for k, v in table.items()}.get(model.lower())


# ──────────────────────────────────────────────────────────────────────
# 内置兜底提供商（降级链最后一层，永不消失）
# ──────────────────────────────────────────────────────────────────────
#: 注意 ``api_key`` 一律取环境变量（不硬编码任何密钥），多为空时由降级链回退到主配置密钥。
FALLBACK_PROVIDERS: list[dict] = [
    {
        "name": "agnes",
        "provider": "openai_compatible",
        "base_url": AGNES_BASE_URL,
        "api_key": AGNES_API_KEY,
        "model": AGNES_MODEL,
        "max_tokens": 32768,
        "temperature": 0.7,
        "concurrency": 10,
        "is_builtin_default": True,
    },
    {
        "name": "DeepSeek",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "is_builtin_default": False,
    },
    {
        "name": "SiliconFlow",
        "provider": "openai_compatible",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "deepseek-ai/DeepSeek-V3",
        "is_builtin_default": False,
    },
    {
        "name": "通义千问",
        "provider": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "qwen-max",
        "is_builtin_default": False,
    },
    {
        "name": "智谱GLM",
        "provider": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-plus",
        "is_builtin_default": False,
    },
    {
        "name": "OpenAI（默认）",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
        "is_builtin_default": False,
    },
]