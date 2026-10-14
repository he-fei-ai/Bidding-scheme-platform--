export interface Project {
  id: number;
  name: string;
  industry: string;
  industry_id?: string | null;
  bidder_name: string;
  notes: string;
  current_step: number;
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  industry?: string;
  industry_id?: string | null;
  bidder_name?: string;
  notes?: string;
}

export interface ProjectUpdate {
  name?: string;
  industry?: string;
  industry_id?: string | null;
  bidder_name?: string;
  notes?: string;
  current_step?: number;
}

export interface ProjectList {
  total: number;
  items: Project[];
}

export interface FileInfo {
  id: number;
  filename: string;
  file_type: string;
  file_role: string;
  status: string;
  message: string;
  created_at: string;
}

export interface AnalysisResult {
  summary_json: string;
  scoring_points_json: string;
  confirmed_summary: boolean;
  confirmed_scoring: boolean;
  boq_json: string;
  work_interface_json: string;
  construction_method_json: string;
  confirmed_boq: boolean;
  confirmed_work_interface: boolean;
  confirmed_construction_method: boolean;
  global_facts_json?: string;
  confirmed_facts?: boolean;
  scope: string;
}

export interface UploadResult {
  id: number;
  filename: string;
  file_type: string;
  file_role: string;
  status: string;
  message: string;
  // REVIEW-FIX 2026-10-04（M1/m2）：上传换主文件时被自动降级的旧 main id；
  // 以及旧摘要/评分是否需基于新主文件重新生成
  demoted_file_ids?: number[];
  needs_reanalysis?: boolean;
}

/**
 * 项目级解析任务状态聚合（GET /projects/{id}/tasks/files/status）
 * ENHANCE 2026-10-05：后端 2026-10-04 已提供该端点，此前前端无任何消费方。
 */
export interface FileParsingStatus {
  status: string;
  processing: number;
  recent_completed: number;
  recent_failed: number;
  total_tasks: number;
  latest: {
    status: string;
    filename: string;
    file_type: string;
    progress: number;
    total_files: number;
    completed_files: number;
    error_message: string;
    started_at: string;
    updated_at: string;
  } | null;
}

export interface SectionTreeNode {
  id: number;
  title: string;
  level: number;
  sort_order: number;
  section_type: string;
  scoring_point_id: string | null;
  target_words?: number | null;
  // 2026-10-08 新增：字数档位（三级目录控制）
  word_count_tier?: string | null;
  tier_label?: string | null;
  tier_target?: number | null;
  cumulative_word_count?: number;
  status: string;
  children: SectionTreeNode[];
  has_content: boolean;
  word_count: number;
  chart_flags?: string[];
  /**
   * 展示编号（纯前端派生，后端不存储、不返回）。
   *
   * 由 `utils.annotateOutlineWithNumbers` 按树位置生成（V3.1 体系：
   * 第一节、/ 一、/ 1、/ 1.1、…），与后端 `renumber_outline_ids` 的
   * 「层级只由树位置派生」是同一条不变量的两种呈现。
   *
   * 2026-10-12 提升为类型上的可选字段（此前只存在于
   * `SectionTreeNode & { number?: string }` 这个交叉类型里，
   * 导致嵌套 children 上的 number 无法访问 —— V5.1 审核弹窗因此
   * 被迫自己另算一套 `index+1.ci+1` 编号，与主目录树显示不一致）。
   */
  number?: string;
}

export interface Section {
  id: number;
  project_id: number;
  parent_id: number | null;
  title: string;
  level: number;
  sort_order: number;
  section_type: string;
  scoring_point_id: string | null;
  ai_prompt_snapshot: string | null;
  flowchart_json: string | null;
  gantt_json: string | null;
  architecture_json: string | null;
  labor_json: string | null;
  comparison_json: string | null;
  layout_json: string | null;
  timeline_json: string | null;
  inlined_chart_json: string | null;
  illustration_type: string | null;
  illustration_data: string | null;
  content: string;
  target_words: number | null;
  status: string;
  created_at: string;
  updated_at: string;
  word_count: number;
}

export interface SectionCreate {
  parent_id?: number | null;
  title: string;
  level?: number;
  sort_order?: number;
  section_type?: string;
  scoring_point_id?: string | null;
}

export interface SectionUpdate {
  title?: string;
  sort_order?: number;
  parent_id?: number | null;
  content?: string;
  target_words?: number | null;
  // 2026-10-08 新增：字数档位覆盖（null=清除，跟随全局默认）
  word_count_tier?: string | null;
  status?: string;
  section_type?: string;
  scoring_point_id?: string | null;
  ai_prompt_snapshot?: string | null;
  flowchart_json?: string | null;
  gantt_json?: string | null;
  architecture_json?: string | null;
  labor_json?: string | null;
  comparison_json?: string | null;
  layout_json?: string | null;
  timeline_json?: string | null;
  inlined_chart_json?: string | null;
  illustration_type?: string | null;
  illustration_data?: string | null;
}

export interface ExportConfig {
  font_title?: string;
  font_body?: string;
  size_title_1?: number;
  size_title_2?: number;
  size_title_3?: number;
  size_title_sub?: number;
  size_body?: number;
  line_spacing?: number;
  show_title_page?: boolean;
  show_toc?: boolean;
  show_header_footer?: boolean;
  bidder_name?: string;
  image_max_width_cm?: number;
  image_dpi?: number;
  gantt_max_columns?: number;
  strict_mode?: boolean;
  // ===== ENHANCE 2026-09-14：导出格式预设体系（移植自易标 exportFormatPresets）=====
  preset_id?: string;
  theme_id?: string;
  heading_fonts?: string[];
  heading_sizes?: number[];
  heading_color?: string;
  heading_border_color?: string;
  header_footer_color?: string;
  table_header_bg?: string;
  table_header_fg?: string;
  table_alt_row_bg?: string;
  table_border_color?: string;
  heading_border_enabled?: boolean;
  page_orientation?: "portrait" | "landscape";
  page_margin_top_cm?: number;
  page_margin_bottom_cm?: number;
  page_margin_left_cm?: number;
  page_margin_right_cm?: number;
  page_number_enabled?: boolean;
  page_number_format?: string;
  heading_level1_page_break?: boolean;
  body_first_line_indent_chars?: number;
}

/** 导出格式预设（版式）元数据 */
export interface ExportLayoutPreset {
  id: string;
  label: string;
  description: string;
  config: ExportConfig;
}

/** 导出格式预设（主题）元数据 */
export interface ExportThemePreset {
  id: string;
  label: string;
  description: string;
  swatches: string[];
  config: ExportConfig;
}

export interface ExportPresetCatalog {
  layouts: ExportLayoutPreset[];
  themes: ExportThemePreset[];
  defaults: { layout_id: string; theme_id: string };
}

export interface DocumentCheckResult {
  passed: boolean;
  total_sections: number;
  empty_sections: { id: number; title: string; level: number }[];
  uncovered_scoring_points: string[];
  warnings: string[];
  errors: string[];
  total_chars: number;
  // 导出预检增强（V1.0）：与后端 DocumentCheckResult 同步
  short_sections?: { id: number; title: string; level: number }[];
  empty_title_sections?: { id: number; title: string; level: number }[];
  orphan_sections?: { id: number; title: string; level: number }[];
  control_char_sections?: { id: number; title: string; level: number }[];
  invalid_images?: string[];
  estimated_pages?: number;
  // 内容质量规则（《文档质量分析报告》修复 15~24，2026-10-10）：
  // 结构化问题清单，同类问题已聚合成 warnings 中的「[内容质量] …」条目
  quality_issues?: QualityIssue[];
}

/** 内容质量规则 Issue（后端 quality_rules / chart_validators CMP-07 同形） */
export interface QualityIssue {
  rule_id: string;
  severity: "high" | "medium" | "low";
  title: string;
  message: string;
  suggestion?: string;
  section_id?: number | null;
  section_title?: string | null;
}

export interface ExportRecord {
  id: number;
  project_id?: number;
  filename: string;
  file_path: string;
  file_size: number;
  config_json?: string;
  status: string;
  created_at: string;
}

export interface ExportResult {
  filename: string;
  file_path: string;
  file_size: number;
  export_id?: number;
  check: DocumentCheckResult;
  // ENHANCE 2026-10-11（P3·同步/SSE 导出响应类型对齐后端契约）：
  // 后端同步 POST /export/docx（export.py）与 SSE completed 事件自 2026-10-04 起
  // 除 file_path（已统一为下载 URL 口径）外还返回下列两字段；此前前端类型未声明，
  // 消费者无法类型安全地读取，存在隐性数据丢失。纯新增可选字段，向后兼容。
  /** 下载 API URL（export_id 缺失时为 null；与 file_path 同值，便于语义明确的消费） */
  download_url?: string | null;
  /** 磁盘相对路径（相对 PROJECTS_DATA_DIR，供运维排查与日志定位） */
  relative_path?: string;
}

export interface AIConfig {
  provider_name: string;
  provider: string;
  api_key: string;
  model: string;
  base_url: string;
  document_profile: string;
  concurrency: number;
  temperature: number;
  max_tokens: number;
  /** 请求超时（秒）。2026-10-14 起 `/ai/config` 真正可写（此前是死列） */
  timeout?: number;
  is_builtin_default?: boolean;
  current_profile_name?: string;
  /** 配置来源回显（2026-10-04 增强）：env=环境变量覆盖 / database / default */
  config_source?: 'env' | 'database' | 'default';
}

export interface KnownProvider {
  /** 稳定机器 id（2026-10-14 新增）：跨前后端引用厂商的锚点，避免中文展示名歧义 */
  id?: string;
  name: string;
  base_url: string;
  type: string;
  readonly?: boolean;
  is_default?: boolean;
  model?: string;
  api_key?: string;
}

// ========== 按厂商隔离的配置类型 ==========

export interface TextProviderConfig {
  provider_name: string;
  /** 稳定机器 id（2026-10-14 新增，对应 /ai/providers 的 id）；旧后端不返回时为 undefined */
  provider_id?: string;
  provider_type: string;
  api_key: string;
  base_url: string;
  model: string;
  max_tokens: number;
  concurrency: number;
  temperature: number;
  document_profile: string;
  /** 请求超时（秒），2026-10-14 新增可配置项 */
  timeout?: number;
  is_active: boolean;
  api_key_set?: boolean;
  updated_at?: string;
}

/**
 * 保存厂商文本模型配置的请求载荷（2026-10-14 文本模型配置 BUG 修复轮新增）。
 * 镜像后端 `TextProviderConfigData` 请求模型：除 provider_name 外均可选/有默认，
 * **不含** `is_active`（服务端状态，由 activate 端点维护）与响应侧的 `api_key_set`/`updated_at`。
 * 此前误用响应类型 `TextProviderConfig`（is_active/provider_type 必填）作为保存入参，
 * 调用方只能 `as TextProviderConfig` 强转，tsc 报 TS2352/TS2345。
 *
 * `document_profile` 语义与后端 P1-DOC 对齐：`undefined`/`null` = 保留库内原值，
 * `""` = 显式清空，非空串 = 写入新值。
 */
export interface TextProviderSavePayload {
  /** 厂商名（text_provider_configs 主键） */
  provider_name: string;
  /** 厂商类型，默认 openai_compatible */
  provider_type?: string;
  /** API Key；占位符/空串 = 保留原值，非空 = 覆盖 */
  api_key?: string;
  /** 模型服务 base_url */
  base_url?: string;
  /** 模型名 */
  model?: string;
  /** 最大 token 数（后端钳制 [256, 65536]） */
  max_tokens?: number;
  /** 并发数（后端钳制 [1, 64]） */
  concurrency?: number;
  /** 温度（后端钳制 [0, 2]） */
  temperature?: number;
  /** 行业画像：undefined/null=保留原值，""=显式清空 */
  document_profile?: string | null;
  /** 请求超时（秒），后端钳制 [5, 600] */
  timeout?: number;
  /** 保存后是否自动激活为主配置（后端默认 true；"仅保存不切换"传 false） */
  activate_after_save?: boolean;
}

export interface ImageProviderConfig {
  provider_name: string;
  api_key: string;
  base_url: string;
  model: string;
  max_concurrency: number;
  max_per_project: number;
  quality: string;
  style_injection: string;
  enabled: boolean;
  default_size: string;
  default_ratio: string;
  price_per_image: number;
  is_active: boolean;
  api_key_set?: boolean;
  updated_at?: string;
}

// ========== AI Profile 多配置系统 ==========
export interface AIPProfile {
  id: number;
  name: string;
  display_name: string;
  description: string;
  provider_name: string;
  provider: string;
  api_key: string;
  model: string;
  base_url: string;
  max_tokens: number;
  temperature: number;
  concurrency: number;
  timeout: number;
  json_mode: boolean;
  task_type: string;
  rate_limit_per_min: number;
  token_budget_daily: number;
  is_active: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface UsageSummary {
  days: number;
  total_calls: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  total_cost_cny: number;
  avg_response_time_ms: number;
  min_response_time_ms: number;
  max_response_time_ms: number;
  success_count: number;
  error_count: number;
  success_rate: number;
  avg_tokens_per_call: number;
  period: string;
}

export interface HealthProvider {
  provider: string;
  model: string;
  base_url: string;
  total_calls: number;
  success: number;
  failed: number;
  success_rate: number;
  avg_response_time_ms: number;
  last_used: string;
  health_score: number;
  confidence: number;
  status: 'healthy' | 'degraded' | 'unhealthy';
}

export interface DailyTrend {
  day: string;
  task_type: string;
  calls: number;
  tokens: number;
  cost_usd: number;
}

export interface BudgetStatus {
  has_budget: boolean;
  daily_limit: number;
  used_today: number;
  remaining: number;
  usage_pct: number;
  budget_exceeded: boolean;
}

export interface PromptTemplate {
  key: string;
  category: string;
  label: string;
  content: string;
  default_content: string;
  /** 模板中使用的所有占位符变量列表（大小写不敏感） */
  variables?: string[];
}

/** 提示词历史版本（列表项，不含正文） */
export interface PromptVersionMeta {
  id: number;
  key: string;
  label: string;
  category: string;
  source: "edit" | "reset" | "rollback";
  changed_by: string;
  created_at: string;
  content_length: number;
}

/** 提示词历史版本（详情，含正文） */
export interface PromptVersionDetail extends PromptVersionMeta {
  content: string;
  default_content: string;
}

// ========== 废标检查模块 ==========

export interface BidDocument {
  id?: number;
  name: string;
  text: string;
  file_id?: number;
}

export interface RejectionCheckItem {
  id: string;
  description: string;
  category: string; // 废标 / 错别字 / 逻辑
  source?: string;
}

export interface RejectionStartRequest {
  project_id?: number | null;
  tender_text: string;
  bid_documents: BidDocument[];
  check_items: RejectionCheckItem[];
  enabled_dimensions: string[]; // ["rejection","spelling","logic"]
}

export interface RejectionResultItem {
  id: string;
  description: string;
  severity: "critical" | "warning" | "info";
  quote?: string;
  suggestion?: string;
  location?: string;
  bid_doc_id?: number;
  bid_doc_name?: string;
  category: string;
  check_item_id?: string;
  quote_a?: string;
  quote_b?: string;
  location_a?: string;
  location_b?: string;
  /**
   * 证据原文核验结果（后端 verify_rejection_evidence 写入）：
   * - true  证据摘录已在投标文件原文中定位到，position / excerpt / line_hint 有效
   * - false 摘录无法在原文中定位（可能是 AI 改写或幻觉），建议人工重点复核
   * - null  该条目没有原文摘录（如"材料缺失"类），属正常，不参与核验
   */
  validated?: boolean | null;
  /** 证据形态：quote=有原文摘录，structural=结构性线索（材料缺失类） */
  evidence_kind?: "quote" | "structural";
  /** 证据在投标文件原文中的字符偏移（仅 validated=true 时有效） */
  position?: number;
  /** 证据位置的上下文摘录 */
  excerpt?: string;
  /** 证据所在行号提示，如 "第 12 行" */
  line_hint?: string;
  /** check_item_id 是否命中输入检查项清单（防止 AI 自造条款编号） */
  code_matched?: boolean;
  /** 该条目由多少条重复项合并而来（>1 表示发生过去重合并） */
  merged_count?: number;
  /**
   * 跨分块「疑似缺失」复核结论（后端 resolve_missing_material_claims 写入）：
   * - resolved  该材料在其它分块中存在线索，疑似分块误判，已标记排除
   * - confirmed 全文均无线索，缺失判断成立
   * - pending   抽不出可核验关键词，交人工确认（不做排除）
   */
  missing_state?: "pending" | "resolved" | "confirmed";
  /** 缺失复核的说明文案 */
  missing_state_reason?: string;
  /** 错别字条目：原始错误文本 */
  original?: string;
  /** 错别字条目：建议改正文本 */
  corrected?: string;
  /** 错别字条目：position 是「整文」还是仅「批次内」偏移 */
  position_scope?: "document" | "batch";
  /** 错别字条目：是否已一键修正 */
  applied?: boolean;
  /** 错别字条目：修正后的上下文摘录 */
  after_excerpt?: string;
}

export interface RejectionCheckTask {
  id: number;
  project_id?: number | null;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  progress_message: string;
  enabled_dimensions: string[];
  bid_documents: BidDocument[];
  check_items: RejectionCheckItem[];
  results: {
    rejection: RejectionResultItem[];
    spelling: RejectionResultItem[];
    logic: RejectionResultItem[];
  };
  false_positives: string[];
  error: string;
  created_at: string;
  updated_at: string;
}

// ========== 标书查重模块 ==========

export interface DuplicateStartRequest {
  project_id?: number | null;
  file_ids: number[];
  tender_file_id?: number | null;
}

export interface DuplicateCheckTask {
  id: number;
  file_ids: number[];
  tender_file_id?: number | null;
  signature: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  progress_message: string;
  results: {
    metadata: {
      metadata: Array<{
        file_id: number;
        filename: string;
        author: string;
        created: string;
        modified: string;
        company: string;
        last_saved_by: string;
        title: string;
      }>;
      anomalies: Array<{ type: string; description: string; value: string[]; severity?: string }>;
      // ENHANCE 2026-09-15：同源指纹与同源疑点（围标串标线索）
      fingerprints?: Array<Record<string, unknown>>;
      same_source_pairs?: Array<{
        file_a: string;
        file_b: string;
        severity: string;
        evidence: string[];
      }>;
    };
    toc: {
      tocs: Array<{ filename: string; titles: string[] }>;
      matrix: number[][];
      filenames: string[];
      duplicate_groups: Array<{
        file_a: string;
        file_b: string;
        similarity: number;
        common_titles: string[];
      }>;
      // ENHANCE 2026-09-15：近义改写标题与文件级综合相似度（覆盖率 + 顺序）
      similar_groups?: Array<{
        file_a: string;
        file_b: string;
        title_a: string;
        title_b: string;
        similarity: number;
        criterion?: string;
      }>;
      pairwise?: Array<{
        file_a: string;
        file_b: string;
        score: number;
        title_overlap: number;
        path_overlap: number;
        order_similarity: number;
        shared_count: number;
        risk: string;
      }>;
    };
    content: {
      duplicate_groups: Array<{
        type: string;
        sentence: string;
        occurrences: Array<{
          file_id: number;
          filename: string;
          sentence_idx: number;
          original: string;
        }>;
      }>;
      similar_groups: Array<{
        type: string;
        similarity: number;
        sentence_a: string;
        sentence_b: string;
        file_a: string;
        file_b: string;
      }>;
      stats: {
        total_sentences: number;
        duplicate_count: number;
        similar_count: number;
        // ENHANCE 2026-09-15：招标原文白名单四级命中统计
        // （exact/strict/skeleton/near），用于判断"重复变少"是否属于正常引用豁免
        tender_whitelist?: Record<string, number>;
        tender_whitelist_count?: number;
      };
    };
    images: {
      total_images: number;
      // ENHANCE 2026-09-15：跨文件重复图片张数（含同一文件的多次出现）
      duplicate_image_count?: number;
      duplicate_groups: Array<{
        hash: string;
        size: number;
        occurrences: Array<{
          file_id: number;
          filename: string;
          path: string;
          // ENHANCE 2026-09-15：图片上下文（章节路径 / 序号 / 前一句）
          image_index?: number;
          directory?: string;
          previous_sentence?: string;
        }>;
        locations?: string[];
      }>;
    };
  };
  error: string;
  created_at: string;
}

// ========== 知识库模块 ==========

export interface KnowledgeDocument {
  id: number;
  filename: string;
  file_path: string;
  file_type: string;
  industry: string;
  status: "pending" | "copying" | "converting" | "extracting" | "matching" | "recovering" | "saving" | "success" | "error";
  step_log: Array<{
    step: string;
    status: string;
    message: string;
    timestamp: string;
  }>;
  markdown_path?: string | null;
  blocks: Array<{ index: number; text: string; type: string }>;
  candidates: Array<{ title: string; summary: string; keywords: string[] }>;
  match_result: Record<string, unknown>;
  coverage: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeItem {
  id: number;
  title: string;
  summary: string;
  content: string;
  keywords: string[];
  industry: string;
  tags: string[];
  source_doc_id?: number | null;
  source_ref: string;
  created_at: string;
}

export interface KnowledgeItemUpdate {
  title?: string;
  summary?: string;
  content?: string;
  keywords?: string[];
  industry?: string;
  tags?: string[];
}

export interface KnowledgeSearchResult {
  total: number;
  items: KnowledgeItem[];
  limit: number;
  offset: number;
}

export interface KnowledgeStats {
  total_documents: number;
  success_documents: number;
  total_items: number;
  total_usage: number;
}

// ========== 行业树模块 ==========

export interface ScoringItem {
  id: string;
  content: string;
  score: number;
  weight: number;
}

export interface IndustryNode {
  id: string;
  name: string;
  name_en?: string;
  parent_id?: string | null;
  level: number;
  description?: string;
  prompt_hint?: string;
  icon?: string;
  knowledge_base_ids: string[];
  default_scoring_items: ScoringItem[];
  is_active: boolean;
  is_ai_generated: boolean;
  order_index: number;
  reference_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  children: IndustryNode[];
}

export interface IndustryNodeCreate {
  name: string;
  name_en?: string;
  parent_id?: string | null;
  level?: number;
  description?: string;
  prompt_hint?: string;
  icon?: string;
  knowledge_base_ids?: string[];
  default_scoring_items?: ScoringItem[];
  order_index?: number;
}

export interface IndustryNodeUpdate {
  name?: string;
  name_en?: string;
  description?: string;
  prompt_hint?: string;
  icon?: string;
  knowledge_base_ids?: string[];
  default_scoring_items?: ScoringItem[];
  is_active?: boolean;
  order_index?: number;
}

export interface GenerateChildrenRequest {
  count?: number;
  temperature?: number;
  model?: string;
}

export interface MatchIndustryResult {
  node_id: string;
  node_name: string;
  confidence: number;
}

export interface MatchIndustryAIResult extends MatchIndustryResult {
  reason: string;
}

export interface IndustryReference {
  id: number;
  name: string;
  industry: string;
  created_at: string;
}

export interface AuditLog {
  id: number;
  node_id: string;
  action: string;
  old_value: string;
  new_value: string;
  changed_by: string;
  changed_at: string;
}

export interface OutlineReviewResult {
  passed: boolean;
  issues: string[];
  suggestions: string[];
  outline?: SectionTreeNode[];
}

export interface IndustryStats {
  total: number;
  active: number;
  ai_generated: number;
  root_count: number;
  leaf_count: number;
  last_updated: string;
}

// ========== Chart Types ==========

export type ChartType = "flowchart" | "gantt" | "architecture" | "labor" | "comparison" | "layout" | "timeline";

export interface ValidationResult {
  valid: boolean;
  errors: string[];
}