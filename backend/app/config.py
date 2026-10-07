import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


HOST = os.environ.get("BID_ASSISTANT_HOST", "127.0.0.1")
PORT = int(os.environ.get("BID_ASSISTANT_PORT", "8000"))


DATA_DIR = Path(
    os.environ.get(
        "BID_ASSISTANT_DATA_DIR",
        PROJECT_ROOT / "data",
    ),
)


DATABASE_PATH = DATA_DIR / "bid_assistant.sqlite3"


PROJECTS_DATA_DIR = DATA_DIR / "projects"
KNOWLEDGE_DATA_DIR = DATA_DIR / "knowledge"
KNOWLEDGE_SOURCE_DIR = KNOWLEDGE_DATA_DIR / "source"
KNOWLEDGE_MARKDOWN_DIR = KNOWLEDGE_DATA_DIR / "markdown"
REJECTION_REPORTS_DIR = DATA_DIR / "rejection_reports"
DUPLICATE_REPORTS_DIR = DATA_DIR / "duplicate_reports"
FEASIBILITY_REPORTS_DIR = DATA_DIR / "feasibility_reports"


def report_download_roots() -> tuple[Path, ...]:
    """允许作为「报告下载」来源的根目录白名单（已 resolve，便于直接比较）。

    BUG-FIX 2026-09-15：废标/查重报告此前写死在 `DATA_DIR/rejection_reports`、
    `DATA_DIR/duplicate_reports`，但下载端点只允许 `DATA_DIR/projects` 下的路径，
    于是 `/rejection/export-report/{id}` 与 `/duplicate/export-report/{id}` **恒定 403**，
    前端「下载报告」按钮从未真正可用（单测只覆盖了服务层 `export_report`，没覆盖路由守卫）。
    这里把两类报告目录与项目导出目录统一登记为合法根目录。
    """
    return (
        PROJECTS_DATA_DIR.resolve(),
        REJECTION_REPORTS_DIR.resolve(),
        DUPLICATE_REPORTS_DIR.resolve(),
        FEASIBILITY_REPORTS_DIR.resolve(),
    )


def is_downloadable_path(path: Path) -> bool:
    """判断 `path` 是否落在允许下载的根目录内（已 resolve 后再比较，防路径穿越）。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root) for root in report_download_roots())


# Redis配置
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_DEFAULT_EXPIRE = int(os.environ.get("REDIS_DEFAULT_EXPIRE", "3600"))


# 正文生成并发控制（用于 AdaptiveConcurrencyController）
CONCUR_MIN = int(os.environ.get("CONCUR_MIN", "1"))  # 最小并发数
CONCUR_MAX = int(os.environ.get("CONCUR_MAX", "20"))  # 最大并发数（原15，提升至20以支持更高并行）
CONCUR_INITIAL = int(
    os.environ.get("CONCUR_INITIAL", "8")
)  # 初始并发数（原6，提升至8更快达到最佳并发）
# 全局AI并发上限：跨所有项目限制的总并发AI请求数，防止provider过载
GLOBAL_AI_CONCURRENCY_MAX = int(os.environ.get("GLOBAL_AI_CONCURRENCY_MAX", str(CONCUR_MAX)))


# SSE 总超时时间（秒），防止长连接挂起
# 目录生成默认15分钟
# CLEANUP 2026-10-04：移除死配置 SSE_TIMEOUT_CONTENT —— 正文生成已改用动态超时
# （按章节规模估算 + 1.5x 软告警 + 4h 硬熔断，见 sse_content.py/_expand_budget），
# 全仓无任何代码读取该配置；test_outline_asyncio.py 的钉死测试同步更新。
SSE_TIMEOUT_OUTLINE = int(os.environ.get("SSE_TIMEOUT_OUTLINE", "900"))  # 15分钟

# 正文生成 SSE 保活与队列轮询参数
# ENHANCE 2026-09-29（P0 心跳风暴/队列饿死修复）：原实现把「8 秒」硬编码在
# sse_content.py 主循环里，且只做空闲判断、没有「两次心跳最小间隔」判断。
#   SSE_HEARTBEAT_INTERVAL_SECS：AI 静默多久发一次 keep_alive，同时作为两条心跳
#     之间的最小间隔。默认 8 秒 = 修复前的设计意图，向后兼容，前端无需改动。
#   SSE_QUEUE_POLL_INTERVAL_SECS：queue.get 的轮询超时。原为 10 秒（事件转发最坏
#     延迟 10 秒）；默认 1 秒让进度事件近实时转发。设为 0.5~2 之间均可，调大只会
#     增加日志延迟，不影响正确性。
SSE_HEARTBEAT_INTERVAL_SECS = float(os.environ.get("SSE_HEARTBEAT_INTERVAL_SECS", "8"))
SSE_QUEUE_POLL_INTERVAL_SECS = float(os.environ.get("SSE_QUEUE_POLL_INTERVAL_SECS", "1"))

# 全局事实一致性审计（POST /projects/{id}/facts/audit）
# ENHANCE 2026-09-29（覆盖率假象）：旧实现只取前 5 章、每章前 500 字送审，
# 却把 audited_sections 报成全章数 —— 10 万字的标书被检查不到 3%，而前端绿字
# 「一致性检查通过」。现在改为按字符装箱全覆盖，本项仅作为**成本上限开关**：
#   FACTS_AUDIT_MAX_GROUPS：最多送审的分组数（每组≈ 30000 字符）。默认 0 = 不限制
#     （全覆盖）。设为 >0 时只审计前 N 组（更省 AI），并如实返回 truncated=true。
#     默认 0 相对「修复前只查 5 章」是行为变严格（每组一次 AI 调用，全量文档约为
#     修复前数十倍调用量），但符合产品预期：不报假的「全部一致」。接口形状未变，前端无需改动。
FACTS_AUDIT_MAX_GROUPS = int(os.environ.get("FACTS_AUDIT_MAX_GROUPS", "0"))


# ========== ENHANCE 2026-10-06：正文生成质量门禁默认值 ==========
# 缺口：正文生成的三个质量门禁（内容编排 / 字数采样 / 生成后一致性审计）此前
# 全部默认 False，意味着「跑完一轮正文」并不保证任何质量保障，而前端把它当
# 正常流程走，用户无从得知自己这一轮是**无门禁**跑的。
#
# 为什么不直接把默认值改成 True：三项门禁都会显著增加 AI 调用次数与耗时
# （一致性审计还要再跑一遍全文审计），对现有用户属于「静默变慢变贵」，属于
# 产品决策，不应由代码单方面翻转。因此这里采取**可配置 + 默认不变**：
#   运维可用 env 统一打开，前端显式传参仍可覆盖（Query 默认值改为读这里）。
#   默认值与改动前完全一致 → 零行为变更、向后兼容。
#
#   CONTENT_USE_ORCHESTRATION_DEFAULT   内容编排（图表/表格插入决策）
#   CONTENT_USE_WORD_SAMPLING_DEFAULT   字数预算采样
#   CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT 生成后全文一致性审计
def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


CONTENT_USE_ORCHESTRATION_DEFAULT = _env_bool("CONTENT_USE_ORCHESTRATION_DEFAULT", "false")
CONTENT_USE_WORD_SAMPLING_DEFAULT = _env_bool("CONTENT_USE_WORD_SAMPLING_DEFAULT", "false")
CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT = _env_bool("CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT", "false")


# ========== 增强 1.1: OCR 配置 ==========
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"
OCR_ENGINE = os.environ.get("OCR_ENGINE", "mineru")  # mineru | paddleocr | none
OCR_MAX_FILE_SIZE_MB = int(os.environ.get("OCR_MAX_FILE_SIZE_MB", "50"))
# MinerU CLI 可执行文件路径（容器内为绝对路径，本地为 PATH 中的 "mineru"）
OCR_MINERU_PATH = os.environ.get("OCR_MINERU_PATH", "mineru")
# 单文件 OCR 解析超时（秒），防止慢解析阻塞并发信号量
OCR_TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "300"))
# 最大并发解析数，避免同时拉起过多 MinerU 进程耗尽内存
OCR_MAX_CONCURRENT = int(os.environ.get("OCR_MAX_CONCURRENT", "2"))

# ========== 遗留修复 2026-10-06（L1）：.ppt 的 PowerPoint COM 自动化护栏 ==========
# .ppt（OLE 二进制）在没有 LibreOffice 的 Windows 上只能靠 win32com 拉起
# PowerPoint 转换。原实现直接在请求线程里 Dispatch("PowerPoint.Application")，
# 没有任何开关与超时：装了 Office 的机器上传 .ppt 会真实启动 PowerPoint 进程，
# 大文件或弹窗（修复模式/密码框）会把整个上传请求永久挂住，且无法靠配置关掉。
# PPT_COM_ENABLED=false   → 完全跳过 COM，直接返回降级提示（推荐生产环境）。
# PPT_COM_TIMEOUT（秒）   → COM 转换在后台线程执行，超时后放弃等待并返回降级提示，
#                          不阻塞上传主流程（后台线程是 daemon，会随进程退出）。
PPT_COM_ENABLED = os.environ.get("PPT_COM_ENABLED", "true").lower() == "true"
PPT_COM_TIMEOUT = int(os.environ.get("PPT_COM_TIMEOUT", "120"))

# ========== 遗留修复 2026-10-06（L3）：file_parse_tasks 保留策略 ==========
# 每次上传与每次「重新提取」都会新增一行 file_parse_tasks；此前没有任何清理，
# 长期项目的任务表只增不减（表名带 tasks，却从不被清理）。
# PARSE_TASK_RETENTION：每个项目保留的最新任务行数上限（超出即淘汰最旧的终态行），
#   默认 200；设为 0 表示不限制（保留旧行为）。
PARSE_TASK_RETENTION = int(os.environ.get("PARSE_TASK_RETENTION", "200"))


# ========== 增强 10.1（P-2）：PDF 表格感知提取 ==========
# True 时 PDF 优先走 pdfplumber 提取：按页加「--- 第 N 页 ---」页码标识，
# 并用「[表格 n] ... [表格结束]」标记表格，便于下游 AI 还原表格数据。
# pdfplumber 未安装或提取失败时自动回退 pypdf（原有行为），因此默认开启安全。
# 设为 false 可强制走 pypdf 纯文本路径（pdfplumber 内存占用更高时用）。
PDF_TABLE_EXTRACT_ENABLED = os.environ.get("PDF_TABLE_EXTRACT_ENABLED", "true").lower() == "true"

# ========== 增强 10.2（P-4）：短标书「直出」目录生成 ==========
# 易标规格二.2：短标书 + 强模型 → 直接输出 JSON 三级目录；
# 长标书 + 普通模型 → 分步生成（L1 → 遍历二三级）。
# 平台此前只有分步路径（V5.1），短标书场景白白多花 3 轮 AI 调用，
# 而强模型一次性输出反而更稳定（避免多次调用间口径漂移）。
# OUTLINE_DIRECT_ENABLED=false 可强制关闭直出路径（回退纯分步）。
OUTLINE_DIRECT_ENABLED = os.environ.get("OUTLINE_DIRECT_ENABLED", "true").lower() == "true"
# 评分点 JSON 文本长度超过此值则判定为「长标书」，走分步路径
OUTLINE_DIRECT_MAX_SCORING_CHARS = int(os.environ.get("OUTLINE_DIRECT_MAX_SCORING_CHARS", "2400"))
# 评分条目数超过此值同样走分步路径
OUTLINE_DIRECT_MAX_SCORING_ITEMS = int(os.environ.get("OUTLINE_DIRECT_MAX_SCORING_ITEMS", "8"))
# 直出路径期望的目录规模上限（一级章数）
OUTLINE_DIRECT_MAX_LEVEL1 = int(os.environ.get("OUTLINE_DIRECT_MAX_LEVEL1", "12"))


# ========== 增强 2.1: RAG 向量检索配置 ==========
RAG_ENABLED = os.environ.get("RAG_ENABLED", "true").lower() == "true"
RAG_EMBEDDING_MODEL = os.environ.get("RAG_EMBEDDING_MODEL", "BAAI/bge-m3")
RAG_VECTOR_DB = os.environ.get("RAG_VECTOR_DB", "chromadb")  # chromadb | duckdb
RAG_HYBRID_RETRIEVAL = os.environ.get("RAG_HYBRID_RETRIEVAL", "true").lower() == "true"
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
RAG_RERANK_ENABLED = os.environ.get("RAG_RERANK_ENABLED", "false").lower() == "true"


# ========== 增强 2.2: 检索缓存配置 ==========
RAG_CACHE_TTL = int(os.environ.get("RAG_CACHE_TTL", "3600"))  # 缓存过期时间，默认1小时


# ========== 增强 3.1: HTTP 代理配置 ==========
# 用于 AI 服务调用（如 OpenAI API）的网络代理，空字符串表示不启用代理
# 格式示例: http://127.0.0.1:7890
HTTP_PROXY = os.environ.get("HTTP_PROXY", "")  # HTTP 代理地址
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "")  # HTTPS 代理地址


# ========== 增强 3.2: 中文字体路径配置 ==========
# 用于 PDF 导出、图片渲染等场景的中文字体文件路径
# 默认使用系统字体，如需指定请设置绝对路径（如 /usr/share/fonts/xxx.ttf）
FONT_PATH = os.environ.get("FONT_PATH", "")


# ========== 增强 4.1: Mermaid HTTP 服务配置 ==========
# Mermaid 图表 HTTP 渲染服务地址（Docker mermaid-service 或 Railway Cloud）
# 格式示例: http://localhost:8080 或 https://mermaid-service.railway.app
MERMAID_SERVICE_URL = os.environ.get("MERMAID_SERVICE_URL", "http://localhost:8080")

# Mermaid HTTP 服务请求超时时间（秒）
MERMAID_SERVICE_TIMEOUT = int(os.environ.get("MERMAID_SERVICE_TIMEOUT", "30"))

# Mermaid 渲染结果 Redis 缓存开关（启用后可减少重复渲染）
MERMAID_CACHE_REDIS_ENABLED = (
    os.environ.get("MERMAID_CACHE_REDIS_ENABLED", "false").lower() == "true"
)


# ========== 内置 AI 厂商平台（默认厂商，无需用户手动输入）==========
# agnes 厂商平台：作为系统默认文本模型厂商，开箱即用
# 注意：必须通过环境变量 AGNES_API_KEY 配置有效密钥，系统不提供内置默认密钥
# 未配置时将拒绝 AI 请求，避免未授权访问
import logging as _logging


_logger = _logging.getLogger(__name__)
_AGNES_API_KEY = os.getenv("AGNES_API_KEY")
if not _AGNES_API_KEY:
    _logger.warning(
        "未配置 AGNES_API_KEY 环境变量，AI 功能将不可用。"
        "请设置环境变量 AGNES_API_KEY=<您的密钥> 或在前端设置页面配置。"
    )
AGNES_API_KEY = _AGNES_API_KEY or ""
AGNES_BASE_URL = os.getenv("AGNES_BASE_URL", "https://api.agnes-ai.cn/v1")
AGNES_MODEL = os.getenv("AGNES_MODEL", "agnes-2.0-flash")


# ========== 增强 4.2: 导出图片 DPI 配置 ==========
# DOCX 导出时图片的目标 DPI（不低于 300dpi）
# 较高的 DPI 可提升打印质量，但会增加文件大小
EXPORT_IMAGE_DPI = int(os.environ.get("EXPORT_IMAGE_DPI", "300"))


# ========== 图表模块 2026-10-04: 渲染失败占位符开关 ==========
# 控制图表渲染失败时（真实数据经 HTTP Service + PIL v2 双路径均失败）的行为：
#   false（默认）：静默跳过该图表，且不占用图号（图号回滚复用，正文图号连续不虚跳）。
#     —— 符合无降级原则：投标文件宁缺毋假，不在成品中留下任何失败说明/占位。
#   true：在该图表位置插入一行灰色斜体「图表渲染失败」事实说明（非伪造数据），
#     便于排错时定位哪个章节的图表数据有问题；此时图号仍回滚（说明文字不带编号）。
# 向后兼容：默认 false 与历史「始终插入失败说明」不同——历史行为等价于 true。
# 若需保留旧版可见的失败提示，设置环境变量 CHART_FAIL_PLACEHOLDER=true。
CHART_FAIL_PLACEHOLDER = os.environ.get("CHART_FAIL_PLACEHOLDER", "false").lower() == "true"


# ========== 增强 4.3: DOCX 导出表格样式配置 ==========
# BUG-E-06 修复：将表格样式魔法数字提取至配置，支持环境变量覆盖
# 表头背景色（16进制无 # 前缀，默认深蓝 "2B579A"）
EXPORT_TABLE_HEADER_BG = os.environ.get("EXPORT_TABLE_HEADER_BG", "2B579A")
# 交替行背景色（16进制无 # 前缀，默认浅蓝 "F2F7FB"）
EXPORT_TABLE_ALT_ROW_BG = os.environ.get("EXPORT_TABLE_ALT_ROW_BG", "F2F7FB")
# 表头文字颜色（16进制无 # 前缀，默认白色 "FFFFFF"）
EXPORT_TABLE_HEADER_FG = os.environ.get("EXPORT_TABLE_HEADER_FG", "FFFFFF")


# ========== 认证配置（默认关闭，开启后要求 Bearer Token） ==========
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "false").lower() == "true"
AUTH_TOKEN_EXPIRE_MINUTES = int(os.environ.get("AUTH_TOKEN_EXPIRE_MINUTES", "720"))
AUTH_INITIAL_PASSWORD = os.environ.get("AUTH_INITIAL_PASSWORD", "admin123")

# 运行环境：development / production。仅用于安全加固判断，不改变其他行为。
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").lower()

# BUG-P0-1 安全增强：启动时输出认证状态警告，防止生产环境忘记开启认证
if not AUTH_ENABLED:
    _logger.warning(
        "⚠️  认证机制已关闭（AUTH_ENABLED=false），所有 API 请求无需登录即可访问！\n"
        "   生产部署请设置环境变量 AUTH_ENABLED=true 启用 JWT 认证。"
    )

# BUG-P0-2 安全增强：生产环境强制开启认证，否则拒绝启动（纵深防御）
# 避免运维误将「开发免登录」配置带到生产，导致全 API 暴露。
if ENVIRONMENT == "production" and not AUTH_ENABLED:
    raise RuntimeError(
        "ENVIRONMENT=production 但 AUTH_ENABLED=false：生产环境必须启用认证，"
        "请设置 AUTH_ENABLED=true 后启动。"
    )

# BUG-P0-3 安全增强：已开启认证却仍使用默认初始密码时告警
if AUTH_ENABLED and AUTH_INITIAL_PASSWORD == "admin123":
    _logger.warning(
        "⚠️  正在使用默认初始密码 admin123，请通过 AUTH_INITIAL_PASSWORD 环境变量修改为强密码！"
    )


# ========== AI base_url SSRF 内网访问策略 ==========
# 增强 2026-09-29（配置安全）：AI 提供商 base_url 由用户配置，服务端会据此发起
# 出站请求（健康检查 / 模型列表 / 推理调用）。原 _validate_base_url 仅校验协议，
# 允许 http/https 指向任意主机（含 localhost、169.254.169.254 云元数据、内网 Redis 等），
# 构成 SSRF 跳板风险。为兼容「本地优先部署」（Ollama / 内网网关），默认放行内网；
# 若无需内网 AI 服务，设 AI_ALLOW_INTERNAL_URL=false 即可启用完整 SSRF 拦截。
AI_ALLOW_INTERNAL_URL = os.environ.get("AI_ALLOW_INTERNAL_URL", "true").lower() == "true"

if ENVIRONMENT == "production" and AI_ALLOW_INTERNAL_URL:
    _logger.warning(
        "⚠️  生产环境仍允许 AI base_url 指向内网/元数据地址（AI_ALLOW_INTERNAL_URL=true）。\n"
        "   若无内网自建模型需求，建议设置 AI_ALLOW_INTERNAL_URL=false 启用 SSRF 防护。"
    )
