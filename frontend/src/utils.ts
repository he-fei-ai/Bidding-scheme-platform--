import type { FileParsingStatus, SectionTreeNode } from "./types";

// ============================================================
// V2.1 标题编号（前端展示版）
// 与后端 heading_standard.py 完全同步
// ============================================================
const CHINESE_NUMBERS: string[] = [
  "", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
  "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
  "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十",
];
const ALPHABET: string[] = [..."abcdefghijklmnopqrstuvwxyz".split("")];
ALPHABET.push(...ALPHABET.map((c) => `a${c}`));  // 超过 26 个

/**
 * V3.1 标题格式化（前端展示版）
 * 根据路径计数器生成完整的标准编号标题
 * 与后端 heading_standard.DEFAULT_NUMBERING_TEMPLATES / HEADING_STYLE_CONFIG 完全一致
 *
 * V3.1 编号规范（遗留 L3 修复 2026-10-04：旧实现 L6~L8 沿用 V3.0 八级口径，
 * 与后端导出不同步；现对齐 heading_standard.py）：
 *   L1: 第X节     path=[L1]
 *   L2: X、       path=[L2]         (中文数字)
 *   L3: X、       path=[L3]         (阿拉伯数字)
 *   L4: X.X、     path=[L3, L4]    (层级编号)
 *   L5: X.X.X、   path=[L3, L4, L5]
 *   L6: X）、     path=[L3, L4, L5, L6]（仅显本级数字）
 *   L7: a、       path=[L7]         (小写字母)
 *   L8: aa、      path=[L8]         (双字母，同后端 ALPHABET 扩展规则)
 */
export function formatV21Heading(
  level: number,
  path: number[],
  title: string,
): string {
  if (!title || !title.trim()) return title || "";
  if (level < 1 || level > 8) return title;

  // 将路径映射到 counters 数组
  // V3.0 计数器布局：
  //   counters[0]=L1, [1]=L2, [2]=L3, [3]=L4, [4]=L5, [5]=L6, [6]=L7, [7]=L8
  //   L4-L6 为层级式，counters[2] 为根
  const counters: number[] = [0, 0, 0, 0, 0, 0, 0, 0];

  if (level === 1) {
    counters[0] = path[0] || 1;
  } else if (level === 2) {
    counters[1] = path[0] || 1;
  } else if (level === 3) {
    counters[2] = path[0] || 1;
  } else if (level === 4) {
    // [L3, L4]
    counters[2] = path[0] || 1;
    counters[3] = path[1] || 1;
  } else if (level === 5) {
    // [L3, L4, L5]
    counters[2] = path[0] || 1;
    counters[3] = path[1] || 1;
    counters[4] = path[2] || 1;
  } else if (level === 6) {
    // V3.1 六级：路径 [L3,L4,L5,L6]，仅本级编号 + ）（同后端 {num}）模板）
    counters[2] = path[0] || 1;
    counters[3] = path[1] || 1;
    counters[4] = path[2] || 1;
    counters[5] = path[3] || 1;
  } else if (level === 7) {
    counters[6] = path[0] || 1;
  } else if (level === 8) {
    counters[7] = path[0] || 1;
  }

  const get = (idx: number, fallback = 1) =>
    counters[idx] && counters[idx] > 0 ? counters[idx] : fallback;

  let number = "";
  if (level === 1) {
    number = `第${CHINESE_NUMBERS[get(0)]}节`;
  } else if (level === 2) {
    number = CHINESE_NUMBERS[get(1)] || "一";
  } else if (level === 3) {
    number = `${get(2)}`;
  } else if (level === 4) {
    number = `${get(2)}.${get(3)}`;
  } else if (level === 5) {
    number = `${get(2)}.${get(3)}.${get(4)}`;
  } else if (level === 6) {
    // V3.1：六级为「X）」而非四级点分（与后端导出一致）
    number = `${get(5)}）`;
  } else if (level === 7) {
    // V3.1：七级为小写字母（同后端 {alpha}）
    number = ALPHABET[get(6) - 1] || "a";
  } else if (level === 8) {
    // V3.1：八级双字母（同后端 alpha_alpha，ALPHABET 已含 aa..az 扩展位）
    number = ALPHABET[get(7) - 1] || "a";
  }

  return `${number}、${title}`;
}

/**
 * V3.0: 根据目录树计算每个节点的完整路径计数器
 * 返回 Map<nodeId, path[]>，与后端 _format_outline_number 一致
 *
 * V3.0 路径约定：
 *   L1 节点: path=[L1]
 *   L2 节点: path=[L2]
 *   L3 节点: path=[L3]
 *   L4 节点: path=[L3, L4]  (父级+自身)
 *   L5 节点: path=[L3, L4, L5]
 *   L6 节点: path=[L3, L4, L5, L6]
 *   L7 节点: path=[L7]
 *   L8 节点: path=[L8]
 */
export function computeHeadingCounters(
  nodes: SectionTreeNode[],
  parentPath: number[] = [],
): Map<number, number[]> {
  const result = new Map<number, number[]>();
  const levelCounters: Map<number, number> = new Map();

  for (const node of nodes) {
    const level = node.level || 1;
    // 当前层级的计数
    const currentCount = (levelCounters.get(level) || 0) + 1;
    levelCounters.set(level, currentCount);

    // 重置比当前层级深的所有计数器
    for (let l = level + 1; l <= 8; l++) {
      levelCounters.set(l, 0);
    }

    // V3.0 构建 path（与后端 _format_outline_number 一致）
    // BUG-FIX 2026-10-04（多级编号不同步）：levelCounters 是「每次递归新建的局部
    // 计数器」，L4 节点所在递归层的 Map 里根本没有 level=3/4/5 的计数，
    // 原实现 levelCounters.get(3) 永远 undefined → 落回默认 1，导致所有四级
    // 标题无论挂在哪个三级下都显示「1.x、」，五级永远「1.1.x」，与后端导出
    // （export.py 用 parent_section_number 实算）不一致。parentPath 参数此前
    // 形同虚设：父节点 path 已携带祖先编号（L3=[n]、L4=[n,m]），改用它派生。
    let path: number[];
    if (level === 4) {
      // 四级：1.1、→ [L3, L4]，L3 编号取父链
      path = [parentPath.length > 0 ? parentPath[0] : (levelCounters.get(3) || 1), currentCount];
    } else if (level === 5) {
      // 五级：1.1.1、→ [L3, L4, L5]，前两级取父链（父为 L4，path=[L3,L4]）
      path = [
        parentPath.length > 0 ? parentPath[0] : (levelCounters.get(3) || 1),
        parentPath.length > 1 ? parentPath[1] : (levelCounters.get(4) || 1),
        currentCount,
      ];
    } else if (level === 6) {
      // 六级：1.1.1.1、→ [L3, L4, L5, L6]，前三级取父链
      path = [
        parentPath.length > 0 ? parentPath[0] : (levelCounters.get(3) || 1),
        parentPath.length > 1 ? parentPath[1] : (levelCounters.get(4) || 1),
        parentPath.length > 2 ? parentPath[2] : (levelCounters.get(5) || 1),
        currentCount,
      ];
    } else {
      // L1/L2/L3/L7/L8：独立编号
      path = [currentCount];
    }

    result.set(node.id, path);

    if (node.children && node.children.length > 0) {
      const sub = computeHeadingCounters(node.children, path);
      sub.forEach((v, k) => result.set(k, v));
    }
  }
  return result;
}

/**
 * 为 SectionTreeNode 添加 number 字段（V2.1 标准编号展示）
 * 返回新的树，不修改原对象
 */
export function annotateOutlineWithNumbers(
  nodes: SectionTreeNode[],
): Array<SectionTreeNode & { number?: string }> {
  const counters = computeHeadingCounters(nodes);
  const annotate = (node: SectionTreeNode): SectionTreeNode & { number?: string } => {
    const path = counters.get(node.id) || [1];
    const number = formatV21Heading(node.level || 1, path, "");
    const annotated: SectionTreeNode & { number?: string } = {
      ...node,
      number,
      children: (node.children || []).map(annotate) as SectionTreeNode[],
    };
    return annotated;
  };
  return nodes.map(annotate);
}

export function getLevelColor(level: number): string {
  switch (level) {
    case 1: return "#1677ff";
    case 2: return "#52c41a";
    case 3: return "#faad14";
    default: return "#999";
  }
}

export function getLevelLabel(level: number): string {
  switch (level) {
    case 1: return "一级";
    case 2: return "二级";
    case 3: return "三级";
    default: return `${level}级`;
  }
}

export function findNodeInTree(nodes: SectionTreeNode[], id: number): SectionTreeNode | null {
  for (const n of nodes) {
    if (n.id === id) return n;
    if (n.children && n.children.length > 0) {
      const found = findNodeInTree(n.children, id);
      if (found) return found;
    }
  }
  return null;
}

export function updateNodeInTree(
  nodes: SectionTreeNode[],
  sectionId: number,
  updates: Partial<SectionTreeNode>,
): SectionTreeNode[] {
  return nodes.map((n) => {
    if (n.id === sectionId) {
      return { ...n, ...updates };
    }
    if (n.children && n.children.length > 0) {
      return { ...n, children: updateNodeInTree(n.children, sectionId, updates) };
    }
    return n;
  });
}

export function collectLeafIds(nodes: SectionTreeNode[], filterPendingFailed?: boolean): number[] {
  const ids: number[] = [];
  for (const n of nodes) {
    if (!n.children || n.children.length === 0) {
      if (!filterPendingFailed || n.status === "pending" || n.status === "failed") {
        ids.push(n.id);
      }
    } else {
      ids.push(...collectLeafIds(n.children, filterPendingFailed));
    }
  }
  return ids;
}

export function countSubtreeLeaves(node: SectionTreeNode): number {
  if (!node.children || node.children.length === 0) return 1;
  return node.children.reduce((sum, child) => sum + countSubtreeLeaves(child), 0);
}

export function getLeafCount(nodes: SectionTreeNode[]): number {
  let c = 0;
  for (const n of nodes) {
    if (!n.children || n.children.length === 0) c++;
    else c += getLeafCount(n.children);
  }
  return c;
}

export function getPendingFailedLeafCount(nodes: SectionTreeNode[]): number {
  let c = 0;
  for (const n of nodes) {
    if (!n.children || n.children.length === 0) {
      if (n.status === "pending" || n.status === "failed" || n.status === "cancelled") c++;
    } else {
      c += getPendingFailedLeafCount(n.children);
    }
  }
  return c;
}

/**
 * 单章节的「兜底扩写」阈值（ENHANCE 2026-09-15）。
 * 必须与后端 `workflows_expand.section_expand_threshold` 保持同一口径：
 * 有 target 用 target×80%，无 target 按层级分档（L1=500/L2=level2Default/L3=300/L4=400），
 * 均不超过平铺阈值，保底 200 字。
 */
export function expandWordThreshold(
  node: Pick<SectionTreeNode, "target_words" | "level">,
  flatMinimum: number,
  level2Default: number,
): number {
  let target = node.target_words || 0;
  if (!target) {
    const tiers: Record<number, number> = { 1: 500, 2: level2Default, 3: 300, 4: 400 };
    target = tiers[node.level] || 300;
  }
  return Math.max(200, Math.min(Math.round(flatMinimum), Math.round(target * 0.8)));
}

export interface WordDeficientStats {
  /** 已生成但字数不足的章节数 */
  count: number;
  /** 距阈值总缺口（字） */
  deficitWords: number;
}

/**
 * 统计「已生成但字数不足」的叶子章节（兜底扩写的目标集合）。
 *
 * BUG-FIX 2026-09-15：兜底扩写按钮此前用 `getPendingFailedLeafCount`（待生成/失败/取消数）
 * 做门控与计数——但扩写实际作用于「已生成且字数不足」的章节（后端只处理有内容的章节）。
 * 全部章节生成完成但字数不足时按钮反而被禁用，核心场景完全不可用。
 */
export function getWordDeficientStats(
  nodes: SectionTreeNode[],
  flatMinimum: number,
  level2Default: number,
): WordDeficientStats {
  let count = 0;
  let deficitWords = 0;
  const walk = (list: SectionTreeNode[]) => {
    for (const n of list) {
      if (!n.children || n.children.length === 0) {
        if (n.status === "completed" && n.has_content) {
          const threshold = expandWordThreshold(n, flatMinimum, level2Default);
          if (n.word_count < threshold) {
            count++;
            deficitWords += threshold - n.word_count;
          }
        }
      } else {
        walk(n.children);
      }
    }
  };
  walk(nodes);
  return { count, deficitWords };
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * UTF-8 安全的 Base64 编码
 * btoa 只支持 Latin1 字符，对含中文的字符串（如 Mermaid 渲染出的 SVG）会抛
 * InvalidCharacterError。使用 TextEncoder 先转 UTF-8 再编码。
 */
export function utf8Base64(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach((b) => {
    binary += String.fromCharCode(b);
  });
  return btoa(binary);
}

/**
 * 将 SVG 字符串渲染为 PNG data URL（通过 Canvas）
 *
 * 用途：流程图/甘特图"插入到正文"时，将 Mermaid 渲染的 SVG 转为 PNG 再存入正文，
 * 保证后端导出 Word（python-docx 不支持 SVG）能正常显示。
 * 同时规避 btoa 对中文 SVG 抛错的问题。
 *
 * @param svgHtml SVG 序列化字符串
 * @param scale 输出缩放倍数（默认 2x，提高 Word 中清晰度）
 * @returns Promise<PNG data URL>
 */
export function svgStringToPngDataUrl(svgHtml: string, scale = 2): Promise<string> {
  return new Promise((resolve, reject) => {
    try {
      // 解析 SVG；若缺少 width/height，则从 viewBox 补充，
      // 否则 <img> 加载 SVG 时默认渲染为 300x150，导致内容被压缩
      const doc = new DOMParser().parseFromString(svgHtml, "image/svg+xml");
      const svgEl = doc.documentElement;
      const viewBox = svgEl.getAttribute("viewBox");
      let w = parseFloat(svgEl.getAttribute("width") || "");
      let h = parseFloat(svgEl.getAttribute("height") || "");
      if ((!w || !h) && viewBox) {
        const parts = viewBox.trim().split(/[\s,]+/).map(Number);
        if (parts.length === 4) {
          w = w || parts[2];
          h = h || parts[3];
        }
      }
      if (!w || !h) {
        w = 800;
        h = 600;
      }
      svgEl.setAttribute("width", String(w));
      svgEl.setAttribute("height", String(h));

      const svgText = new XMLSerializer().serializeToString(doc);
      // encodeURIComponent 方案（避免 btoa 中文崩溃），data URL 同源可绘入 Canvas
      const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgText)}`;
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
          canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
          const ctx = canvas.getContext("2d");
          if (!ctx) {
            reject(new Error("无法创建 Canvas 上下文"));
            return;
          }
          // 白底填充，避免透明背景导出为黑色
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          resolve(canvas.toDataURL("image/png"));
        } catch (e) {
          reject(e);
        }
      };
      img.onerror = () => reject(new Error("SVG 图片加载失败"));
      img.src = url;
    } catch (e) {
      reject(e);
    }
  });
}

/**
 * 从 API 错误响应中提取用户友好消息
 */
export function extractApiError(e: any): string {
  if (!e) return "操作失败";
  if (typeof e === "string") return e;
  if (e.response?.data?.detail) {
    const detail = e.response.data.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d: any) => d.msg || d).join("; ");
    return String(detail);
  }
  if (e.message) return e.message;
  return "操作失败";
}

// ============================================================
// 导出文档模块交互纯函数（ENHANCE 2026-10-04，组件级可测化）
// ============================================================

/**
 * 导出文件大小格式化：B / KB / MB 三段。
 *
 * 背景：ExportConfigPage 历史表格与导出结果区各有一份内联实现（重复代码），
 * 且结果区旧版缺 B 档，<1KB 文件显示为 "0.0 KB"。统一为本函数。
 */
export function formatExportFileSize(size: number): string {
  if (!Number.isFinite(size) || size < 0) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** SSE 进度事件 → UI 状态 reducer（从 handleExport 回调抽取，行为不变）。
 *
 * - start：重置阶段文案，进度不变（返回 0）
 * - log：不进 UI（返回 null，调用方仅 console.debug）
 * - progress：百分比取整 + 阶段文案（step 缺省用默认文案）
 * - 其他事件（connected/completed/error/keep_alive）：不进 UI 状态
 */
export function reduceExportProgressEvent(evt: {
  event?: string;
  step?: string;
  progress?: number;
  content?: string;
}): { phase: string; progress: number } | null {
  if (evt.event === "start") {
    return { phase: "开始构建文档...", progress: 0 };
  }
  if (
    evt.event === "progress" &&
    typeof evt.progress === "number" &&
    evt.progress >= 0
  ) {
    return {
      phase: evt.step || "正在生成 DOCX 文档...",
      progress: Math.round(evt.progress * 100),
    };
  }
  return null;
}

// ============================================================
// 目录生成模块 —— 前端交互纯函数（组件级可测化 2026-10-05）
// 从 OutlineEditorPage.tsx 的 SSE onmessage 分支抽取，行为逐分支等价。
// ============================================================

/** V5.1 目录生成 8 个阶段标签（单一事实源，页面 UI 与进度归约共用） */
export const OUTLINE_STEPS_V51: string[] = [
  "1、AI 参考样本自主生成一级目录",
  "2、一级目录校验（长度/重复/ID连续性）",
  "3、逐章串行生成二级目录（4-6个/章）",
  "4、二级目录校验",
  "5、逐二级串行生成三级目录（3-5个/二级）",
  "6、AI 自检（唯一性+层级合规）",
  "7、最终审核通过，准备保存",
  "8、保存目录到数据库",
];

export interface OutlineGeneratingPatch {
  outlineStep?: number;
  progressPercent?: number;
  progressMessage: string;
}

/**
 * BUG-OUTLINE-03 修复口径：将 generating SSE 事件归约为 UI 进度状态增量。
 * 所有 phase 统一按 outline_step 计算百分比，消除 outline_phase 与其他
 * phase（context/saving/v51）之间的进度跳跃。心跳事件仅更新提示文案，
 * 不改变步骤与百分比。返回对象中缺省的字段表示“保持当前值不变”。
 */
export function reduceOutlineGeneratingEvent(
  data: { phase?: string; outline_step?: number; message?: string },
  stepLabels: string[] = OUTLINE_STEPS_V51,
): OutlineGeneratingPatch {
  const totalSteps = stepLabels.length;
  const pct = (step: number) =>
    Math.min(100, Math.round(((step - 1) / totalSteps) * 100));

  if (data.phase === "outline_phase" && data.outline_step) {
    return {
      outlineStep: data.outline_step,
      progressPercent: pct(data.outline_step),
      progressMessage: data.message || stepLabels[data.outline_step - 1] || "",
    };
  }
  if (data.phase === "heartbeat") {
    return { progressMessage: data.message || "AI 正在生成中，请稍候..." };
  }
  // 其他阶段（context、saving、v51 等）：有 outline_step 时按其计算进度
  if (data.outline_step) {
    return {
      outlineStep: data.outline_step,
      progressPercent: pct(data.outline_step),
      progressMessage: data.message || "",
    };
  }
  return { progressMessage: data.message || "" };
}

// ============================================================
// 正文生成模块 —— 前端交互纯函数（组件级可测化 2026-10-05）
// 从 SectionEditorPage.tsx 的 SSE onmessage 进度条目合并逻辑抽取，行为等价。
// ============================================================

/**
 * 章节进度条目 upsert：按 section_id 匹配，存在则浅合并更新，不存在则追加。
 * 返回新数组（不原地修改），供 React 状态更新使用。抽自内联 setProgressItems
 * 回调，使逐章节 SSE 累加/更新逻辑可脱离组件单测。
 */
export function upsertSectionProgressItem<T extends { section_id: number }>(
  prev: T[],
  item: T,
): T[] {
  const existing = prev.findIndex((p) => p.section_id === item.section_id);
  if (existing >= 0) {
    const updated = [...prev];
    updated[existing] = { ...updated[existing], ...item };
    return updated;
  }
  return [...prev, item];
}

// ============================================================
// 上传解析模块 —— 前端交互纯函数（组件级可测化 2026-10-05）
// 从 FileUploadPage.tsx 抽取，钉住前后端解析状态契约。
// ============================================================

export interface FileStatusTag {
  color: string;
  text: string;
}

/**
 * BUG-FIX 2026-09-18（P1）：前后端状态契约对齐——后端写入的是
 * success / failed（Celery 链路另有 processing），此前的
 * uploaded/extracting/extracted 集合后端从不产生，导致解析成功
 * 显示灰色原始值、提取计数永远为 0。保留旧 key 兼容历史数据。
 */
const FILE_STATUS_TAG_MAP: Record<string, FileStatusTag> = {
  success: { color: "success", text: "已提取" },
  processing: { color: "processing", text: "提取中" },
  pending: { color: "default", text: "待提取" },
  uploaded: { color: "default", text: "已上传" },
  extracting: { color: "processing", text: "提取中" },
  extracted: { color: "success", text: "已提取" },
  failed: { color: "error", text: "提取失败" },
};

export function mapFileStatusTag(status: string): FileStatusTag {
  return FILE_STATUS_TAG_MAP[status] || { color: "default", text: status };
}

/** 提取完成计数：成功状态为 "success"（兼容历史 "extracted"） */
export function countExtractedFiles(files: { status: string }[]): number {
  return files.filter((f) => f.status === "success" || f.status === "extracted")
    .length;
}

// ============================================================
// 上传解析模块 —— 批量上传交互状态机（组件级可测化 2026-10-05）
// 从 FileUploadPage.tsx 的 handleUpload / finish 内联逻辑抽取为纯函数，
// 使「批量上传状态错乱」类缺陷可以在无 DOM 环境下单测。
// ============================================================

/** 单份文件的上传结果分类 */
export type UploadOutcome = "ok" | "parse_failed" | "error";

/** 本批次聚合计数：ok=上传并解析成功；parseFailed=HTTP 成功但解析失败；failed=请求失败 */
export interface UploadBatchTotals {
  ok: number;
  parseFailed: number;
  failed: number;
}

export const EMPTY_BATCH_TOTALS: UploadBatchTotals = {
  ok: 0,
  parseFailed: 0,
  failed: 0,
};

/**
 * 上传结果分类（BUG-FIX 2026-10-05 B1 批量上传状态错乱）：
 * 后端对「HTTP 200 但解析失败」返回 status="failed"（文件已入库、文本为空），
 * 此前前端把它与真正的成功一并计入 ok，批尾汇总谎报"全部成功"。
 */
export function classifyUploadResult(
  res: { status?: string | null } | null | undefined,
): UploadOutcome {
  if (!res) return "error";
  if (res.status === "failed") return "parse_failed";
  return "ok";
}

/** 累加本批次计数（纯函数，返回新对象） */
export function accumulateBatchTotals(
  totals: UploadBatchTotals,
  outcome: UploadOutcome,
): UploadBatchTotals {
  if (outcome === "parse_failed") {
    return { ...totals, parseFailed: totals.parseFailed + 1 };
  }
  if (outcome === "error") {
    return { ...totals, failed: totals.failed + 1 };
  }
  return { ...totals, ok: totals.ok + 1 };
}

/** 本批次已处理文件总数 */
export function totalBatchCount(totals: UploadBatchTotals): number {
  return totals.ok + totals.parseFailed + totals.failed;
}

/**
 * 是否对当前这一份文件单独弹提示：
 * 只有「最后一个在途 且 本批只处理了 1 个」时才逐条提示，批量场景由批尾汇总替代，
 * 避免一次拖 10 个文件刷 10 条 toast。
 */
export function shouldNotifySingle(
  pendingInFlight: number,
  totals: UploadBatchTotals,
): boolean {
  return pendingInFlight <= 1 && totalBatchCount(totals) <= 1;
}

/**
 * 批尾汇总文案（整批处理完后一次性提示）。
 * 单文件（总数 <= 1）返回 null —— 已由逐条提示覆盖，不重复打扰。
 */
export function formatBatchSummary(
  totals: UploadBatchTotals,
): { type: "success" | "warning" | "error"; text: string } | null {
  const total = totalBatchCount(totals);
  if (total <= 1) return null;
  const { ok, parseFailed, failed } = totals;
  if (parseFailed === 0 && failed === 0) {
    return { type: "success", text: `批量上传完成，成功 ${ok} 个文件` };
  }
  const parts: string[] = [`成功 ${ok} 个`];
  if (parseFailed > 0) parts.push(`解析失败 ${parseFailed} 个`);
  if (failed > 0) parts.push(`上传失败 ${failed} 个`);
  return { type: "warning", text: `批量上传完成：${parts.join("，")}` };
}

/** 单个文件的失败文案（区分「请求失败」与「已上传但解析失败」） */
export function formatUploadFailureText(
  filename: string,
  outcome: UploadOutcome,
  detail?: string,
): string {
  if (outcome === "parse_failed") {
    return `${filename} 解析失败：${detail || "请检查文件格式"}`;
  }
  return `${filename} 上传失败：${detail || "未知错误"}`;
}

/**
 * 重新提取结果分类（BUG-FIX 2026-10-06 R5，与上传 B1 同口径）：
 * 后端 re-extract 返回 HTTP 200 + status="failed"（降级提示串）或
 * status="success" + message 非空（空文本提示），此前前端一律弹
 * 「重新提取成功」——用户以为问题已修复，实际文件仍无正文。
 */
export type ReExtractOutcome = "ok" | "empty" | "failed";

export function classifyReExtractResult(
  res: { status?: string | null; message?: string | null } | null | undefined,
): ReExtractOutcome {
  if (!res) return "failed";
  if (res.status === "failed") return "failed";
  if (res.message) return "empty";
  return "ok";
}

/** 重新提取结果文案（failed 透出后端原因；empty 透出空文本提示） */
export function formatReExtractResultText(
  outcome: ReExtractOutcome,
  detail?: string,
): string {
  if (outcome === "failed") {
    return `重新提取失败：${detail || "请检查文件格式"}`;
  }
  if (outcome === "empty") {
    return detail || "已重新提取，但未提取到文本内容";
  }
  return "重新提取成功";
}

/** 图片类扩展名（走 OCR 链路，解析耗时长，适合后台异步解析） */
export const IMAGE_FILE_TYPES: readonly string[] = [
  "png", "jpg", "jpeg", "bmp", "tiff", "webp",
];

/** 是否图片文件（按 file_type 或文件名后缀判断，两者任一命中即可） */
export function isImageFile(fileType?: string | null, filename?: string | null): boolean {
  const t = (fileType || "").toLowerCase().replace(/^\./, "");
  if (t && IMAGE_FILE_TYPES.includes(t)) return true;
  const name = (filename || "").toLowerCase();
  return IMAGE_FILE_TYPES.some((ext) => name.endsWith(`.${ext}`));
}

/** 主/辅角色切换：当前角色 → 目标角色 */
export function nextFileRole(current?: string | null): "main" | "supporting" {
  return current === "main" ? "supporting" : "main";
}

/** 角色切换成功文案（告知旧主文件被自动降级，避免用户困惑） */
export function formatRoleToggleMessage(
  filename: string,
  nextRole: "main" | "supporting",
  demotedCount: number,
): string {
  if (nextRole !== "main") return "已设为辅助文件";
  return demotedCount > 0
    ? `已将《${filename}》设为主文件，原主文件降为辅助`
    : `已将《${filename}》设为主文件`;
}

// ============================================================
// 上传解析模块 —— 解析任务状态（GET /tasks/files/status，2026-10-05 接入）
// ============================================================

/**
 * 解析状态 → 展示文案（纯函数，组件级可测）。
 * 无实质信息（idle / 无任务）时返回 null，由调用方决定不渲染。
 */
export function formatParsingStatusText(
  s: FileParsingStatus | null | undefined,
): string | null {
  if (!s) return null;
  if (s.processing > 0) {
    return `正在解析 ${s.processing} 个文件…`;
  }
  const { recent_completed: done, recent_failed: bad } = s;
  if (done === 0 && bad === 0) return null;
  if (bad === 0) return `最近解析完成 ${done} 个文件`;
  return `最近解析完成 ${done} 个，失败 ${bad} 个`;
}

// ============================================================
// 提取项目（要点/评分点）模块 —— 前端交互纯函数（组件级可测化 2026-10-05）
// 从 SummaryReviewPage.tsx 抽取，行为逐分支等价。
// ============================================================

export interface ScoringPoint {
  id: string;
  name: string;
  weight: string;
  standard: string;
  source: string;
}

/** 摘要文本有效性：排除空/“{}”/“[]”，JSON 对象视为有效，非 JSON 靠长度判定 */
export function isValidSummary(text: string): boolean {
  if (!text) return false;
  if (text === "{}" || text === "[]") return false;
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null;
  } catch {
    return text.trim().length > 20;
  }
}

/**
 * 评分点解析：容错处理多种后端/AI 返回形态。
 * - 直接数组；或包在 scoring_points/points/items/data/result/results/list 包裹对象内
 * - 字段别名：name/title、weight/score、standard/criteria
 * - 缺失 id 回退为 R{n}，非法条目回退为空字段项，解析失败返回空数组
 */
export function parseScoringPoints(text: string): ScoringPoint[] {
  const mapPoint = (p: any, i: number): ScoringPoint => {
    if (!p) return { id: `R${i + 1}`, name: "", weight: "", standard: "", source: "" };
    return {
      id: p.id || `R${i + 1}`,
      name: p.name || p.title || "",
      weight: p.weight || p.score || "",
      standard: p.standard || p.criteria || "",
      source: p.source || "",
    };
  };
  try {
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
    if (Array.isArray(parsed)) {
      return parsed.map((p, i) => mapPoint(p, i));
    }
    if (parsed && typeof parsed === "object") {
      for (const key of ["scoring_points", "points", "items", "data", "result", "results", "list"]) {
        const arr = (parsed as any)[key];
        if (Array.isArray(arr)) {
          return arr.map((p, i) => mapPoint(p, i));
        }
      }
    }
  } catch {
    // ignore parse errors
  }
  return [];
}

/** 宽松数组解析：仅当顶层为合法 JSON 数组时返回，否则空数组 */
export function parseArrayData(text: string): any[] {
  if (!text || text === "[]") return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed;
    return [];
  } catch {
    return [];
  }
}

// ============================================================
// 审核与预检模块 —— 一致性冲突严重度映射（组件级可测化 2026-10-05）
// 从 ConsistencyPage.tsx 抽取，钉住未知严重度回退为 medium 的行为。
// ============================================================

export interface ConsistencySeverityMeta {
  color: string;
  label: string;
}

const CONSISTENCY_SEVERITY_META: Record<string, ConsistencySeverityMeta> = {
  high: { color: "red", label: "严重" },
  medium: { color: "orange", label: "中等" },
  low: { color: "blue", label: "轻微" },
};

export function mapConsistencySeverity(severity: string): ConsistencySeverityMeta {
  return CONSISTENCY_SEVERITY_META[severity] || CONSISTENCY_SEVERITY_META.medium;
}

// ============================================================
// 文本模型配置模块 —— 厂商默认元数据（组件级可测化 2026-10-05）
// 从 TextConfigPage.tsx 抽取，单一事实源防止 base_url/model 键漂移。
// ============================================================

export interface ProviderMeta {
  label: string;
  base_url: string;
  model: string;
}

export const TEXT_PROVIDER_META: Record<string, ProviderMeta> = {
  agnes: { label: "Agnes AI（新加坡）", base_url: "https://api.agnes-ai.cn/v1", model: "agnes-2.5-flash" },
  dashscope: { label: "阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-turbo" },
  volcengine: { label: "火山引擎", base_url: "https://ark.cn-beijing.volces.com/api/v3", model: "doubao-pro-32k" },
  hunyuan: { label: "腾讯混元", base_url: "https://api.hunyuan.cloud.tencent.com/v1", model: "hunyuan-pro" },
  zhipu: { label: "智谱AI", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  stepfun: { label: "阶跃星辰", base_url: "https://api.stepfun.com/v1", model: "step-2-16k" },
  baidu: { label: "百度文心", base_url: "https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat", model: "ERNIE-Speed" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  xingchen_maas: { label: "讯飞星辰 MaaS (Token Plan)", base_url: "https://maas-token-api.cn-huabei-1.xf-yun.com/v2", model: "xsparkx2flash" },
  nvidia: { label: "NVIDIA NIM API", base_url: "https://integrate.api.nvidia.com/v1", model: "nvidia/nemotron-mini-4b-instruct" },
  openai_compatible: { label: "自定义兼容 OpenAI", base_url: "", model: "" },
};

/** 大小写不敏感查找厂商元数据（后端 provider_name 与前端键大小写可能不一致） */
export function findProviderMeta(providerName: string): ProviderMeta | null {
  const key = Object.keys(TEXT_PROVIDER_META).find(
    (k) => k.toLowerCase() === providerName.toLowerCase(),
  );
  return key ? TEXT_PROVIDER_META[key] : null;
}

/** 导出失败分类（从 handleExport 双层 catch 抽取的判定逻辑）：
 *
 * - cancelled：用户主动取消（不降级、不弹错）
 * - conflict：组锁 409 冲突（不降级，单独提示）
 * - fallback：其他 SSE 传输/构建失败（降级同步接口）
 */
export function classifyExportFailure(
  err: any,
  cancelRequested: boolean,
): "cancelled" | "conflict" | "fallback" {
  if (cancelRequested || err?.__cancelled) return "cancelled";
  if (err?.status === 409 || err?.response?.status === 409 || err?.__conflict) return "conflict";
  return "fallback";
}

