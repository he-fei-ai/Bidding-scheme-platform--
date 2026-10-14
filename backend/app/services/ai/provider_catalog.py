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
    "id": "agnes",
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
#:
#: P1-ID（2026-10-14）：每条新增稳定机器标识 ``id``。此前条目只有中文 ``name``，
#: 而 ``text_provider_configs`` / ``ai_config`` 均以 ``name`` 为主键，导致「按机器
#: 可判定的方式查元数据」无处落脚 —— 前端 `TEXT_PROVIDER_META` 只能自行维护一份
#: snake_case 键表（11 个键仅 2 个能对上后端 name），`find_model_metadata` 也无法
#: 按中文名命中（见 `resolve_provider_meta_key`）。``id`` 是**新增字段**，不改变
#: 任何既有键与取值，向后兼容。
KNOWN_PROVIDERS: list[dict] = [
    dict(DEFAULT_TEXT_PROVIDER),
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "type": "openai_compatible"},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "type": "deepseek"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow", "base_url": "https://api.siliconflow.cn/v1", "type": "openai_compatible"},
    {"id": "dashscope", "name": "阿里云百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "type": "openai_compatible"},
    {"id": "zhipu", "name": "智谱AI (GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4", "type": "openai_compatible"},
    {"id": "baidu", "name": "百度千帆", "base_url": "https://qianfan.baidubce.com/v2", "type": "openai_compatible"},
    {"id": "hunyuan", "name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "type": "openai_compatible"},
    {"id": "volcengine", "name": "字节跳动豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "type": "openai_compatible"},
    {"id": "minimax", "name": "MiniMax", "base_url": "https://api.minimax.chat/v1", "type": "openai_compatible"},
    {"id": "moonshot", "name": "Moonshot (Kimi)", "base_url": "https://api.moonshot.cn/v1", "type": "openai_compatible"},
    {"id": "lingyi", "name": "零一万物 Yi", "base_url": "https://api.lingyiwanwu.com/v1", "type": "openai_compatible"},
    {"id": "baichuan", "name": "百川智能", "base_url": "https://api.baichuan-ai.com/v1", "type": "openai_compatible"},
    {"id": "stepfun", "name": "阶跃星辰 StepFun", "base_url": "https://api.stepfun.com/v1", "type": "openai_compatible"},
    {"id": "tiangong", "name": "昆仑万维天工", "base_url": "https://api.tiangong.cn/v1", "type": "openai_compatible"},
    {"id": "sensenova", "name": "商汤日日新（SenseTime）", "base_url": "https://token.sensenova.cn/v1", "type": "openai_compatible"},
    {"id": "anthropic", "name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "type": "anthropic_compatible"},
    {"id": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "type": "openai_compatible"},
    {"id": "xfyun_spark", "name": "科大讯飞 Spark (MaaS)", "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2", "type": "openai_compatible"},
    {"id": "xingchen_maas", "name": "讯飞星辰 MaaS (Token Plan)", "base_url": "https://maas-token-api.cn-huabei-1.xf-yun.com/v2", "type": "openai_compatible"},
    {"id": "nvidia", "name": "NVIDIA NIM API", "base_url": "https://integrate.api.nvidia.com/v1", "type": "openai_compatible"},
    {"id": "pangu", "name": "华为云盘古", "base_url": "https://pangu.cn-north-4.myhuaweicloud.com/v1", "type": "openai_compatible"},
    {"id": "sensenova_chat", "name": "商汤日日新", "base_url": "https://api.sensechao.com/v1", "type": "openai_compatible"},
    {"id": "taichu", "name": "中科院紫东太初", "base_url": "https://api.taichu.com/v1", "type": "openai_compatible"},
    {"id": "jiutian", "name": "中国移动九天", "base_url": "https://jiutian.10086.cn/v1", "type": "openai_compatible"},
    {"id": "xingchen", "name": "中国电信星辰", "base_url": "https://xingchen.ctyun.cn/v1", "type": "openai_compatible"},
    {"id": "mathgpt", "name": "好未来 (MathGPT)", "base_url": "https://www.xueersi.com/api/mathgpt/v1", "type": "openai_compatible"},
    {"id": "youdao", "name": "网易有道", "base_url": "https://ai.youdao.com/v1", "type": "openai_compatible"},
    {"id": "groq", "name": "Groq", "base_url": "https://api.groq.com/openai/v1", "type": "openai_compatible"},
    {"id": "together", "name": "Together AI", "base_url": "https://api.together.xyz/v1", "type": "openai_compatible"},
    {"id": "fireworks", "name": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1", "type": "openai_compatible"},
    {"id": "mistral", "name": "Mistral AI", "base_url": "https://api.mistral.ai/v1", "type": "openai_compatible"},
    {"id": "xai", "name": "xAI (Grok)", "base_url": "https://api.x.ai/v1", "type": "openai_compatible"},
    {"id": "perplexity", "name": "Perplexity", "base_url": "https://api.perplexity.ai", "type": "openai_compatible"},
    {"id": "streamlake", "name": "快手万顷 (StreamLake / KAT-Coder)", "base_url": "https://wanqing.streamlakeapi.com/api/gateway/v1/endpoints", "type": "openai_compatible"},
    {"id": "cohere", "name": "Cohere", "base_url": "https://api.cohere.com/v1", "type": "openai_compatible"},
    {"id": "ai21", "name": "AI21 Labs (Jamba)", "base_url": "https://api.ai21.com/studio/v1", "type": "openai_compatible"},
    {"id": "custom", "name": "自定义兼容 OpenAI 接口", "base_url": "", "type": "openai_compatible"},
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


def find_known_provider_by_id(provider_id: str) -> dict | None:
    """按稳定机器 id 查元数据（大小写/下划线不敏感）。

    P1-ID（2026-10-14）：`/ai/provider-configs` 用它把 `provider_name`（中文展示名）
    翻译成前端/元数据表都能识别的 `id`，从而消除前端自维护键表带来的漂移。
    """
    if not provider_id:
        return None
    target = provider_id.strip().lower().replace("-", "_")
    for p in KNOWN_PROVIDERS:
        if str(p.get("id", "")).strip().lower().replace("-", "_") == target:
            return p
    return None


def provider_id_of(provider_name: str) -> str:
    """把厂商展示名翻译为稳定 id；未收录时返回空串（调用方按原名兜底）。"""
    p = find_known_provider(provider_name)
    return str(p.get("id", "")) if p else ""


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


#: 厂商 id/type → PROVIDER_MODEL_METADATA 表键（单一映射表，避免各处重复推导）。
#: 只有在 PROVIDER_MODEL_METADATA 里**确有条目**的厂商才登记；未登记的走默认表。
_PROVIDER_ID_TO_META_KEY: dict[str, str] = {
    "agnes": "openai_compatible",
    "openai": "openai_compatible",
    "custom": "openai_compatible",
    "deepseek": "deepseek",
    "siliconflow": "siliconflow",
    "dashscope": "dashscope",
    "zhipu": "zhipu",
    "baidu": "baidu",
    "hunyuan": "hunyuan",
    "volcengine": "volcengine",
    "stepfun": "stepfun",
    "moonshot": "moonshot",
    "minimax": "minimax",
    "lingyi": "lingyi",
    "baichuan": "baichuan",
}


def _normalize_provider_token(token: str) -> str:
    return (token or "").strip().lower().replace("_", "").replace("-", "")


def resolve_provider_meta_key(provider: str) -> str | None:
    """把「厂商 id / 展示名 / type」统一解析为 PROVIDER_MODEL_METADATA 的表键。

    P1-META（2026-10-14）：`find_model_metadata` 此前只做「去下划线后互相包含」，
    对 `provider="智谱AI (GLM)"` 这类**中文展示名**永远匹配不上 → `/ai/model-info`
    对 zhipu / hunyuan / volcengine / dashscope / baidu / moonshot 一律回落默认值
    （4096 tokens / 并发 4），前端「选模型后自动填充 max_tokens」形同虚设。
    实测（2026-10-14）38 个厂商名中仅 2 个能命中。

    解析顺序（先精确后模糊，保证零回归）：
      1. 展示名 → KNOWN_PROVIDERS → ``id`` → 映射表；
      2. ``id`` 直接查映射表；
      3. 原「去下划线后互相包含」的模糊匹配（保留既有行为）。
    空串**必须提前返回 None**：否则 `"" in "deepseek"` 恒真，会把空厂商名错配到
    第一张表（deepseek），导致 `find_model_metadata("", "gpt-4o")` 行为不可预期。
    """
    if not provider or not provider.strip():
        return None

    # 1) 展示名 → id → 映射表
    known = find_known_provider(provider)
    if known:
        meta_key = _PROVIDER_ID_TO_META_KEY.get(str(known.get("id", "")).lower())
        if meta_key:
            return meta_key
        # 收录厂商但没有专属元数据表 → 按 id 做一次模糊匹配（覆盖 openai_compatible 等）
        meta_key = _PROVIDER_ID_TO_META_KEY.get("openai_compatible") if known.get("type") == "openai_compatible" else None
        if meta_key:
            return meta_key

    # 2) 传入的本身就是 id（前端 TEXT_PROVIDER_META 的键 / provider_type）
    pid_raw = provider.strip().lower()
    if pid_raw in _PROVIDER_ID_TO_META_KEY:
        return _PROVIDER_ID_TO_META_KEY[pid_raw]

    # 3) 既有模糊口径（去下划线后互相包含）
    pid = _normalize_provider_token(provider)
    if not pid:
        return None
    for key in PROVIDER_MODEL_METADATA:
        norm = key.replace("_", "")
        if norm in pid or pid in norm:
            return key
    return None


def find_model_metadata(provider: str, model: str) -> dict | None:
    """按厂商（id / 展示名 / type，大小写与下划线容错）+ 模型名查元数据，未命中返回 None。

    F-8 历史修复的行为在此固化：厂商名与模型名都做大小写容错，
    避免「厂商 /models 回传大小写与元数据表键不一致」导致永远匹配失败。
    P1-META（2026-10-14）：厂商解析下沉到 `resolve_provider_meta_key`，
    新增中文展示名 / 稳定 id 的解析能力。
    """
    if not model:
        return None
    matched = resolve_provider_meta_key(provider)
    if not matched:
        return None
    table = PROVIDER_MODEL_METADATA.get(matched)
    if not table:
        return None
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