from pydantic import BaseModel, Field


class Project(BaseModel):
    id: int
    name: str
    industry: str = ""
    industry_id: str | None = (
        None  # 行业树节点ID（数据库列由迁移添加，Pydantic 模型此前缺失该字段）
    )
    bidder_name: str = ""
    notes: str = ""
    current_step: int = 1
    # BUG-FIX 2026-09-18（P1）：项目创建者用户ID（多用户隔离），旧数据为 NULL
    created_by: int | None = None
    created_at: str = ""
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    industry: str = ""
    industry_id: str | None = None  # 行业树节点ID
    bidder_name: str = ""
    notes: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    industry_id: str | None = None  # 行业树节点ID
    bidder_name: str | None = None
    notes: str | None = None
    current_step: int | None = None


class ProjectList(BaseModel):
    total: int
    items: list[Project]


class AIMessage(BaseModel):
    role: str
    content: str


class FileInfo(BaseModel):
    id: int
    filename: str
    file_type: str
    file_role: str
    status: str
    message: str
    created_at: str

    model_config = {"from_attributes": True}


class GlobalFact(BaseModel):
    """全局事实条目（写入 analysis.global_facts_json 的扁平列表元素）。

    BUG-FIX 2026-09-29（前后端字段契约）：本模型此前没有 `needs_manual_input`，
    而 /facts/generate 在 placeholder 策略下会逐条打该标、前端据此渲染
    「待人工填写」角标。后果：用户只要手动保存一次事实（POST /facts 用本模型
    校形并 model_dump 重生 JSON），Pydantic 默认丢弃额外字段 → 标记从库里静默消失，
    刷新后【待填写】看起来像正常事实被确认并进正文。现补齐该字段。
    """

    key: str
    label: str = ""
    value: str = ""
    category: str = "basic"
    source: str = "ai"
    verified: bool = False
    needs_manual_input: bool = False


class GlobalFactList(BaseModel):
    items: list[GlobalFact]


class FactsGenerateRequest(BaseModel):
    """全局事实提取参数。

    ENHANCE 2026-09-15：`missing_value_policy` 控制「资料没给具体值」时的处理方式，
    移植自易标 globalFactsTask 的三档策略：
      - fabricate  ：可模拟生成合理、稳定、不冲突的值（历史默认行为）
      - omit       ：不编造，改为不含具体值的笼统承诺（"按招标文件及合同约定执行"）
      - placeholder：不编造，value 写「【待填写】」交人工补齐
    """

    missing_value_policy: str = "fabricate"


class Section(BaseModel):
    id: int
    project_id: int
    parent_id: int | None = None
    title: str
    level: int = 1
    sort_order: int = 0
    section_type: str = "text"
    scoring_point_id: str | None = None
    # 2026-10-20 遗留 3/5：Aligned 程序构造的一级目录落库后保留锁定/正文模式
    # （此前仅存在于内存树，刷新即丢失）。locked 仅保护结构字段。
    locked: bool = False
    content_mode: str | None = None
    ai_prompt_snapshot: str | None = None
    flowchart_json: str | None = None
    gantt_json: str | None = None
    architecture_json: str | None = None
    labor_json: str | None = None
    comparison_json: str | None = None
    layout_json: str | None = None
    timeline_json: str | None = None
    inlined_chart_json: str | None = None
    illustration_type: str | None = None
    illustration_data: str | None = None
    content: str = ""
    target_words: int | None = None
    # 2026-10-08 新增：字数档位（brief/standard/detailed/long）
    # 用于三级目录（叶子节点）的字数控制；None 表示跟随全局默认档位
    word_count_tier: str | None = None
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""
    word_count: int = 0

    model_config = {"from_attributes": True}


# SECTION_EXPORT_COLUMNS: _batch_load_section_details 使用的列列表
# 包含导出 DOCX 所需的所有字段，避免 SELECT * 带来的不必要开销
SECTION_EXPORT_COLUMNS = (
    "id, project_id, parent_id, title, level, sort_order, content, "
    "flowchart_json, gantt_json, architecture_json, labor_json, "
    "comparison_json, layout_json, timeline_json, inlined_chart_json"
)


class SectionCreate(BaseModel):
    parent_id: int | None = None
    title: str = Field(..., min_length=1, max_length=500)
    # BUG-FIX 2026-09-29：level/sort_order 改为可选，缺省时由服务层按父子关系/兄弟序
    # 自动推导。此前默认 level=1、sort_order=0，前端「添加子目录」固定传 level=2、
    # sort_order 不传，导致多级编号错位、新章节排序跳到最前。显式传值仍按传入值入库
    # （向后兼容批量导入等既有调用方）。
    level: int | None = None
    sort_order: int | None = None
    section_type: str = "text"
    scoring_point_id: str | None = None
    # 2026-10-08 新增：字数档位（brief/standard/detailed/long）；None 表示跟随全局默认
    word_count_tier: str | None = None
    target_words: int | None = None


class SectionUpdate(BaseModel):
    title: str | None = None
    sort_order: int | None = None
    parent_id: int | None = None
    content: str | None = None
    target_words: int | None = None
    # 2026-10-08 新增：字数档位覆盖；None 表示清除（跟随全局默认）
    word_count_tier: str | None = None
    status: str | None = None
    section_type: str | None = None
    scoring_point_id: str | None = None
    ai_prompt_snapshot: str | None = None
    flowchart_json: str | None = None
    gantt_json: str | None = None
    architecture_json: str | None = None
    labor_json: str | None = None
    comparison_json: str | None = None
    layout_json: str | None = None
    timeline_json: str | None = None
    inlined_chart_json: str | None = None
    illustration_type: str | None = None
    illustration_data: str | None = None


class SectionTreeNode(BaseModel):
    id: int
    title: str
    level: int
    sort_order: int
    section_type: str
    scoring_point_id: str | None = None
    # 2026-10-20 遗留 3/5：前端读取 locked 后禁编辑锁定章节的结构；
    # content_mode 透出供正文编辑页展示"生成模式"。
    locked: bool = False
    content_mode: str | None = None
    target_words: int | None = None
    # 2026-10-08 新增：字数档位相关字段（仅三级目录叶子有意义）
    word_count_tier: str | None = None
    tier_label: str | None = None
    tier_target: int | None = None
    # 只读统计字段：L1/L2 显示其子树所有 L3 的字数累计
    cumulative_word_count: int = 0
    status: str
    created_at: str | None = None
    children: list["SectionTreeNode"] = []
    has_content: bool = False
    word_count: int = 0
    chart_flags: list[str] = []
    graph_suitable: bool = False
    suggested_chart: str = ""


class ExportConfig(BaseModel):
    font_title: str = "宋体"
    font_body: str = "宋体"
    size_title_1: float = 14
    size_title_2: float = 14
    size_title_3: float = 14
    size_title_sub: float = 12
    size_body: float = 12
    line_spacing: float = 1.5
    show_title_page: bool = True
    show_toc: bool = True
    show_header_footer: bool = True
    bidder_name: str = ""
    image_max_width_cm: float = 18.0  # 优化：从 15.5cm 提升至 18cm，图表更宽更清晰
    image_dpi: int = 300  # 确保 300 DPI 高分辨率打印质量
    gantt_max_columns: int = 18
    # V1.0 增强：严格模式开关，开启后预检 warning 升级为 error
    strict_mode: bool = False
    # ENHANCE（本轮跨模块深度探索·审核×导出打通）：一致性校核结果接入导出闸门。
    # True 时，check_document 增加「未解决的高严重度一致性冲突」判据
    # （consistency_conflicts 中 resolved=0 且 severity='high' 的规则/AI 冲突），
    # 命中即阻断导出并提示先去一致性校核页处理。
    # 默认 False：保持历史行为（审计结果与导出解耦），旧调用方零影响；
    # 由前端导出配置页暴露开关，用户显式勾选后才生效。
    block_on_high_conflicts: bool = False
    # D5 加固（2026-10-07）：图表标题兜底补图开关。开启后，章节无图表数据/无图表标记时，
    # 按标题本地模板自动补图（仅命中强关键词的图表相关章节）。默认关闭以保持历史行为；
    # 亦可由环境变量 AUTO_FALLBACK_CHARTS=1 全局开启。
    auto_fallback_charts: bool = False
    # L-6（2026-10-17）：图题编号与标题间分隔符。默认空格（历史口径
    # 「图3-1 施工进度计划」）；招标规范要求冒号时传 "：" 得到
    # 「图3-1：施工进度计划」。失败占位符剥离图号的前缀正则对两种分隔符均兼容。
    # 仅接受 " " / "：" / ":" / "" 四种取值，其余回退默认空格（见 _figure_caption）。
    figure_no_separator: str = " "

    # ===== ENHANCE 2026-09-14：导出格式预设体系（移植自易标 exportFormatPresets）=====
    # 版式预设 / 主题预设 id，空串表示不使用预设（保持历史行为）
    preset_id: str = ""
    theme_id: str = ""
    # 逐级标题字体与字号（下标 0=一级 … 5=六级）。
    # 为空列表时回退到 font_title / size_title_N，保证旧调用方行为不变。
    heading_fonts: list[str] = []
    heading_sizes: list[float] = []
    # 主题色（统一为不带 # 的 6 位十六进制；空串 = 使用系统默认常量）
    heading_color: str = ""          # 标题文字色
    heading_border_color: str = ""   # 标题章框装饰线颜色
    header_footer_color: str = ""    # 页眉页脚文字色
    table_header_bg: str = ""        # 表头底色
    table_header_fg: str = ""        # 表头文字色
    table_alt_row_bg: str = ""       # 表格斑马纹底色
    table_border_color: str = ""     # 表格边框色
    heading_border_enabled: bool = False  # 一级标题章框装饰线开关
    # 页面设置（0 = 使用历史默认值 2.54/2.54/3.18/3.18）
    page_orientation: str = "portrait"   # portrait | landscape
    page_margin_top_cm: float = 0
    page_margin_bottom_cm: float = 0
    page_margin_left_cm: float = 0
    page_margin_right_cm: float = 0
    # 页码与分页
    page_number_enabled: bool = True
    page_number_format: str = "第{page}页"  # 支持 {page} 占位符
    heading_level1_page_break: bool = False  # 一级标题另起一页
    # 正文首行缩进（字符）
    body_first_line_indent_chars: float = 2.0


class Export(BaseModel):
    id: int
    project_id: int
    filename: str
    file_path: str
    file_size: int = 0
    config_json: str = "{}"
    status: str = "completed"
    created_at: str = ""

    model_config = {"from_attributes": True}


class EmptySection(BaseModel):
    id: int
    title: str
    level: int


class DocumentCheckResult(BaseModel):
    passed: bool
    total_sections: int
    empty_sections: list[EmptySection] = []
    uncovered_scoring_points: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    total_chars: int = 0
    # ====== 导出预检增强（V1.0：结构/内容/资源/字符/页数 五维检查）======
    short_sections: list[EmptySection] = []  # 内容过短（<50字，仅警告）
    empty_title_sections: list[EmptySection] = []  # 标题为空的章节
    orphan_sections: list[EmptySection] = []  # 孤立节点（父节点不存在）
    control_char_sections: list[EmptySection] = []  # 含 Word 不兼容控制字符的章节
    invalid_images: list[str] = []  # 无法访问的图片引用（本地路径/URL）
    estimated_pages: int = 0  # 预估总页数（按字数与图片密度）
    # ====== 内容质量规则（《文档质量分析报告》修复 15~24，2026-10-10）======
    # 结构化问题清单，每项含 rule_id/severity/title/message/suggestion/
    # section_id/section_title（与 chart_validators CMP-07 同形）。
    # 同类问题已聚合成 warnings 中的「[内容质量] …」条目；本字段供前端
    # 渲染可定位到章节的问题列表。向后兼容：默认空列表，旧调用方不受影响。
    quality_issues: list[dict] = []
    # ENHANCE（本轮跨模块深度探索·审核×导出打通）：未解决的高严重度一致性
    # 冲突数（consistency_conflicts 中 resolved=0 且 severity='high'）。
    # 仅在调用方显式传入 block_on_high_conflicts=True 时统计；默认 0，
    # 旧调用方行为不变。导出路由据此返回 400 阻断并引导去校核页处理。
    high_conflicts_open: int = 0


# ========== 废标检查模块 ==========


class BidDocument(BaseModel):
    id: int | None = None
    name: str
    text: str = ""
    file_id: int | None = None


class RejectionCheckItem(BaseModel):
    id: str
    description: str
    category: str = "废标"  # 废标 / 错别字 / 逻辑
    source: str = ""  # AI 提取 / 用户自定义


class RejectionStartRequest(BaseModel):
    project_id: int | None = None
    tender_text: str = ""
    bid_documents: list[BidDocument] = []
    check_items: list[RejectionCheckItem] = []
    enabled_dimensions: list[str] = ["rejection", "spelling", "logic"]


class RejectionCheckTask(BaseModel):
    id: int
    project_id: int | None = None
    status: str = "pending"
    progress: int = 0
    progress_message: str = ""
    enabled_dimensions: list[str] = []
    bid_documents: list[dict] = []
    check_items: list[dict] = []
    results: dict = {}
    false_positives: list[str] = []
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    model_config = {"from_attributes": True}


class MarkFalsePositiveRequest(BaseModel):
    item_id: str
    is_false_positive: bool = True


class RetryDimensionRequest(BaseModel):
    dimension: str  # rejection / spelling / logic
    bid_doc_id: int | None = None  # 可选：仅重试某文档


class ExtractItemsRequest(BaseModel):
    tender_text: str


class ApplySpellingFixRequest(BaseModel):
    """错别字一键修正请求（ENHANCE 2026-09-15）。

    - `item_ids`：要修正的错别字条目 ID（支持批量，逐条错误隔离）
    - `writeback_project`：是否同时把修正写回项目正文小节
      （仅当投标文本确实由项目正文拼装、且偏移能精确映射时才会写）
    """

    task_id: int
    item_ids: list[str] = []
    writeback_project: bool = False


# ========== 可行性研究报告请求模型（ENHANCE 2026-09-15） ==========


class FeasibilityProjectInfoModel(BaseModel):
    """可研项目基础参数（十项，缺项由后端按【待补充】处理）。"""

    projectName: str = ""
    projectType: str = "government"
    industry: str = ""
    constructionUnit: str = ""
    location: str = ""
    constructionContent: str = ""
    constructionPeriodYears: str = "2"
    operationPeriodYears: str = "20"
    totalInvestment: str = ""
    fundingSource: str = ""


class FeasibilitySaveProjectInfoRequest(BaseModel):
    project_info: FeasibilityProjectInfoModel
    clear_downstream: bool = True


class FeasibilitySourceAddRequest(BaseModel):
    file_name: str = ""
    content: str


class FeasibilityOutlineConfigRequest(BaseModel):
    """目录口径配置；`reference_document_ids=None` 表示不改动当前参考知识库选择。"""

    outline_template: str = "government"
    target_words: int = 30000
    reference_document_ids: list[int] | None = None


class FeasibilityOutlineSaveRequest(BaseModel):
    """目录树保存请求（树形编辑器）。

    `id_map`（旧编号 → 新编号）与 `affected_node_ids`：删除/新增/排序导致重新编号时，
    后端据此把正文与精确选材搬运到新编号上，避免人工章节内容丢失或错挂。
    """

    outline_data: dict
    reason: str = "replace"
    id_map: dict[str, str] = Field(default_factory=dict)
    affected_node_ids: list[str] = Field(default_factory=list)


class FeasibilityReferenceDocumentsRequest(BaseModel):
    """参考知识库文档选择（可研选材）。"""

    document_ids: list[int] = Field(default_factory=list)


class FeasibilityNodeKnowledgeRequest(BaseModel):
    """单章节精确指定的知识条目（空列表 = 恢复按相关度自动匹配）。"""

    item_ids: list[int] = Field(default_factory=list)


class FeasibilityMarkdownSaveRequest(BaseModel):
    markdown: str = ""


class FeasibilityChapterSaveRequest(BaseModel):
    content: str = ""


class FeasibilityContentRunRequest(BaseModel):
    """正文生成参数：only_missing 续跑未完成章节；skip_polish 跳过自然化审校。"""

    only_missing: bool = False
    skip_polish: bool = False


# ========== 批量操作请求模型 ==========


class RejectionBulkActionRequest(BaseModel):
    """批量操作请求体"""

    task_ids: list[int]  # 任务ID列表
    action: str  # 动作类型: "delete", "mark_all_false_positive"


class BatchTaskDeleteRequest(BaseModel):
    """批量删除任务请求"""

    task_ids: list[int]


class BatchMarkFalsePositiveRequest(BaseModel):
    """批量标记误报请求"""

    task_id: int
    item_ids: list[str]


# ========== 报告调度模型 ==========


class ReportScheduleRequest(BaseModel):
    """报告定时调度请求"""

    task_id: int
    schedule_type: str = "daily"  # daily/weekly/monthly/once
    schedule_time: str | None = None  # ISO format time for once scheduling
    enabled: bool = True


class ReportScheduleTask(BaseModel):
    """定时调度任务"""

    id: int
    task_id: int
    schedule_type: str
    schedule_time: str | None
    enabled: bool
    created_at: str


class RejectionResultItem(BaseModel):
    id: str
    description: str
    severity: str = "warning"  # critical / warning / info
    quote: str = ""  # 原文引用
    suggestion: str = ""  # 修改建议
    location: str = ""  # 位置信息
    bid_doc_id: int | None = None
    bid_doc_name: str = ""
    category: str = ""  # 废标 / 错别字 / 逻辑
    check_item_id: str = ""
    quote_a: str = ""
    quote_b: str = ""
    location_a: str = ""
    location_b: str = ""
    validated: bool | None = None


class RejectionResultSet(BaseModel):
    rejection: list[RejectionResultItem] = []
    spelling: list[RejectionResultItem] = []
    logic: list[RejectionResultItem] = []


# ========== 标书查重模块 ==========


class DuplicateStartRequest(BaseModel):
    project_id: int
    file_ids: list[int] = []  # uploaded_files 表的 ID
    tender_file_id: int | None = None


# ========== 知识库模块 ==========


class KnowledgeDocumentBase(BaseModel):
    filename: str
    file_type: str = ""
    industry: str = ""


class KnowledgeItemBase(BaseModel):
    title: str
    summary: str = ""
    content: str = ""
    keywords: list[str] = []
    industry: str = ""
    tags: list[str] = []


class KnowledgeItemUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    industry: str | None = None
    tags: list[str] | None = None


# ========== 行业树模块 ==========


class IndustryNodeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    name_en: str | None = None
    description: str | None = None
    prompt_hint: str | None = None
    icon: str | None = None
    knowledge_base_ids: list[str] = []
    default_scoring_items: list[dict] = []
    order_index: int = 0


class IndustryNodeCreate(IndustryNodeBase):
    parent_id: str | None = None
    level: int | None = None


class IndustryNodeUpdate(BaseModel):
    name: str | None = None
    name_en: str | None = None
    description: str | None = None
    prompt_hint: str | None = None
    icon: str | None = None
    knowledge_base_ids: list[str] | None = None
    default_scoring_items: list[dict] | None = None
    is_active: bool | None = None
    order_index: int | None = None


class IndustryNode(IndustryNodeBase):
    id: str
    parent_id: str | None = None
    level: int
    is_active: bool = True
    is_ai_generated: bool = False
    reference_count: int = 0
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""
    children: list["IndustryNode"] = []

    model_config = {"from_attributes": True}


class GenerateChildrenRequest(BaseModel):
    count: int = Field(5, ge=3, le=8)
    temperature: float = 0.7
    model: str | None = None


class MatchIndustryRequest(BaseModel):
    text: str = Field(..., min_length=1)


class BatchImportRequest(BaseModel):
    nodes: list[IndustryNodeCreate]


class ReorderGroupItem(BaseModel):
    """重排序分组项"""

    parent_id: int | None = None
    ordered_ids: list[int]


class ReorderRequest(BaseModel):
    """为重排序端点提供验证的请求模型"""

    groups: list[ReorderGroupItem]


# ========== 智能问答模块 ==========


class QaChatRequest(BaseModel):
    """智能问答请求"""

    project_id: int
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] = []
