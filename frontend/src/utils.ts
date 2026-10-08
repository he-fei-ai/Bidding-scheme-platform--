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
  // BUG-FIX 2026-10-08（编号不显示 · P0）：原实现第一行就是
  //   `if (!title || !title.trim()) return title || "";`
  // 而唯一的生产调用方 `annotateOutlineWithNumbers` 正是**故意传空标题**
  // 只取编号前缀 → 每次都命中这条早退并返回 ""。于是 `node.number` 恒为空串，
  // 正文编辑页章节树（SectionEditorPage 直接渲染 `{node.number}`）的编号列
  // **永远空白**，而导出/标题模板侧编号俱全 → 界面对不上导出结果。
  // 现把「标题为空」的处理下沉到返回值处：为空时只输出编号前缀（含顿号），
  // 供展示层与标题相邻拼接；title 非空时行为逐字不变。
  if (level < 1 || level > 8) return title || "";

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
  // CHINESE_NUMBERS 只到 30；序号超出（如手工造出 40 个同级节点）不能渲染成
  // 「第undefined节」，退回阿拉伯数字保底。
  const zh = (n: number): string => CHINESE_NUMBERS[n] || String(n);

  let number = "";
  if (level === 1) {
    number = `第${zh(get(0))}节`;
  } else if (level === 2) {
    number = zh(get(1));
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

  if (!title || !title.trim()) return number ? `${number}、` : "";
  return `${number}、${title}`;
}

/**
 * 节点层级归一化（BUG-FIX 2026-10-08·level 命名空间统一）
 *
 * 原实现散落 `node.level || 1`：level 缺失/为 0/为 null 一律当一级。审核态目录
 * （review_needed 事件）由后端 `_generate_level1_v51` 下发，L1 节点**根本不带**
 * level 字段，`normalize_outline_items`（唯一会按递归深度补 level 的函数）只在
 * 落库前 sse_db_ops._clear_and_save_outline 才跑 —— 于是前端 `?? 1` 把 L2/L3 子节点
 * 也当成一级，编号渲染成「1、2、3」而非「1.1、1.1.1」，与落库后的目录树、导出
 * 完全对不上。现统一：非法/缺失 level 归 1（与后端 `node.get("level", 1)` 同语义），
 * 树内层级由调用方按递归深度填好（见 OutlineEditorPage.outlineDictToTreeNodes）。
 */
function normalizeNodeLevel(level: unknown): number {
  const n = typeof level === "number" ? level : Number(level);
  if (!Number.isInteger(n) || n < 1 || n > 8) return 1;
  return n;
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
 *
 * BUG-FIX 2026-10-08（编号冲突）：`node` 可能不是对象（AI/接口脏数据），
 * 原实现直接 `node.level` 会抛 TypeError 让整棵树的编号渲染失败；现跳过并继续。
 * 注：本函数的返回以 id 为键，id 重复时后写覆盖先写（Map 语义）。展示链路
 * `annotateOutlineWithNumbers` 已改为遍历内直接携带 path，不依赖 id 唯一性。
 */
export function computeHeadingCounters(
  nodes: SectionTreeNode[],
  parentPath: number[] = [],
): Map<number, number[]> {
  const result = new Map<number, number[]>();
  collectHeadingPaths(Array.isArray(nodes) ? nodes : [], parentPath, result);
  return result;
}

/**
 * 编号派生唯一实现（BUG-FIX 2026-10-08：统一编号命名空间）
 * computeHeadingCounters / annotateOutlineWithNumbers 共用，保证「界面展示编号」
 * 与「计算 path」永远同一份口径，不会各自漂移。
 */
function collectHeadingPaths(
  nodes: readonly SectionTreeNode[],
  parentPath: number[],
  result: Map<number, number[]>,
): void {
  const levelCounters: Map<number, number> = new Map();

  for (const node of nodes) {
    if (!node || typeof node !== "object") continue;
    const level = normalizeNodeLevel(node.level);
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
    const path = deriveHeadingPath(level, currentCount, parentPath, levelCounters);

    result.set(node.id, path);

    if (Array.isArray(node.children) && node.children.length > 0) {
      collectHeadingPaths(node.children, path, result);
    }
  }
}

/** 单个节点的 path 派生（V3.0 路径约定，纯函数便于单测） */
function deriveHeadingPath(
  level: number,
  currentCount: number,
  parentPath: number[],
  levelCounters: Map<number, number>,
): number[] {
  const at = (i: number, lv: number) =>
    parentPath.length > i ? parentPath[i] : (levelCounters.get(lv) || 1);
  if (level === 4) {
    // 四级：1.1、→ [L3, L4]，L3 编号取父链
    return [at(0, 3), currentCount];
  }
  if (level === 5) {
    // 五级：1.1.1、→ [L3, L4, L5]，前两级取父链（父为 L4，path=[L3,L4]）
    return [at(0, 3), at(1, 4), currentCount];
  }
  if (level === 6) {
    // 六级：1.1.1.1、→ [L3, L4, L5, L6]，前三级取父链
    return [at(0, 3), at(1, 4), at(2, 5), currentCount];
  }
  // L1/L2/L3/L7/L8：独立编号
  return [currentCount];
}

/**
 * 为 SectionTreeNode 添加 number 字段（V2.1 标准编号展示）
 * 返回新的树，不修改原对象
 *
 * BUG-FIX 2026-10-08（编号冲突 · 高）：原实现先 `computeHeadingCounters` 建
 * `Map<id, path[]>` 再按 id 回查标注。审核态目录尚未落库，节点 id 是前端临时
 * 值，只要出现重复（同层局部序号撞号，见 OutlineEditorPage 的旧
 * `-(idx+1)` 分配），后写节点的 path 就覆盖先写的 → 同层级多个节点显示同一编号，
 * 且 `counters.get(node.id) || [1]` 会把未登记的节点静默降级成「第一节」。
 * 改为遍历内直接携带 path 向下传，编号与 id 唯一性彻底解耦。
 */
export function annotateOutlineWithNumbers(
  nodes: SectionTreeNode[],
): Array<SectionTreeNode & { number?: string }> {
  const out: Array<SectionTreeNode & { number?: string }> = [];
  annotateInto(Array.isArray(nodes) ? nodes : [], [], out);
  return out;
}

function annotateInto(
  nodes: readonly SectionTreeNode[],
  parentPath: number[],
  out: Array<SectionTreeNode & { number?: string }>,
): void {
  const levelCounters: Map<number, number> = new Map();
  for (const node of nodes) {
    if (!node || typeof node !== "object") continue;
    const level = normalizeNodeLevel(node.level);
    const currentCount = (levelCounters.get(level) || 0) + 1;
    levelCounters.set(level, currentCount);
    for (let l = level + 1; l <= 8; l++) {
      levelCounters.set(l, 0);
    }
    const path = deriveHeadingPath(level, currentCount, parentPath, levelCounters);
    const childrenOut: Array<SectionTreeNode & { number?: string }> = [];
    if (Array.isArray(node.children) && node.children.length > 0) {
      annotateInto(node.children, path, childrenOut);
    }
    out.push({
      ...node,
      number: formatV21Heading(level, path, ""),
      children: childrenOut as SectionTreeNode[],
    });
  }
}

/** 计算同层下一个可用 sort_order（1 起，避开已占用值） */
export function nextFreeSortOrder(nodes: SectionTreeNode[]): number {
  const taken = new Set<number>();
  for (const n of nodes) {
    if (Number.isInteger(n.sort_order) && n.sort_order > 0) taken.add(n.sort_order);
  }
  let cursor = 1;
  while (taken.has(cursor)) cursor += 1;
  return cursor;
}

/**
 * 未落库节点的临时 ID 分配器：全局单调递减，保证**整棵树**唯一。
 *
 * BUG-FIX 2026-10-08（原实现 `id: n.id ?? -(idx + 1)` 只按「同层局部下标」编号）：
 * L2 / 最终审核阶段后端下发的是**嵌套树**（generate_outline_v51 的 level2/final
 * 审核事件 outline 即完整树），多个父节点各自的第 1 个子节点都会拿到 `-1`、
 * 各自第 2 个都拿到 `-2`……由此产生三条连锁故障：
 *   (a) `reviewEditTitles: Record<number,string>` 以 id 为键 → 改一个二级标题，
 *       所有同局部序号的兄弟节点标题一起被改（applyTitles 递归回填），人工审核
 *       的编辑被静默放大成批量改名；
 *   (b) React `<div key={child.id}>` 出现重复 key → 列表复用错行，输入框串值；
 *   (c) `annotateOutlineWithNumbers` 旧实现按 id 回查 path → 编号互相覆盖。
 * 负 ID 保证永不与数据库正整数主键冲突，单调递减保证同一会话内多次转换不重复。
 */
let tempNodeIdSeq = 0;
export function allocTempNodeId(): number {
  tempNodeIdSeq -= 1;
  return tempNodeIdSeq;
}

/** 标题归一化（BUG-FIX 2026-10-08·空数据/编码）：AI 可能给出 null/数字/对象，
 *  原实现 `n.title || ""` 留下非字符串，下游 `!n.title.trim()` 直接 TypeError
 *  让整个审核弹窗白屏。这里统一转成字符串并裁掉首尾空白。 */
function normalizeNodeTitle(raw: unknown): string {
  if (typeof raw === "string") return raw.trim();
  if (raw === null || raw === undefined) return "";
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  return "";
}

/**
 * 将后端 outline 字典转换为 SectionTreeNode 格式
 *
 * BUG-FIX 2026-10-08（level 派生）：原实现 `level: n.level ?? 1` 采信节点自带
 * level。但 review_needed 事件直出 `_generate_level1_v51` / `_generate_level2_v51`
 * 的原始结构，L1 节点根本没有 level 字段（唯一补 level 的 `normalize_outline_items`
 * 只在落库前跑），于是 L2/L3 子节点被当成一级 → 审核弹窗里编号显示成「1、2、3」
 * 而不是「1.1、1.1.1」，用户审核依据的编号与最终落库/导出的编号对不上。
 * 改为**按递归深度派生**，与后端落库契约（`_flatten_outline_tree` / `_save_nodes`
 * 「层级由实际递归深度决定」）逐字一致。
 *
 * BUG-FIX 2026-10-08（sort_order）：原 `?? idx` 是 0 起，而落库口径是 1 起
 * （`_normalize_section_position` = MAX+1），且与兄弟中已显式给出的 sort_order
 * 可能撞号 → 落库后顺序与审核时看到的不一致。现按 1 起补齐且避开已占用值。
 */
export function outlineDictToTreeNodes(
  nodes: any[],
  depth = 1,
  usedIds?: Set<number>,
): SectionTreeNode[] {
  if (!Array.isArray(nodes)) return [];
  const seen = usedIds ?? new Set<number>();
  const derivedLevel = depth > 8 ? 8 : depth;

  // 先算出本层所有节点的 sort_order：已显式给出的保留，缺省的从 1 起找空位
  const takenOrders = new Set<number>();
  nodes.forEach((n: any) => {
    const so = n && typeof n === "object" ? Number(n.sort_order) : NaN;
    if (Number.isInteger(so) && so > 0) takenOrders.add(so);
  });
  let cursor = 1;
  const resolveOrder = (raw: unknown): number => {
    const so = Number(raw);
    if (Number.isInteger(so) && so > 0) return so;
    while (takenOrders.has(cursor)) cursor += 1;
    takenOrders.add(cursor);
    return cursor;
  };

  return nodes
    .filter((n: any) => n !== null && n !== undefined && typeof n === "object")
    .map((n: any) => {
      const ownId =
        typeof n.id === "number" && Number.isFinite(n.id) && n.id > 0 ? n.id : null;
      let id = ownId;
      if (id === null || seen.has(id)) {
        // 兜底：后端若下发重复/非法 id，也不得与同树其它节点撞号
        id = allocTempNodeId();
      }
      seen.add(id);
      return {
        id,
        title: normalizeNodeTitle(n.title),
        level: Number.isInteger(Number(n.level)) && Number(n.level) > 0 ? Number(n.level) : derivedLevel,
        sort_order: resolveOrder(n.sort_order),
        section_type: typeof n.section_type === "string" ? n.section_type : "text",
        scoring_point_id: n.scoring_point_id ?? null,
        status: "active",
        children: outlineDictToTreeNodes(n.children, derivedLevel + 1, seen),
        has_content: Boolean(n.has_content),
        word_count: Number.isInteger(Number(n.word_count)) ? Number(n.word_count) : 0,
      } as SectionTreeNode;
    });
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

// ========== 字数档位配置（2026-10-08）==========

/** 字数档位定义（与后端 WRITING_LENGTH_TIER_CONFIG 一致） */
export interface WordCountTier {
  key: 'brief' | 'standard' | 'detailed' | 'long';
  label: string;
  targetWords: number;
  toleranceRatio: number; // ±10%
  isDefault: boolean;
}

export const WORD_COUNT_TIERS: WordCountTier[] = [
  { key: 'brief', label: '简短', targetWords: 800, toleranceRatio: 0.10, isDefault: false },
  { key: 'standard', label: '标准', targetWords: 1200, toleranceRatio: 0.10, isDefault: true },
  { key: 'detailed', label: '详细', targetWords: 2000, toleranceRatio: 0.10, isDefault: false },
  { key: 'long', label: '超长', targetWords: 2500, toleranceRatio: 0.10, isDefault: false },
];

export const DEFAULT_WORD_COUNT_TIER = 'standard';

/** 方案三：L3 自身正文 300 字 ±20%（240~360 字） */
export const L3_OWN_TARGET_WORDS = 300;
export const L3_OWN_TOLERANCE_RATIO = 0.20;

/** 方案三下 L3 自身正文目标字数（300 字） */
export function getL3OwnTarget(): number {
  return L3_OWN_TARGET_WORDS;
}

/** 方案三下 L3 自身正文达标区间：240~360 字 */
export function getL3OwnRange(): [number, number] {
  return [
    Math.floor(L3_OWN_TARGET_WORDS * (1 - L3_OWN_TOLERANCE_RATIO)),
    Math.ceil(L3_OWN_TARGET_WORDS * (1 + L3_OWN_TOLERANCE_RATIO)),
  ];
}

/** 判断 L3 自身正文是否在 300 ± 20% 区间内（与后端 check_document 同口径） */
export function isWithinL3OwnRange(wordCount: number): boolean {
  const [lo, hi] = getL3OwnRange();
  return wordCount >= lo && wordCount <= hi;
}


/**
 * 解析节点的有效目标字数（2026-10-08：三级目录档位控制）。
 * 优先级：单节点 target_words > 单节点 word_count_tier > 全局 writing_length > 默认档位
 */
export function resolveTargetForNode(
  node: Pick<SectionTreeNode, 'level' | 'target_words' | 'word_count_tier'>,
  globalTierKey?: string,
): number | null {
  const level = node.level || 3;
  // L1/L2 不参与字数控制
  if (level < 3) return null;

  // 1) 单节点精确覆盖
  if (node.target_words && node.target_words > 0) {
    return node.target_words;
  }

  // 2) 单节点档位覆盖
  if (node.word_count_tier) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === node.word_count_tier);
    if (tier) return tier.targetWords;
  }

  // 3) 全局档位
  if (globalTierKey) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === globalTierKey);
    if (tier) return tier.targetWords;
  }

  // 4) L3 自身正文（方案三 2026-10-09）：300 ± 20%（240~360）
  //    四档 800/1200/2000/2500 作为「L3 子树总字数」口径，仅存在下级目录章节
  //   （L4+）时生效；无下级的 L3 以自身正文 300 字为唯一口径。
  if (level === 3) {
    return getL3OwnTarget();
  }

  // 5) 默认档位
  return WORD_COUNT_TIERS.find(t => t.isDefault)?.targetWords ?? 1200;
}

/** 按方案三口径解析节点字数容差：L3 自身正文 ±20%，四档 ±10%，自定义目标 ±10% */
export function resolveToleranceForNode(
  node: Pick<SectionTreeNode, 'level' | 'target_words' | 'word_count_tier'>,
): number {
  if (node.word_count_tier) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === node.word_count_tier);
    return tier?.toleranceRatio ?? 0.10;
  }
  if (node.level === 3) {
    return L3_OWN_TOLERANCE_RATIO;
  }
  return 0.10;
}

/** 计算字数达标区间：[floor(target × (1 - tol)), ceil(target × (1 + tol))] */
export function getWordCountRange(targetWords: number | null | undefined, toleranceRatio: number): [number, number] {
  if (!targetWords || targetWords <= 0) return [0, 0];
  return [Math.floor(targetWords * (1 - toleranceRatio)), Math.ceil(targetWords * (1 + toleranceRatio))];
}

/** 判断字数是否在 ±10% 浮动范围内（达标判定，L3 自身正文用 ±20%） */
export function isWithinTolerance(wordCount: number, targetWords: number, toleranceRatio = 0.10): boolean {
  if (!targetWords || wordCount <= 0) return false;
  const lo = Math.floor(targetWords * (1 - toleranceRatio));
  const hi = Math.ceil(targetWords * (1 + toleranceRatio));
  return wordCount >= lo && wordCount <= hi;
}


/**
 * 单章节的「兜底扩写」阈值（2026-10-08：按三级目录档位判定）。
 * L1/L2 返回 0（不参与扩写）；L3+ 按档位目标 ×80%，不超过平铺阈值，保底 200 字。
 */
export function expandWordThreshold(
  node: Pick<SectionTreeNode, 'target_words' | 'level' | 'word_count_tier'>,
  flatMinimum: number,
  _level2Default?: number, // [已废弃] 保留以兼容旧调用
  globalTierKey?: string,
): number {
  const level = node.level || 3;
  // L1/L2 不参与扩写
  if (level < 3) return 0;

  // 解析有效目标字数
  const target = resolveTargetForNode(node, globalTierKey) ?? 1200;
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
 * 2026-10-08：按三级目录档位判定达标线（L1/L2 不参与）。
 */
export function getWordDeficientStats(
  nodes: SectionTreeNode[],
  flatMinimum: number,
  _level2Default?: number, // [已废弃] 保留以兼容旧调用
  globalTierKey?: string,
): WordDeficientStats {
  let count = 0;
  let deficitWords = 0;
  const walk = (list: SectionTreeNode[]) => {
    for (const n of list) {
      if (!n.children || n.children.length === 0) {
        if (n.status === "completed" && n.has_content) {
          const threshold = expandWordThreshold(n, flatMinimum, _level2Default, globalTierKey);
          if (threshold > 0 && n.word_count < threshold) {
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
export function upsertSectionProgressItem<T extends { section_id: number; progress?: number; status?: string }>(
  prev: T[],
  item: T,
): T[] {
  const existing = prev.findIndex((p) => p.section_id === item.section_id);
  const clamp = (n: unknown) => {
    const v = Number(n);
    if (!Number.isFinite(v)) return 0;
    return Math.max(0, Math.min(100, Math.round(v)));
  };
  const merged: T = { ...item };
  if (merged.progress !== undefined) {
    // BUG-FIX 2026-10-08（P1 进度回退）：原实现直接覆盖 progress。
    // 后端重试会为同一章节重发 status="generating"（progress 0），
    // 于是条目从 completed/100 掉回 0 —— 单章节百分比与 ProgressPanel 汇总一起倒退。
    // 改为「单调不降 + 钳到 [0,100]」：进入终态后不再回退，重试期间只会上探。
    merged.progress = Math.max(clamp(item.progress), clamp((prev[existing] as T | undefined)?.progress));
  }
  if (existing >= 0) {
    const updated = [...prev];
    updated[existing] = { ...updated[existing], ...merged };
    return updated;
  }
  return [...prev, merged];
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
  // BUG-FIX 2026-10-08：缺少 status 字段（旧后端）不再默认为成功，
  // 而是返回 "parse_failed" 让用户知情，避免静默丢失解析失败的文件。
  // 注意：后端正常响应总是包含 status 字段，仅旧版本后端可能缺失。
  if (res.status === undefined || res.status === null) return "parse_failed";
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

export const DEFAULT_CONSISTENCY_SEVERITY = "medium";

const BUILTIN_CONSISTENCY_SEVERITY_META: Record<string, ConsistencySeverityMeta> = {
  high: { color: "red", label: "严重" },
  medium: { color: "orange", label: "中等" },
  low: { color: "blue", label: "轻微" },
};

// ENHANCE 2026-10-08（统一命名空间）：本表与后端
// `app/services/consistency_rules.py` 的 `SEVERITY_META` 是**同一份定义的两个副本**，
// 且互不引用 —— 后端调了文案/配色前端不会跟着变。现支持运行时用
// `GET /consistency/rules` 的快照覆盖（`applyConsistencySeverityMeta`），
// 拉取失败则继续用静态兜底：既统一口径，又不牺牲离线/首屏可用性。
let CONSISTENCY_SEVERITY_META: Record<string, ConsistencySeverityMeta> = {
  ...BUILTIN_CONSISTENCY_SEVERITY_META,
};

/**
 * 用后端规则快照覆盖本地静态表（`GET /consistency/rules` 的 severity.meta）。
 *
 * 传 null / 空对象 / 结构非法时保持现状并返回 false —— 绝不把映射清空，
 * 那会让所有严重度标签失去文案。
 */
export function applyConsistencySeverityMeta(
  meta: Record<string, ConsistencySeverityMeta> | null | undefined,
): boolean {
  if (!meta || typeof meta !== "object") return false;
  const entries = Object.entries(meta).filter(
    ([key, v]) =>
      !!key &&
      !!v &&
      typeof (v as ConsistencySeverityMeta).label === "string" &&
      typeof (v as ConsistencySeverityMeta).color === "string",
  );
if (!entries.length) return false;
  // 逐项**新建**对象，不保留调用方传入的引用 —— 否则调用方事后改写自己那份
  // 会连带改掉模块内部状态（`getConsistencySeverityMeta` 同此坑，已一并深拷贝）。
  CONSISTENCY_SEVERITY_META = Object.fromEntries(
    entries.map(([key, v]) => [
      key,
      { label: (v as ConsistencySeverityMeta).label, color: (v as ConsistencySeverityMeta).color },
    ]),
  ) as Record<string, ConsistencySeverityMeta>;
  return true;
}

/** 读取当前生效的映射（深拷贝：外部改写返回对象不会影响内部状态）。 */
export function getConsistencySeverityMeta(): Record<string, ConsistencySeverityMeta> {
  const out: Record<string, ConsistencySeverityMeta> = {};
  for (const [k, v] of Object.entries(CONSISTENCY_SEVERITY_META)) {
    out[k] = { label: v.label, color: v.color };
  }
  return out;
}

/** 恢复内置静态兜底（测试隔离用）。 */
export function resetConsistencySeverityMeta(): void {
  CONSISTENCY_SEVERITY_META = { ...BUILTIN_CONSISTENCY_SEVERITY_META };
}

export function mapConsistencySeverity(severity: string): ConsistencySeverityMeta {
  return (
    CONSISTENCY_SEVERITY_META[severity] ||
    CONSISTENCY_SEVERITY_META[DEFAULT_CONSISTENCY_SEVERITY] || {
      color: "orange",
      label: "中等",
    }
  );
}

// ============================================================
// 审核与预检模块 —— 冲突来源解析（组件级可测化 2026-10-08）
//
// 后端 `consistency_auditor._collect_fact_instances` 产出的 source 字符串有三种形态：
//   "section_12 (第三章 施工方案)"  → 可点击深链到该章节
//   "summary (摘要)"               → 不可点（项目备注/投标人/行业）
//   "global_facts (已确认事实基准)" → 不可点（全局事实表）
// 此前前端把三者一律渲染成不可点的 Tag，用户无法从冲突跳到 offending 章节。
// ============================================================

export interface ConflictSourceRef {
  /** 原始来源字符串（展示用） */
  raw: string;
  /** 章节标题（从括号里解析；无则为空串） */
  title: string;
  /** 章节 id；非章节来源为 null */
  sectionId: number | null;
  /** 是否可点击深链 */
  linkable: boolean;
}

const _SECTION_SOURCE_RE = /^section_(\d+)\s*(?:\((.*)\))?$/;

/**
 * 解析一致性冲突的 `sources` 数组，拆出可深链的章节引用。
 *
 * 纯函数、无副作用，供组件与测试共用。对无法识别的来源一律返回
 * `sectionId: null, linkable: false`（照常展示文本，不抛错）。
 */
export function parseConflictSources(sources: string[] | null | undefined): ConflictSourceRef[] {
  if (!Array.isArray(sources)) return [];
  return sources.map((raw) => {
    const text = String(raw ?? "");
    const m = _SECTION_SOURCE_RE.exec(text.trim());
    if (!m) {
      return { raw: text, title: "", sectionId: null, linkable: false };
    }
    const sectionId = Number(m[1]);
    if (!Number.isFinite(sectionId) || sectionId <= 0) {
      return { raw: text, title: m[2] || "", sectionId: null, linkable: false };
    }
    return { raw: text, title: m[2] || "", sectionId, linkable: true };
  });
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

