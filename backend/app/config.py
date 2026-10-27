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

# ========== BUG-2026-10-19-02（P1 生产修复）：AI 调用超时可配置 + 自适应 ==========
# 背景：`collect_json_response`（workflows_base.py）内层 with_timeout 原先硬编码
# 20s(json_mode)/30s(降级)，与 provider 自身的 max_tokens 自适应超时
# （openai_compatible.chat：>=16000 tokens→180s、>=8000→120s）直接冲突——
# 慢模型（生产实证 spark-x2.5-4b）生成 L1 目录（max_tokens=8192）需 >30s，
# 外层提前熔断把整个目录生成任务判死（日志实证 3 连败 project=1871130，
# 「目录生成AI调用超时(json_mode)（20秒）」，详见 logs/backend.log 22:59-23:03）。
# 修复：两项超时改为环境变量可配；内容调用超时按 max_tokens 自适应抬高，
# 外层上限永不低于 provider 自身预算（`_content_call_timeout` 实现）。
AI_JSON_MODE_TIMEOUT = max(5, int(os.environ.get("AI_JSON_MODE_TIMEOUT", "20")))
AI_CALL_TIMEOUT = max(10, int(os.environ.get("AI_CALL_TIMEOUT", "60")))


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

# ========== 2026-10-11：字数偏差控制（正文生成模块深度修复）==========
# 缺口（2026-10-11 深度探索实测）：DB 中 10 个已生成 L3 章节的达标率仅 30%，
# 平均偏差率 +34.2%，100% 系统性偏高，最极端达 +120%。根因：
#   1. 提示词以「240-360 字」区间表达，模型按「至少」而非「不超过」理解；
#   2. 生成后无超长压缩机制（use_fallback_expand 只处理不足，且默认关闭）；
#   3. 偏差不可追溯，无 audit 端点。
#
# 修复：
#   - 提示词强化：具体目标值 + 硬性上限 + 自检要求（workflows_sections.py）
#   - 生成后压缩：新 CONTENT_CONDENSE_SYSTEM + condense_overlong_content()
#   - 偏差记录：sections 表新增 word_count_target / word_count_deviation 列
#
# 可配置项：
#   CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT 生成后超长自动压缩总开关。
#     默认 True —— 相比三个「静默关闭」的历史门禁，字数控制属于核心需求
#     （PRD 明确要求「字数符合目标要求」），默认开启以对齐产品预期。
#     关闭时行为与修复前完全一致（零压缩、零额外 AI 调用）。
#   CONTENT_WORD_COUNT_CONDENSE_THRESHOLD 触发压缩的偏差率阈值。
#     默认 1.15（15%）—— 实测 100% 偏高、平均 +34%，15% 略高于 L3 ±20%
#     下界的镜像位置，兼顾「不误压缩」与「及时压缩」。
#     设 1.00 表示零容差（严格按达标区间压缩）；设 999.0 表示禁用压缩。
#   CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS 单次生成最多压缩轮数。
#     默认 1（一轮压缩足以覆盖实测 +120% 的极端偏差；如需更严可设 2）。
CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT = _env_bool(
    "CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT", "true"
)
CONTENT_WORD_COUNT_CONDENSE_THRESHOLD = float(
    os.environ.get("CONTENT_WORD_COUNT_CONDENSE_THRESHOLD", "1.15")
)
CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS = max(
    0, int(os.environ.get("CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS", "1"))
)


# ========== 2026-10-12：易标 R9/R10/R11 字数双向多轮调整 ==========
# 生产接线（services/ai/workflows_word_adjust.py），在 condense / 兜底扩写之后运行：
#   R9  小节强控拉回（仅强控项目；每节 ≤3 轮；额度 |当前−S|）
#   R10 全文扩写 / 缩写（按比例分配额度；缩写 ≤3 轮；扩写连续 3 轮无进展即停）
#   R11 每次结果六道硬校验（不通过即保留原文）
#   CONTENT_USE_WORD_ADJUSTMENT_DEFAULT  是否启用该阶段。
#     默认 false —— 该阶段会额外产生 AI 调用（扩写方向不限轮），只有配置了
#     绝对字数目标的用户才需要；关闭时行为与修复前完全一致（零额外调用）。
#     请求参数 use_word_adjust=true 可单次强制开启。
#   CONTENT_WORD_ADJUST_AUTO_ON_STRICT  强控（R5 ±20% 硬区间）项目是否自动启用。
#     默认 true —— 强控承诺的是「区间是硬约束」，而 condense 只有 1 轮且不带
#     额度/校验，达不到硬约束语义，必须由 R9/R11 收敛。
#   CONTENT_WORD_ADJUST_MAX_ROUNDS      单节 / 单轮最大调整轮数（默认 3）。
#   CONTENT_WORD_ADJUST_CONCURRENCY     调整阶段并发上限（默认 4）。
CONTENT_USE_WORD_ADJUSTMENT_DEFAULT = _env_bool(
    "CONTENT_USE_WORD_ADJUSTMENT_DEFAULT", "false"
)
CONTENT_WORD_ADJUST_AUTO_ON_STRICT = _env_bool(
    "CONTENT_WORD_ADJUST_AUTO_ON_STRICT", "true"
)
CONTENT_WORD_ADJUST_MAX_ROUNDS = max(
    1, int(os.environ.get("CONTENT_WORD_ADJUST_MAX_ROUNDS", "3"))
)
CONTENT_WORD_ADJUST_CONCURRENCY = max(
    1, int(os.environ.get("CONTENT_WORD_ADJUST_CONCURRENCY", "4"))
)


# ========== 上传解析模块：单文件大小上限 ==========
# UPLOAD_MAX_FILE_SIZE_MB：所有格式（含图片）的通用上传上限，默认 50MB。
# 此前该值在 routers/files.py 里硬编码为 `50 * 1024 * 1024`，改上限要改代码。
# 向后兼容：不设置环境变量时行为与旧版完全一致（50MB）。
# 注意与 OCR_MAX_FILE_SIZE_MB 的关系：图片还要额外受 OCR_MAX_FILE_SIZE_MB 约束
# （两个上限取更严者），运维调大单文件上限时通常也需要同步调大 OCR 上限。
UPLOAD_MAX_FILE_SIZE_MB = max(1, int(os.environ.get("UPLOAD_MAX_FILE_SIZE_MB", "50")))


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

# PARSE_TASK_STALE_MINUTES：processing 停留超过该分钟数即判定为孤儿，由 beat 收敛为 failed。
# 背景（GAP-FIX 2026-10-17）：图片上传（R-3）走「broker 派发 → worker 收尾」，下列任一情形
# 都会让 uploaded_files 与 file_parse_tasks **永久停在 processing**：
#   1. dispatch_task 把消息投给了 broker，但 worker 未运行 / 队列消息丢失；
#   2. worker 进程被 SIGKILL（Celery time_limit 超时、容器重启），except 分支来不及执行；
#   3. 任务重试耗尽前 worker 崩溃。
# 此前没有任何回收路径：prune_parse_tasks 明确「在途 processing 行一律保留」，
# cleanup_old_results（每小时 beat）也不碰这两张表 —— 前端进度条因此永久卡住，
# 用户只能删除重传。默认 60 分钟（远大于大图 OCR 的正常耗时）；设为 0 可关闭回收。
PARSE_TASK_STALE_MINUTES = int(os.environ.get("PARSE_TASK_STALE_MINUTES", "60"))


# ========== 增强 10.1（P-2）：PDF 表格感知提取 ==========
# True 时 PDF 优先走 pdfplumber 提取：按页加「--- 第 N 页 ---」页码标识，
# 并用「[表格 n] ... [表格结束]」标记表格，便于下游 AI 还原表格数据。
# pdfplumber 未安装或提取失败时自动回退 pypdf（原有行为），因此默认开启安全。
# 设为 false 可强制走 pypdf 纯文本路径（pdfplumber 内存占用更高时用）。
PDF_TABLE_EXTRACT_ENABLED = os.environ.get("PDF_TABLE_EXTRACT_ENABLED", "true").lower() == "true"

# ========== BUG-FIX 2026-10-19：PDF 提取超时护栏 ==========
# 与 PPT_COM_TIMEOUT / OCR_TIMEOUT 同一教训：解析在请求线程内同步执行，
# 遇到病态 PDF（或日志风暴等环境放大）会挂 thousands 倍时长，前端超时判失败、
# file_parse_tasks 永停 processing、uploaded_files 无任何记录。
# 现给 _extract_pdf 整体加限时护栏：超时报错，由 save_upload 既有失败分支
# 如实写任务行 failed + 文件行 failed，不再无限挂住上传请求。
# 默认 600s（实测 278 页正常 PDF 全程 <60s，留 10 倍余量）；设为 0 关闭护栏。
PDF_EXTRACT_TIMEOUT = int(os.environ.get("PDF_EXTRACT_TIMEOUT", "600"))

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
# 文档方案一选择依据：模型最大输出 token 预算下限。运行模型输出预算低于此值
# （弱模型）即便评分点规模短也强制走分步路径，避免一次性长 JSON 被截断。
OUTLINE_DIRECT_MIN_OUTPUT_TOKENS = int(os.environ.get("OUTLINE_DIRECT_MIN_OUTPUT_TOKENS", "4096"))
# 运维显式声明当前文本模型的最大输出 token（0=未声明，回退 provider 属性探测
# 或仅按规模判别的旧口径）。弱模型（如输出预算 2k）应设为对应值以自动规避直出。
OUTLINE_DIRECT_MODEL_MAX_OUTPUT_TOKENS = int(os.environ.get("OUTLINE_DIRECT_MODEL_MAX_OUTPUT_TOKENS", "0"))

# ========== 增强 2026-10-08：目录生成各阶段 AI 调用 max_tokens 可调 ==========
# PRD 描述目录生成 max_tokens=2000，但实际按阶段需要放大（长标书三级 JSON 体积大）。
# 此处把各阶段上限显式暴露为配置，默认值与历史硬编码值一致（向后兼容），
# 便于按模型上下文窗口 / 标书规模调优，无需改代码。
OUTLINE_MAX_TOKENS_DIRECT = int(os.environ.get("OUTLINE_MAX_TOKENS_DIRECT", "6000"))
OUTLINE_MAX_TOKENS_L1 = int(os.environ.get("OUTLINE_MAX_TOKENS_L1", "8192"))
OUTLINE_MAX_TOKENS_L2 = int(os.environ.get("OUTLINE_MAX_TOKENS_L2", "4096"))
OUTLINE_MAX_TOKENS_SELFCHECK = int(os.environ.get("OUTLINE_MAX_TOKENS_SELFCHECK", "5000"))

# ========== 2026-10-11：目录生成自动确认开关（P-2） ==========
# 旧实现在 sse_outline.py 硬编码 auto_confirm=True，多阶段人工审核闸门不可达。
# 现改为配置项，默认 True（保持向后兼容，生产环境行为不变）。
# 设为 False 时启用多阶段人工审核：L1/L2/L3 各阶段生成后暂停，等待用户审核确认。
OUTLINE_AUTO_CONFIRM = os.environ.get("OUTLINE_AUTO_CONFIRM", "true").lower() == "true"

# ========== 增强 2026-10-10：三级标题质量收口（占位泛化标题治理） ==========
# 问题：L3 阶段完全不调用 AI，仅靠规则拼接；当二级标题为通用型
# （如「总体方案与实施策划」）时，三级标题大量退化为「总体实施策划（2）」「专项6」
# 这类占位式泛化名称，且伴随（2）/（3）/（第N项）等裸编号冲突后缀。
# 详见 backend/tests/test_l3_title_quality_2026_10_10.py 的复现用例。
# OUTLINE_L3_QUALITY_ENHANCE_ENABLED=false 可完全回退到历史行为（向后兼容）。
OUTLINE_L3_QUALITY_ENHANCE_ENABLED = (
    os.environ.get("OUTLINE_L3_QUALITY_ENHANCE_ENABLED", "true").lower() == "true"
)
# 三级标题 AI 修复单次最多修复的条数（旧实现硬编码 10，大目录必然残留脏标题）
OUTLINE_L3_REPAIR_MAX = int(os.environ.get("OUTLINE_L3_REPAIR_MAX", "24"))
# 三级标题 AI 修复的并发上限。修复任务用 asyncio.gather 全量并发，
# 每条任务都携带项目上下文（≤6000 token），大目录会瞬时打出数十个请求，
# 既压垮 provider 速率限制，也会在超时重试时成倍放大负载。见
# workflows_outline_utils.repair_l3_generic_titles 的信号量实现。
OUTLINE_L3_REPAIR_CONCURRENCY = max(
    1, int(os.environ.get("OUTLINE_L3_REPAIR_CONCURRENCY", "5"))
)
# 三级标题 AI 修复的最大轮数（2026-10-13 新增）。
# 背景：单轮最多修 OUTLINE_L3_REPAIR_MAX 条，大目录（>24 个泛化标题）
# 单轮必然大量残留，而旧实现的二次校验只写日志不做补救。现在
# `_l3_specificity_closeout` 按本配置循环修复，每轮后重新校验，
# 问题数不降即提前收尾（避免空烧 AI 调用）。设为 1 退回历史单轮行为。
OUTLINE_L3_REPAIR_MAX_ROUNDS = max(
    1, int(os.environ.get("OUTLINE_L3_REPAIR_MAX_ROUNDS", "3"))
)
# 同一个三级标题最多允许出现在几个不同的父级下（2026-10-15 新增）。
# 背景：2026-10-11 的去重政策修正把「跨父级同名」定性为合法（3.1/4.1 各自成节），
# 这确实消除了「（其N）」后缀污染，但也让**整章成套复用同一批泛化三级标题**
# 失去任何约束 —— 生产库实测出现过单个标题（如「智能建造地下室施工工艺」）
# 在 25 个互不相关的一级章下重复，用户反馈的「三级标题大量是占位式泛化名称」
# 主要就是这一形态。合法复用（同一工序要点出现在 2 个相关章）与成套复制的
# 分界就是复用广度，故按父级个数设上限，超出的部分改由所在章主题重新生成。
# 2026-10-27 第三轮收紧：默认从 3 降至 2（docx 实测 65 个 L3 标题重复 2-3 次，
# 收紧上限可进一步降低跨章重复率）。
# 设为 0 表示关闭该护栏（退回 2026-10-11 的历史行为，向后兼容）。
OUTLINE_L3_MAX_PARENT_REUSE = max(
    0, int(os.environ.get("OUTLINE_L3_MAX_PARENT_REUSE", "2"))
)
# 一级章节数硬上限（2026-10-19 新增，修复《岳阳路园区科研南楼室内装修工程》事故）。
# 背景：L1 章节数此前与「评分项数量严格对应」，评分项被拆出 22 条时目录直接
# 生成 22 节（其中大量章节只有标题堆砌、几乎无正文），对 180 天工期的装修项目
# 完全失控。现按行业惯例设硬上限（默认 15，8~15 节为投标文件合理区间）：
#   - AI 生成阶段：提示词告知「评分项超出上限时按专业逻辑归并」；
#   - 代码阶段：超过上限直接截断并记录 WARNING（评分覆盖允许牺牲，结构可控优先）；
#   - 兜底路径 `_build_l1_from_scoring_items` 同样按上限截断。
# 设为 0 表示关闭上限（退回历史行为，向后兼容）。
OUTLINE_L1_MAX_CHAPTERS = max(
    0, int(os.environ.get("OUTLINE_L1_MAX_CHAPTERS", "15"))
)
# ========== 2026-10-26：正文深层编号净化（遗留问题 1） ==========
# 问题：正文（sections.content）里的 Markdown 子标题由 AI 自带编号
# （如「### 3.1.2.1 施工准备」）。L1~L4 标题在导出端会被剥离并按目录树重新
# 编号，但 L5+（≥3 段小数的深层编号）按既定设计保留 AI 原文 —— AI 编号漂移
# 时（章节 3.1.2 的正文里写「3.2.1」）会与相邻 DB 子章节编号冲突，且只有发生
# 结构变更（改名/换序/移动）才会被净化。
#
# 本开关在「正文落库前」把这些深层标题的编号改写为「章节路径 + 相对序号」
# （3.1.2 下的第 1 个深层标题 → 3.1.2.1），保持「段数不变 → 解析层级不变」，
# 既消除冲突又不破坏导出端的标题识别（导出依赖编号前缀识别标题，直接剥号
# 会让标题退化为普通段落）。仅作用于 ≥3 段小数的标题；L4 及更浅标题仍由
# 导出端统一重编号，行为完全不变。
# 默认 true（新写入生效，存量正文不动）；设为 false 可回退到历史行为。
CONTENT_DEEP_HEADING_RENUMBER_ENABLED = (
    os.environ.get("CONTENT_DEEP_HEADING_RENUMBER_ENABLED", "true").lower() == "true"
)
# ---------------------------------------------------------------------------
# 2026-10-23 引入《标书智能体（5）——如何让弱模型也能稳定输出复杂 JSON》：
# 目录业务校验规则配置化（外部标准核心认知「合法 JSON ≠ 可用 JSON」——
# json.loads 成功只代表语法合法，结构/数量/深度不达契约的 JSON 必须判负并
# 进入定向修复链，而不是把「只有两级目录」这类半成品当可用结果交付）。
#
# 默认值与本软件生成契约严格一致（outline_common.generate_nodes_distribution：
# 每个一级下二级 3~5、每个二级下三级 5~10；_ensure_min_children
# (min_level2=3, min_level3=5)），因此默认开启不改变合规产物的通过率，
# 只收紧「半成品目录」出口。每项均可独立关闭（0/false），便于按模型能力
# 回退到历史宽松口径（历史规则：仅查标题非空/编号前缀/同级重复/L2≥2/L3≥2）。
# ---------------------------------------------------------------------------
# 业务校验总开关。false 时 validate_outline_business 退化为历史规则。
OUTLINE_BUSINESS_VALIDATION_ENABLED = (
    os.environ.get("OUTLINE_BUSINESS_VALIDATION_ENABLED", "true").lower() == "true"
)
# 完整目录最小深度（外部标准 _validate_complete_outline：_outline_depth < 3 判负）。
# 本软件目录树为三级（L1→L2→L3，L3 为正文最小叶子节点），默认 3；0=关闭。
OUTLINE_BUSINESS_MIN_DEPTH = max(
    0, int(os.environ.get("OUTLINE_BUSINESS_MIN_DEPTH", "3"))
)
# 每个一级下二级数量区间（对齐骨架契约 generate_nodes_distribution 的
# min_level2=3 浮动 0~2）。任一端设 0 表示不限制该端。
OUTLINE_BUSINESS_MIN_L2_PER_L1 = max(
    0, int(os.environ.get("OUTLINE_BUSINESS_MIN_L2_PER_L1", "3"))
)
OUTLINE_BUSINESS_MAX_L2_PER_L1 = max(
    0, int(os.environ.get("OUTLINE_BUSINESS_MAX_L2_PER_L1", "5"))
)
# 每个二级下三级数量下限（对齐 _ensure_min_children(min_level3=5) 与 2026-09-04
# 用户定制「每个二级下三级应达 5~10 个」）。不设上限——与自检提示词
# 「只设下限，不设上限」的用户定制口径一致，数量溢出由后处理与门禁负责。
OUTLINE_BUSINESS_MIN_L3_PER_L2 = max(
    0, int(os.environ.get("OUTLINE_BUSINESS_MIN_L3_PER_L2", "5"))
)
# ---------------------------------------------------------------------------
# 2026-10-20 引入易标 Aligned/Free 双模式目录生成（任务规格二.2）：
#   - "free"（默认）：现状行为——AI 根据评分要求生成 L1，完全向后兼容；
#   - "aligned"：L1 由程序按评分项/专项方案类型直接构造（锁定顺序，
#     节点回填 source_requirement_id/content_mode），模型只生成 L2/L3；
#     评分项不可解析时自动回落 AI 生成，不阻断链路。
# 非法值告警并回落 "free"（与 CONTENT_GENERATION_MODE 同一防御口径）。
OUTLINE_ALIGNED_MODE = os.environ.get("OUTLINE_ALIGNED_MODE", "free").strip().lower()
if OUTLINE_ALIGNED_MODE not in ("free", "aligned"):
    # 注意：本位置早于模块级 `_logger` 定义（后方 L470），局部导入避免 NameError
    import logging as _aligned_logging
    _aligned_logging.getLogger(__name__).warning(
        "OUTLINE_ALIGNED_MODE=%r 非法（应为 free/aligned），已回落为 free",
        OUTLINE_ALIGNED_MODE,
    )
    OUTLINE_ALIGNED_MODE = "free"
# 目录库/知识库参考条目截断长度配置化（2026-10-20 易标机制引入规格二.5：
# 「参考条目的截断长度可配置，防止上下文溢出」）。默认值 = 历史硬编码值，
# 不改变现有行为；非法值告警并回落默认。
def _env_int_positive(name: str, default: int) -> int:
    """读取正整数环境变量，非法/非正时回落默认值（导入期即校验，带告警）。"""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        import logging as _ei_logging
        _ei_logging.getLogger(__name__).warning("%s=%r 非整数，已回落默认 %d", name, raw, default)
        return default
    if val <= 0:
        import logging as _ei_logging
        _ei_logging.getLogger(__name__).warning("%s=%d 应为正数，已回落默认 %d", name, val, default)
        return default
    return val


# 知识素材目录（编排提示词）单条标题/摘要截断长度（原硬编码 [:60]/[:120]）
KNOWLEDGE_CATALOG_TITLE_MAX = _env_int_positive("KNOWLEDGE_CATALOG_TITLE_MAX", 60)
KNOWLEDGE_CATALOG_SUMMARY_MAX = _env_int_positive("KNOWLEDGE_CATALOG_SUMMARY_MAX", 120)
# 目录库参考模板 JSON 注入 L1 提示词的最大字符数（原硬编码 [:3000]）
OUTLINE_REFERENCE_JSON_MAX_CHARS = _env_int_positive("OUTLINE_REFERENCE_JSON_MAX_CHARS", 3000)
# ---------------------------------------------------------------------------
# 2026-10-20 易标机制引入规格三.7：长上下文降级策略。
# 易标在消息总长超模型上下文 70% 时自动切 Agent 隔离环境；本软件无 Agent
# 运行时，等效降级 = 生成前度量消息总长，超阈值时程序化裁剪参考资料段
# （保留 system 铁律与章节定位信息），避免请求整条失败。
#   - CONTENT_CONTEXT_LIMIT_CHARS：模型上下文字符容量估算（默认 120k 字，
#     约合中英混合 6-8 万 token）；
#   - CONTENT_CONTEXT_DEGRADE_RATIO：触发裁剪的占比阈值（默认 0.7，同易标）。
# CONTENT_CONTEXT_DEGRADE_ENABLED 默认关闭：保持现有行为，验证后再灰度开启。
CONTENT_CONTEXT_DEGRADE_ENABLED = _env_bool("CONTENT_CONTEXT_DEGRADE_ENABLED", "false")
CONTENT_CONTEXT_LIMIT_CHARS = _env_int_positive("CONTENT_CONTEXT_LIMIT_CHARS", 120000)
CONTENT_CONTEXT_DEGRADE_RATIO = float(os.environ.get("CONTENT_CONTEXT_DEGRADE_RATIO", "0.7"))
if not (0 < CONTENT_CONTEXT_DEGRADE_RATIO <= 1):
    CONTENT_CONTEXT_DEGRADE_RATIO = 0.7

# 同一个三级标题在**同一个一级章内**最多允许出现在几个不同二级父级下
# （2026-10-19 新增，修复「换了个 L2 标题，L3 标题完全照抄」的死循环重复形态）。
# 背景：`OUTLINE_L3_MAX_PARENT_REUSE` 按全树不同父级实例计数、上限 3——
# 「同一章内 3 个 L2 各挂同一批 L3」恰好卡在线内不触发（生产事故实录形态）。
# 同章内跨 L2 复用比跨章复用更刺眼（读者同屏对照即穿帮），故单独设更严上限。
# 设为 0 表示关闭该护栏（向后兼容）。
OUTLINE_L3_INTRA_MAX_PARENTS = max(
    0, int(os.environ.get("OUTLINE_L3_INTRA_MAX_PARENTS", "2"))
)
# 三级标题「泛化词干 × 维度词」组合识别开关（2026-10-15 第五轮新增）。
# 背景：前四轮治理已拦住「专项6」「总体实施策划（2）」两个原始家族，但同一
# 症状换了三副新面孔（复现见 backend/scripts/diag_l3_leaks.py 的 D1/D2/D3，
# 回归见 backend/tests/test_l3_generic_stem_guard_2026_10_15.py）：
#   1) `_title_entity_fragments` 从章名切出的「本章专属实体」未过滤泛化词段，
#      章名本身泛化（「总体方案与实施策划」「资源配置与协调保障」）时，
#      「总体方案」「实施策划」「资源配置」「协调保障」被当成实体与维度词拼接，
#      产出「总体方案施工工艺」「实施策划质量控制」——语义含量为零；
#   2) `_disambiguate_title` 的优先级 1 给泛化主体追加维度词，产出
#      「总体实施策划专项方案/补充措施/实施方案/保障措施」；
#   3) 以上两类标题都逃过既有脏判定（`_GENERIC_ONLY_TITLES` 按"恰好等于"比对），
#      导致 2026-10-15 的「消歧产物脏复检」形同虚设，全部绿灯放行到落库。
# 判据：标题恰好 = 泛化词干 + 泛化维度词，且不含任何项目实体。
# 设为 false 完全回退到历史行为（向后兼容，供 A/B 对比或定位问题时使用）。
OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED = (
    os.environ.get("OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED", "true").lower() == "true"
)
# 三级标题 AI 修复的「有效修复配额」上限（2026-10-15 第五轮新增）。
# 背景：`repair_l3_generic_titles` 旧实现先 `warnings[:max_repair]` 切片、
# 再在循环里 `continue` 跳过空标题节点 —— 被跳过的条目仍消耗配额，
# 实际修复数显著小于 max_repair，靠后位置的脏标题永远进不了任务队列。
# 现改为「凑满 max_repair 个可修复任务为止」，配额按可执行任务计。
# 本配置项仅作为该口径的显式开关（true=新口径，false=历史截断口径），
# 同时仍受 OUTLINE_L3_REPAIR_MAX 约束。
OUTLINE_L3_REPAIR_QUOTA_BY_TASK = (
    os.environ.get("OUTLINE_L3_REPAIR_QUOTA_BY_TASK", "true").lower() == "true"
)

# ========== 2026-10-19：目录生成工作流程优化（并发/局部自检/质量评分） ==========
# 对应《目录生成工作流程优化方案》优化 1 / 3 / 6 / 11。
#
# L2 并发生成并发上限（优化 1）。0 = 按 provider 自动（anthropic 16 / 其他 32），
# 保持历史行为；>0 时强制使用该值，便于按模型速率限制调优（方案推荐 3-5，
# 生产默认自动值远高于该推荐区间，是因为 provider 侧速率普遍宽裕）。
OUTLINE_L2_CONCURRENCY = max(0, int(os.environ.get("OUTLINE_L2_CONCURRENCY", "0")))

# ── 2026-10-23（P0，生产实证 project=1871130 21:47~21:56）─────────────────
# L2 失败章节补偿重跑轮数。生产日志里 `_generate_level2_v51` 首轮 8/15、
# 次轮 13/15 章节因 429 + 超时失败，全部直接落「默认目录」——用户拿到的是
# 与本项目无关的模板目录，且后台只留一行 WARNING，前端完全无感。
# 根因之一是首轮一次性把 N 章并发打出去，provider 侧 429 后 rate_gate 才把
# 并发从 4 砍到 1，已发出的请求收不回来，集体撞上 20s/60s 超时预算。
# 补偿策略：首轮失败后，用**低并发 + 全新预算**串行补跑失败章节，
# 不再与首轮的 N 路并发争抢额度。0 = 关闭补偿（保持 2026-10-23 前的行为）。
OUTLINE_L2_RETRY_FAILED_ROUNDS = max(
    0, int(os.environ.get("OUTLINE_L2_RETRY_FAILED_ROUNDS", "1"))
)
# 补偿轮的并发上限。刻意取小值：补偿轮的目标是"救回质量"而非"抢速度"，
# 高并发只会重演首轮的 429 雪崩。
OUTLINE_L2_RETRY_CONCURRENCY = max(
    1, int(os.environ.get("OUTLINE_L2_RETRY_CONCURRENCY", "2"))
)
# 降级告警阈值：兜底（默认目录）章节占比超过该比例时，除 WARNING 外额外打一条
# 带 error_code 的 ERROR，使监控系统能直接按错误码聚合"目录质量退化"事件。
OUTLINE_DEGRADED_ALERT_RATIO = min(
    1.0, max(0.0, float(os.environ.get("OUTLINE_DEGRADED_ALERT_RATIO", "0.3")))
)

# 质量评分报告总开关（优化 6）。关闭后 `_self_check_v51` 不计算评分，
# verdict 不含 quality_score，前端完成卡片不展示报告（向后兼容）。
OUTLINE_QUALITY_SCORE_ENABLED = (
    os.environ.get("OUTLINE_QUALITY_SCORE_ENABLED", "true").lower() == "true"
)

# AI 局部自检（优化 3）。开启后：有确定性问题时只把「疑似冲突区域」的最小
# 上下文子树（含全部一级章节标题 + 冲突章完整展开 + 无冲突章仅留标题）送给 AI，
# 而非全量目录树 —— 实测冲突占比通常 <20%，Token 消耗下降约 70%。
# 无确定性问题时的全量审查不受影响（本就不该为无问题目录付费）。
OUTLINE_AI_LOCAL_REVIEW_ENABLED = (
    os.environ.get("OUTLINE_AI_LOCAL_REVIEW_ENABLED", "true").lower() == "true"
)

# 局部自检的焦点节点上限（优化 3 护栏）。疑似冲突节点数超过该值时，切片体积
# 可能接近甚至超过全量送审，此时自动回退全量，保证 AI 判断力不受损。
OUTLINE_AI_LOCAL_REVIEW_MAX_NODES = max(
    1, int(os.environ.get("OUTLINE_AI_LOCAL_REVIEW_MAX_NODES", "120"))
)



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

# ──────────────────────────────────────────────────────────────────────
# repair_mermaid 统计（2026-10-11 图表增强）
# ──────────────────────────────────────────────────────────────────────
# type mismatch 高频判定阈值：同类型 AI 误输出累计次数 ≥ 此值算高频，
# 触发 chart_data_fix prompt 动态注入警告。调低更敏感、调高更宽松。
REPAIR_MISMATCH_THRESHOLD = int(os.environ.get("REPAIR_MISMATCH_THRESHOLD", "3"))

# mismatch case 环形 buffer 最大条数（先进先出）
REPAIR_MISMATCH_CASE_BUFFER = int(os.environ.get("REPAIR_MISMATCH_CASE_BUFFER", "200"))

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


# ========== 标书智能体（6）图文控制对齐（2026-10-23）==========
# 对齐外部引用软件《超长文本生成和图文控制》的核心机制：
#   预编排（正文生成前 AI 提名配图素材）→ 正文只写正文 → 全部正文完成后统一配图。
# 三项开关相互独立，默认值均保持「不改变既有行为」之外的最小风险组合。
#
# 1) CONTENT_ILLUSTRATION_PHASE_ENABLED
#    开启「配图阶段」：启用内容编排（use_orchestration）时，正文改用
#    CONTENT_SECTION_SYSTEM_PLANNED（严禁输出图表代码块），全部正文生成完成后
#    由 illustration_phase 统一校验/修复/插入图表围栏（失败隔离，不影响正文）。
#    关闭：完全回退到「正文自带图表围栏」的既有链路（零行为变更）。
#    依赖：仅在 use_orchestration=true 时生效（编排关闭时无 plan 可执行）。
CONTENT_ILLUSTRATION_PHASE_ENABLED = os.environ.get(
    "CONTENT_ILLUSTRATION_PHASE_ENABLED", "true"
).lower() == "true"

# 2) CONTENT_ILLUSTRATION_MAX_CHARTS
#    全文配图硬上限（mermaid + chart + image 合计），0 = 不限。
#    对应外部文档「AI 提名、程序拍板」的全局择优：候选多于额度时按段择优。
CONTENT_ILLUSTRATION_MAX_CHARTS = max(
    0, int(os.environ.get("CONTENT_ILLUSTRATION_MAX_CHARTS", "12"))
)

# 3) ORCHESTRATION_PLAN_PAYLOAD_ENABLED
#    编排阶段是否要求 AI 直接产出可执行素材（mermaid.code / chart.data / image.prompt）。
#    开启后配图阶段可直接使用 plan 里的素材（外部文档口径：配图不是事后临时加，
#    而是正文生成前就规划好）；关闭时只保留 needed/priority 判定，配图阶段回落到
#    既有「按章节内容生成图表」链路。
ORCHESTRATION_PLAN_PAYLOAD_ENABLED = os.environ.get(
    "ORCHESTRATION_PLAN_PAYLOAD_ENABLED", "true"
).lower() == "true"

# 4) ORCHESTRATION_CHART_ENABLED
#    编排阶段是否允许提名「数据型图表」（chart 块：甘特/架构/劳动力/对比/布置/时间线）。
ORCHESTRATION_CHART_ENABLED = os.environ.get(
    "ORCHESTRATION_CHART_ENABLED", "true"
).lower() == "true"

# ========== 遗留项 2026-10-08: 正文普通代码块剥离开关 ==========
# 控制正文清洗（clean_generated_content）对「闭合」普通代码块（```python / ```json /
# ```text 等，不含 mermaid/chart-json 图表围栏）的处理：
#   false（默认）：保留块内内容为正文段落，仅剥离围栏标记行 —— 与 2026-10-06 之前
#     的既有行为完全一致（回归保护见 tests/test_unclosed_fence_boundary_2026_10_08.py::
#     test_closed_plain_fence_untouched）。
#   true：闭合普通代码块连同标记行与内容整体剔除 —— 适用于标书正文不允许出现
#     代码片段的业务场景（与「未闭合坏块一律删除」的口径统一）。
# 向后兼容：默认 false，不改变任何既有输出；需剥离时设置环境变量
# STRIP_PLAIN_CODE_BLOCKS=true（运行时读取，改动后重启生效）。
STRIP_PLAIN_CODE_BLOCKS = os.environ.get("STRIP_PLAIN_CODE_BLOCKS", "false").lower() == "true"


# ========== 遗留项 2026-10-15（第五轮）: 续写接缝去重的最小重叠长度 ==========
# 控制正文续写（_auto_continue_on_length）在拼接两轮输出时对「接缝重复」的剥离口径：
#   - 完全重叠（extra 整体都落在上一轮正文尾部窗口内）：**无条件剥离**，不受本
#     阈值影响——此时 AI 没有写出任何新内容，保留只是把同一段话重复一遍。
#   - 部分重叠：仅当重叠长度 >= 本值时才剥离。
# 背景（Risk-P2c 2026-10-15）：旧实现从最长一路探测到 k=1，任何 1~2 字的巧合
# 都被当作「截断续写」剥掉，例如
#     final="本阶段已完成采购" + extra="采购的时间节点如下："  →  剥掉「采购」
# 留下「的时间节点如下：」，主语丢失、句子语义断裂；中文 2 字词（采购/完成/阶段/
# 体系/质量/安全）在相邻段落高频同现，误剥概率很高。
# 权衡：阈值过高会漏掉真实重叠（多出一小段重复文字，属可接受的表观瑕疵）；
# 阈值过低会砍掉句子主语（文本损坏）。故取 6 作为默认下限。
# 向后兼容：设为 0 完全回退到历史行为（k=1 起逐字探测）。
SEAM_OVERLAP_MIN_CHARS = max(0, int(os.environ.get("SEAM_OVERLAP_MIN_CHARS", "6")))


# ========== 遗留项 2026-10-15（第五轮）: 零宽字符清理开关 ==========
# 控制正文清洗（clean_generated_content）是否全局剔除零宽字符：
#   U+200B ZERO WIDTH SPACE / U+200C NON-JOINER / U+200D JOINER /
#   U+2060 WORD JOINER / U+180E MONGOLIAN VOWEL SEPARATOR / U+FEFF BOM·ZWNBSP。
# 背景（Risk-P2d 2026-10-15）：Python re 的 \s 不匹配 U+200B（Unicode 类别 Cf，
# 非空白），零宽字符夹在标题/列表编号里时会让所有 ^(\d{1,4})\s*... 锚点正则
# 失配，各种编号形态一律被降级成普通段落（导出丢标题样式与编号）：
#   「1\u200b、施工部署」  应为 heading3      → 实际 paragraph
#   「1\u200b）施工准备」  应为 heading6      → 实际 paragraph
#   「（1\u200b）安全交底」应为 list_ordered  → 实际 paragraph
# （注：「1.施工准备」在本模块本就定义为 heading3，与零宽字符无关。）
# 这类字符常见于复制粘贴与富文本编辑器。
# 向后兼容：默认 true（修复行为）；设 STRIP_ZERO_WIDTH_CHARS=false 可完全回退
# 到历史行为。不清理变体选择符 U+FE0E/U+FE0F（影响 emoji 呈现）与数学不可见符
# U+2062~U+2064（影响公式语义）。
STRIP_ZERO_WIDTH_CHARS = os.environ.get("STRIP_ZERO_WIDTH_CHARS", "true").lower() == "true"



# ========== 增强: 一致性自动修复（autofix）防护口径 ==========
# BUG-FIX 2026-10-09（P1·autofix 过度替换污染正文）：
# `apply_autofix` 此前把待统一值在**全文所有章节**里的所有出现位置一律替换。
# 事实取值多为「30」「1」这类极短串，数字边界保护挡不住「钢筋直径 30 mm」、
# 「本项目划分为 1 个标段」这些与事实无关的数字 —— 实测一次 autofix 会在无关
# 章节改出 4 处错误（钢筋直径、构件数量、批次号被改成工期/人数），而冲突却因
# 「替换成功」被标为已解决，正文被静默污染。
# 默认口径：只扫冲突来源章节 + 要求取值就近出现事实标签词（详见
# `consistency_rules.fact_has_keyword_nearby`）。

# 是否把替换范围限定在「冲突来源章节」内。
#   true（默认）：只改 `sources_json` 中 `section_<id>` 指向的章节 —— 修复范围
#     与审计证据严格一致，不会波及其他章节；
#   false：回退历史行为（全文所有章节）—— 仅在来源信息缺失（旧数据只存了
#     摘要/基准来源）导致「无来源章节」时才需要。
# 向后兼容：默认值只收紧范围，不改变响应体字段名与语义；接口签名不变。
CONSISTENCY_AUTOFIX_SCOPE_TO_SOURCE_SECTIONS = os.environ.get(
    "CONSISTENCY_AUTOFIX_SCOPE_TO_SOURCE_SECTIONS", "true"
).lower() == "true"

# 是否启用「事实标签词就近校验」。
#   true（默认）：取值出现位置前向 AUTOFIX_KEYWORD_WINDOW_BEFORE、后向
#     AUTOFIX_KEYWORD_WINDOW_AFTER 字符内必须出现该事实的标签词
#     （工期→工期/周期/天数，金额→金额/造价/预算/报价/合同价…）才允许替换；
#     人员类事实改走「取值后必须紧跟人数单位「人」」的结构校验；
#   false：只做数字边界保护（历史行为）。
# 无标签词定义的事实项（未来新增且未登记 FACT_KEYWORDS 的事实）不受此开关影响，
# 自动回退到原有行为 —— 保证「宁多报不漏报」的既有承诺不被破坏。
CONSISTENCY_AUTOFIX_REQUIRE_FACT_KEYWORD = os.environ.get(
    "CONSISTENCY_AUTOFIX_REQUIRE_FACT_KEYWORD", "true"
).lower() == "true"

# 标签词就近校验的**非对称**搜索半径（前向 / 后向，单位：字符）。
# BUG-FIX 2026-10-09（P1 续·同章内无关短数字被误改）：原先取值**左右各 40 字符**
# 都算「附近」，于是「总工期 30 天，其中钢筋进场 30 批」里的第二个 30 —— 「工期」
# 距离它 14 个字 —— 也被判成工期取值改成了 45。中文习惯是**标签词在数值之前**
# （「工期 30 天」），因此前向窗口收紧、后向窗口更窄（后向仅覆盖「30 天工期」
# 这种倒装），实测足以拦掉上述误改且不影响既有正确用例。
# 需要放宽时用环境变量调整，例如超长句里标签词与数值隔 20 字。
CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE = int(
    os.environ.get("CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE", "12")
)
CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_AFTER = int(
    os.environ.get("CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_AFTER", "6")
)

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


# ==================================================================
# 2026-10-12 引入：OpenBidKit 易标 2026-09-14 方法论第二批移植
# 说明：本段配置项均默认关闭或保持现有行为，仅新增能力开关。
# 参考软件：J:\编程\OpenBidKit 易标\OpenBidKit_Yibiao-main-2026-09-14
# 引入项编号 P0/P1/P2/E 见 deliverables/报告_平台软件架构分析与OpenBidKit差距对照_2026-10-12.md
# ==================================================================

# ========== P0-1：目录编号模板化 ==========
# 易标 outlineNumbering.ts 支持 {zh}/{num}/{tail}/{circled}/{alpha}/{roman}
# 占位符模板。此前本平台 outline_gate.renumber_outline_ids 只支持位置式
# 点号编号（1/1.1/1.1.1），无中文数字/圈号/罗马数字/字母模板。
# OUTLINE_NUMBERING_ENABLED=false 保持现有行为，避免任何目录变更。
OUTLINE_NUMBERING_ENABLED = os.environ.get("OUTLINE_NUMBERING_ENABLED", "false").lower() == "true"

# 编号模板：level -> template；level 为 1-based（1/2/3），未列出的 level 走 _DEFAULT
# 可用占位符：{zh} 中文数字、{num} 阿拉伯数字、{tail} 分隔符、{circled} 圈号、
# {alpha} 字母、{roman} 罗马数字。示例：
#   {"1": "第{num}章", "2": "{num}.{num}", "3": "{num}.{num}.{num}"}
#   {"1": "第{zh}章", "2": "{circled}{num}", "3": "（{zh}）"}
OUTLINE_NUMBERING_TEMPLATE = os.environ.get(
    "OUTLINE_NUMBERING_TEMPLATE",
    '{"1": "第{num}章", "2": "{num:1}.{num:2}", "3": "{num:1}.{num:2}.{num:3}"}',
)

# ========== P0-2：目录对齐模式（一一对应锁死评分项）==========
# 易标 outlineGenerationTaskV2 「aligned」模式：从技术评分要求提取评分大类，
# 程序构造 L1，锁死标题顺序与评分项关联，模型只生成 L2/L3。
# 平台此前 L1 完全由 AI 生成，无法保证与评分项一一对应。
#
# BUG-FIX 2026-10-20（死配置清理）：原 `ALIGN_OUTLINE_TO_SCORING` 开关自
# 2026-10-12 实现起没有任何生产消费方（运维把它设 true 也不会产生对齐行为），
# 对齐能力的正式入口是下面的 OUTLINE_ALIGNED_MODE（aligned/free 双模式），
# 已删除该死开关；如确需独立关闭对齐，请使用 OUTLINE_ALIGNED_MODE=free。

# 目录生成模式选择器
#   'standalone-technical'：独立技术方案模式（默认，与现有行为一致）
#   'aligned'：一一对应模式（L1 从评分项锁定）
#   'response-file'：应标文件模式（L1 从评分项锁定，L2/L3 强调响应性）
OUTLINE_MODE_DEFAULT = os.environ.get(
    "OUTLINE_MODE_DEFAULT", "standalone-technical"
)

# ========== P0-3：正文提示词分层消息栈 ==========
# 易标「提示词顺序优化」：把大且稳定的上下文放前面，任务差异放最后，
# 实测缓存命中后输入成本从 0.0058 降到 0.00067，降低约 10 倍。
# SPLIT_PROMPT_LAYERS=false 保持现有 build_chapter_content_messages 行为；
# =true 时启用 8 层消息栈（system/项目概述/全局事实/参考素材/编排/上级/同级/当前）。
SPLIT_PROMPT_LAYERS = os.environ.get("SPLIT_PROMPT_LAYERS", "false").lower() == "true"

# ========== 标书智能体（4）提示词顺序优化与缓存命中（2026-10-23）==========
# 外部引用标准四条原则：①system 只放稳定规则；②最大最稳定上下文尽量前置；
# ③任务差异尽量放最后；④同一份数据的组织格式保持稳定（不塞时间戳/随机数）。
# 以下开关默认全部保持「改造前的提示词内容」，只改消息组织顺序与格式稳定化，
# 生成质量不变、成本可降；每项均可独立回退，向后兼容。

# 缓存可观测性：按任务类型累计 prompt/cached token 并计算命中率，
# 暴露于 /api/v1/ai/cache-metrics 的 task_types 字段。
AI_CACHE_METRICS_ENABLED = (
    os.environ.get("AI_CACHE_METRICS_ENABLED", "true").lower() == "true"
)
# 单次 AI 调用的缓存计量日志（DEBUG 级；正文生成每项目 50+ 次，不开 INFO 免淹没）。
AI_CACHE_LOG_ENABLED = (
    os.environ.get("AI_CACHE_LOG_ENABLED", "true").lower() == "true"
)
# 命中率告警阈值（0~1）。0=关闭（默认，与历史行为一致）；如 0.3 表示某任务
# 累计命中率低于 30% 且样本足够时告警——稳定前缀被破坏的信号。
AI_CACHE_HIT_RATE_ALERT_THRESHOLD = float(
    os.environ.get("AI_CACHE_HIT_RATE_ALERT_THRESHOLD", "0")
)
# 触发告警所需的最小请求数（避免冷启动几条请求的噪声触发）。
AI_CACHE_HIT_RATE_MIN_SAMPLES = int(
    os.environ.get("AI_CACHE_HIT_RATE_MIN_SAMPLES", "20")
)
# 同一任务类型告警的最小间隔秒数（防止刷屏）。
AI_CACHE_ALERT_THROTTLE_SECONDS = int(
    os.environ.get("AI_CACHE_ALERT_THROTTLE_SECONDS", "600")
)

# 正文同级章节列表容量（含当前章节）。同一父章节下所有叶子共用同一个列表
# （按父章节统一截取），sibling_context 字节级一致 → 前缀可命中缓存；
# 任务差异消息里显式声明「请避免与除当前章节外的其他章节内容重复」。
# 默认 8 与历史口径一致（防 token 爆炸）。
CONTENT_SIBLING_MAX_ITEMS = int(os.environ.get("CONTENT_SIBLING_MAX_ITEMS", "8"))

# 审计7增强（2026-10-27）：同级章节超长时的「降描述保留标题」扩容上限。
# 同一父章节下孩子数超过 CONTENT_SIBLING_MAX_ITEMS 时，旧行为只取前 8 个 →
# 第 9 个及之后的章节不在自己的同级列表里（自定位/兄弟感知缺失）。
# 改为：超出时整表降级为仅标题（去 description），条数扩至本上限——
# 标题很短（≤20字），40 条 ≈ 与旧 8 条+描述的 token 量相当；
# 仍保持「按父章节共享同一列表」的缓存命中设计。
CONTENT_SIBLING_TITLES_MAX_ITEMS = int(
    os.environ.get("CONTENT_SIBLING_TITLES_MAX_ITEMS", "40")
)

# 各高频任务的提示词分层总开关（默认 true）。false 时该类任务退回
# 「单条 user 消息」的旧组织方式（内容一致，仅消息条数不同），可独立回退。
OUTLINE_PROMPT_LAYERS_ENABLED = (
    os.environ.get("OUTLINE_PROMPT_LAYERS_ENABLED", "true").lower() == "true"
)
ORCHESTRATION_PROMPT_LAYERS_ENABLED = (
    os.environ.get("ORCHESTRATION_PROMPT_LAYERS_ENABLED", "true").lower() == "true"
)
CHART_PROMPT_LAYERS_ENABLED = (
    os.environ.get("CHART_PROMPT_LAYERS_ENABLED", "true").lower() == "true"
)
QUALITY_PROMPT_LAYERS_ENABLED = (
    os.environ.get("QUALITY_PROMPT_LAYERS_ENABLED", "true").lower() == "true"
)

# ========== 提示词模块增强（2026-10-17）==========
# 以下开关默认全部保持「修复前的默认行为」，可独立开启，向后兼容。

# 提示词变量值默认脱敏（邮箱/手机/身份证/银行卡/JWT/口令/IP …）。
# PROMPT_DESENSITIZE_DEFAULT=false 保持既有「opt-in」语义：只有显式传
# desensitize=True 的调用方（如 workflows_analysis 解析链路）才脱敏。
# =true 时 get_prompt 全链路默认脱敏（安全性优先，可能掩码正文里的编号类信息）。
PROMPT_DESENSITIZE_DEFAULT = (
    os.environ.get("PROMPT_DESENSITIZE_DEFAULT", "false").lower() == "true"
)

# 提示词变量值默认元标记中和（{shared:NAME} / __VAR__）。
# PROMPT_SANITIZE_DEFAULT=false 保持既有行为，避免改变历史渲染语义。
PROMPT_SANITIZE_DEFAULT = (
    os.environ.get("PROMPT_SANITIZE_DEFAULT", "false").lower() == "true"
)

# 注入风险自动包裹阈值：detect_injection_risk 得分达到该值即用
# <<<USER_DATA_START/END>>> 包裹变量值。设为 0 表示始终包裹（会破坏需解析的
# JSON/Mermaid 载荷，不建议）；设为很大的值（如 999）等于关闭自动包裹。
PROMPT_INJECTION_WRAP_THRESHOLD = int(
    os.environ.get("PROMPT_INJECTION_WRAP_THRESHOLD", "3")
)

# 上下文组装默认 Token 预算上限。None/0 表示不限（保持既有行为）。
# 建议生产设为 8000~16000，避免超长招标原文撑爆上下文窗口。
PROMPT_CONTEXT_MAX_TOKENS = int(os.environ.get("PROMPT_CONTEXT_MAX_TOKENS", "0"))

# 提示词缓存加载失败后的冷却秒数（冷却期内直接走硬编码回退，避免 AI 热路径
# 被失败的 DB 查询拖垮）。默认 60 秒，与修复前一致。
PROMPT_CACHE_RETRY_INTERVAL = float(os.environ.get("PROMPT_CACHE_RETRY_INTERVAL", "60"))

# ========== P1-1：配图预编排 ==========
# 易标：正文生成前先全文级预编排配图（illustration-plan.json），
# 每章节规划好配图意图（kind/title/section_ids/priority），
# 正文生成时模型可提前呼应；配图数量按 maxAiImages 分段择优。
# PRE_PLAN_ILLUSTRATIONS=false 保持现有章节内嵌配图行为。
PRE_PLAN_ILLUSTRATIONS = os.environ.get(
    "PRE_PLAN_ILLUSTRATIONS", "false"
).lower() == "true"

# 配图规划单次送审的章节数上限（避免超长上下文）
ILLUSTRATION_PLAN_MAX_SECTIONS = int(
    os.environ.get("ILLUSTRATION_PLAN_MAX_SECTIONS", "20")
)

# ========== P1-2：Mermaid/HTML 修复轮次 ==========
# 易标：Mermaid 生成后若渲染失败，最多 3 轮 AI 修复；HTML 图溢出最多 2 轮修复。
# 平台此前默认为 1 轮。
MERMAID_REPAIR_ROUNDS = int(os.environ.get("MERMAID_REPAIR_ROUNDS", "3"))
HTML_LAYOUT_REPAIR_ROUNDS = int(os.environ.get("HTML_LAYOUT_REPAIR_ROUNDS", "2"))

# ========== P1-3：一致性修复严格唯一命中 ==========
# 易标 textEdit.cjs：applyTextEdit 要求 old_text 唯一命中才替换，
# 多命中或零命中都返回错误，避免追加到末尾造成静默污染。
# CONSISTENCY_UNIQUE_EDIT_STRICT=true（默认开启，属于行为收紧但更安全）。
CONSISTENCY_UNIQUE_EDIT_STRICT = os.environ.get(
    "CONSISTENCY_UNIQUE_EDIT_STRICT", "true"
).lower() == "true"

# ========== P1-4：目录/正文 JSON 修复循环 ==========
# 易标：AI 生成 JSON 失败后先尝试修复（而非直接抛错），最多 N 轮。
OUTLINE_JSON_REPAIR_ROUNDS = int(os.environ.get("OUTLINE_JSON_REPAIR_ROUNDS", "3"))
CONTENT_JSON_REPAIR_ROUNDS = int(os.environ.get("CONTENT_JSON_REPAIR_ROUNDS", "2"))

# ========== P2-1：Agent 修复工作区持久化 ==========
# 易标 D1：Agent 修复在持久工作区写 global-facts.md / technical-plan.md，
# 支持搜索关键词→分段读取→建立索引→多轮修改→程序校验回写。
# AGENT_REPAIR_PERSIST_WORKSPACE=false 保持现有 jsonl 日志行为。
AGENT_REPAIR_PERSIST_WORKSPACE = os.environ.get(
    "AGENT_REPAIR_PERSIST_WORKSPACE", "false"
).lower() == "true"

# Agent 修复工作区根目录（相对 DATA_DIR）
AGENT_REPAIR_WORKSPACE_SUBDIR = os.environ.get(
    "AGENT_REPAIR_WORKSPACE_SUBDIR", "agent_repair"
)

# ========== P2-2：知识库两轮提取 + 遗漏补漏 ==========
# 易标：第一轮一次性结构化提取，第二轮按目录树叶子节点逐个匹配找遗漏。
# KNOWLEDGE_REFILL_TWO_ROUND=false 保持单轮行为。
KNOWLEDGE_REFILL_TWO_ROUND = os.environ.get(
    "KNOWLEDGE_REFILL_TWO_ROUND", "false"
).lower() == "true"

# 第二轮补漏的分批大小（叶子节点分批送入匹配，避免超长上下文）
KNOWLEDGE_REFILL_BATCH_SIZE = int(
    os.environ.get("KNOWLEDGE_REFILL_BATCH_SIZE", "10")
)

# ========== E-3：正文生成顺序控制（2026-10-18）==========
# 正文生成模式（**系统默认值**，env 可覆盖；router/impl 的 Query 默认值均绑定本常量，
# 保证「后端默认、显式传参、运维覆盖」三者的唯一事实源）：
#   'batch'（默认，2026-10-18 第二轮起由 parallel 翻转）：按目录树顺序分批次生成，
#     同父节点章节同批并行、批次间串行 —— 上下文承接自然、全局事实调用稳定、
#     问题定位容易、缓存命中更高，且实际生成顺序与目录树顺序严格一致；
#   'serial'：逐节串行生成（最强上下文承接，最慢）；
#   'parallel'：asyncio.gather 一次性并发启动所有章节（旧行为，完成顺序不保证，
#     仅集合完整性受校验）。需要严格旧行为时可显式传 parallel 或置本 env。
CONTENT_GENERATION_MODE = os.environ.get("CONTENT_GENERATION_MODE", "batch")

# 导入时校验：env 误配（拼写错误/大小写）时告警并回落 batch，
# 避免把非法值当下_default 传给 router 的 pattern 校验导致 422 全拦截。
if CONTENT_GENERATION_MODE not in ("parallel", "batch", "serial"):
    _logger.warning(
        "CONTENT_GENERATION_MODE=%r 非法（应为 parallel/batch/serial），已回落为 batch",
        CONTENT_GENERATION_MODE,
    )
    CONTENT_GENERATION_MODE = "batch"

# batch 模式下每批次最大章节数（按目录树顺序切分，不超过此值）
CONTENT_BATCH_MAX_SECTIONS = int(os.environ.get("CONTENT_BATCH_MAX_SECTIONS", "4"))

# 是否注入前一节内容摘要作为上下文（仅 batch/serial 模式生效）
CONTENT_PREV_SECTION_CONTEXT = os.environ.get(
    "CONTENT_PREV_SECTION_CONTEXT", "true"
).lower() == "true"

# 是否启用生成后顺序校验（落库顺序与目录树顺序一致性检查）
CONTENT_ORDER_VERIFY = os.environ.get(
    "CONTENT_ORDER_VERIFY", "true"
).lower() == "true"

# 是否启用「章节→所需事实」显式映射优先注入（替代纯关键词打分筛选）
CONTENT_FACTS_EXPLICIT_MAP = os.environ.get(
    "CONTENT_FACTS_EXPLICIT_MAP", "true"
).lower() == "true"

# ========== E-3b：正文生成顺序增强（2026-10-18 第二轮）==========
# 目录树顺序排序：排序键为（path 前序, sort_order, id），只读不重排。
# CONTENT_TREE_SORT_STRICT=true 时，_build_leaf_dicts 对未按目录树顺序
# 排列的输入记 warning（默认开；关掉仅影响日志，不改变排序行为）。
CONTENT_TREE_SORT_STRICT = os.environ.get(
    "CONTENT_TREE_SORT_STRICT", "true"
).lower() == "true"

# batch/serial 模式下「批内失败按顺序重试」轮数（失败章节按目录树顺序
# 立即重试，不跳过后移；重试仍失败标记待人工处理并继续后续节点）。
# 0 = 关闭批内重试（回落到旧行为：失败章节留给 SSE 外层整批重试）。
CONTENT_BATCH_RETRY_ROUNDS = int(
    os.environ.get("CONTENT_BATCH_RETRY_ROUNDS", "2")
)

# 正文阶段危大工程识别与上下文注入：扫描招标文本命中危大工程关键词时，
# 向相关章节注入「必含章节/表格/图表、安全阈值、适用规范」判定参数。
# 默认开启；无法导入 hazard_projects 或扫描异常时自动跳过（不影响生成）。
CONTENT_HAZARD_SCAN = os.environ.get(
    "CONTENT_HAZARD_SCAN", "true"
).lower() == "true"

# 适用规范清单注入：从招标文本确定性抽取 GB/JGJ/CJJ 等标准编号，
# 注入稳定上下文层（同项目内稳定，不影响缓存前缀）。无命中则不注入。
CONTENT_STANDARDS_INJECT = os.environ.get(
    "CONTENT_STANDARDS_INJECT", "true"
).lower() == "true"

# 上下文注入校验：生成前审计消息栈（层级齐全/长度未超限/未被截断），
# 结果挂到章节结果 context_check 与生成后综合校验。默认开启。
CONTENT_CONTEXT_AUDIT = os.environ.get(
    "CONTENT_CONTEXT_AUDIT", "true"
).lower() == "true"

# 生成后全局事实调用校验（章节→所需事实映射表完整性 + 与全局事实冲突）。
# 默认开启；校验结果进入 _verification 与 SSE verification 事件。
CONTENT_FACT_VERIFY = os.environ.get(
    "CONTENT_FACT_VERIFY", "true"
).lower() == "true"

# ========== E-1：九大章节字段契约注入 ==========
# 已存在的 NINE_CHAPER_FIELDS 契约（content_field_contract.py:53-99）在此
# 增加注入开关：目录生成时把章节名映射到契约字段，正文生成时按字段注入。
NINE_CHAPTER_CONTRACT_INJECT = os.environ.get(
    "NINE_CHAPTER_CONTRACT_INJECT", "true"
).lower() == "true"

# ========== E-2：六大类危大工程覆盖 ==========
# 易标无此项，本平台独有竞争力。HAZARD_PROJECT_SCAN=true（默认开启）
# 时目录生成前扫描招标文件关键词，识别危大工程类型并强制加入专项章节。
HAZARD_PROJECT_SCAN = os.environ.get(
    "HAZARD_PROJECT_SCAN", "true"
).lower() == "true"

# 六大类危大工程：深基坑/高支模/脚手架/起重吊装/临时用电/消防保卫
# 详见 services/ai/hazard_projects.py 的 HAZARD_PROJECTS 常量。

