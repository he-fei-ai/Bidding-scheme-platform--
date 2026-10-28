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
 *
 * 边界说明（2026-10-13 · 与后端编号体系的关系）：本函数产出**前端层级式
 * 文档标题号**（L1~L8 逐层重置，`deriveHeadingPath` + `formatV21Heading`），
 * 仅用于目录 / 章节编辑界面的展示。后端另有一套**位置式路径号**
 * （`backend/app/services/ai/outline_common.py::child_number` →
 * `sections.get_section_path`，形如 `1.2.3`），服务于生成编排 / 落库 / 门禁
 * 等内部场景。二者用途不同、本就不必逐位相同：正文内嵌编号在生成 / 保存时
 * 即被剥离，文档编号在导出 / 展示时按当前树结构现算。请勿为「对齐」把本展示
 * 号改成后端位置路径号——那会是展示回归。
 */
function collectHeadingPaths(
  nodes: readonly SectionTreeNode[],
  parentPath: number[],
  result: Map<number, number[]>,
): void {
  // BUG-FIX 2026-10-11（BUG-08 · 深递归爆栈）：原实现为原生深度递归，2000 层链式
  // 目录会抛 `RangeError: Maximum call stack size exceeded`（见
  // utils.outlineNumbering.test.ts 的 2000 层回归用例）。改为显式栈驱动的先序遍历，
  // 语义与递归版**完全一致**：逐层计数器（levelCounters）作用域仍限于单个兄弟数组，
  // result.set 的先序覆盖顺序不变，path 派生口径不变。
  type Frame = {
    nodes: readonly SectionTreeNode[];
    idx: number;
    parentPath: number[];
    counters: Map<number, number>;
  };
  const stack: Frame[] = [
    { nodes: Array.isArray(nodes) ? nodes : [], idx: 0, parentPath, counters: new Map() },
  ];
  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    if (frame.idx >= frame.nodes.length) {
      stack.pop();
      continue;
    }
    const node = frame.nodes[frame.idx];
    frame.idx += 1;
    if (!node || typeof node !== "object") continue;
    const level = normalizeNodeLevel(node.level);
    // 当前层级的计数（frame.counters 为本兄弟数组的局部计数器，同递归版语义）
    const currentCount = (frame.counters.get(level) || 0) + 1;
    frame.counters.set(level, currentCount);

    // 重置比当前层级深的所有计数器
    for (let l = level + 1; l <= 8; l++) {
      frame.counters.set(l, 0);
    }

    // V3.0 构建 path（与后端 _format_outline_number 一致；parentPath 携带祖先编号）
    const path = deriveHeadingPath(level, currentCount, frame.parentPath, frame.counters);

    result.set(node.id, path);

    if (Array.isArray(node.children) && node.children.length > 0) {
      stack.push({ nodes: node.children, idx: 0, parentPath: path, counters: new Map() });
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
  // BUG-FIX 2026-10-11（BUG-08 收尾 · 深递归爆栈）：原实现为原生递归
  // （`annotateInto(node.children, ...)`），2000 层链式目录会抛
  // `RangeError: Maximum call stack size exceeded`（见 utils.outlineNumbering.test.ts
  // 的 2000 层回归用例；`collectHeadingPaths` 已于同日改为显式栈，本函数是漏改的
  // 并行副本）。现改为**显式栈驱动的先序遍历**，语义与递归版完全一致：每层
  // levelCounters 作用域仍限于单个兄弟数组，path 派生口径不变；子级写入独立的
  // childrenOut（与递归版同一份数组引用），不影响兄弟在 out 中的先后，展示编号不变。
  type Frame = {
    nodes: readonly SectionTreeNode[];
    idx: number;
    parentPath: number[];
    levelCounters: Map<number, number>;
    sink: Array<SectionTreeNode & { number?: string }>;
  };
  const stack: Frame[] = [
    {
      nodes: Array.isArray(nodes) ? nodes : [],
      idx: 0,
      parentPath,
      levelCounters: new Map(),
      sink: out,
    },
  ];
  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    if (frame.idx >= frame.nodes.length) {
      stack.pop();
      continue;
    }
    const node = frame.nodes[frame.idx];
    frame.idx += 1;
    if (!node || typeof node !== "object") continue;
    const level = normalizeNodeLevel(node.level);
    const currentCount = (frame.levelCounters.get(level) || 0) + 1;
    frame.levelCounters.set(level, currentCount);
    for (let l = level + 1; l <= 8; l++) {
      frame.levelCounters.set(l, 0);
    }
    const path = deriveHeadingPath(level, currentCount, frame.parentPath, frame.levelCounters);
    const childrenOut: Array<SectionTreeNode & { number?: string }> = [];
    frame.sink.push({
      ...node,
      number: formatV21Heading(level, path, ""),
      children: childrenOut as SectionTreeNode[],
    });
    if (Array.isArray(node.children) && node.children.length > 0) {
      stack.push({
        nodes: node.children,
        idx: 0,
        parentPath: path,
        levelCounters: new Map(),
        sink: childrenOut,
      });
    }
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
*  让整个审核弹窗白屏。这里统一转成字符串并裁掉首尾空白。
 *
 * BUG-FIX 2026-10-09：此前本函数是模块私有，但 OutlineEditorPage 的
 * `handleReviewConfirm.applyTitles` 也调用它（标题非字符串时的兜底分支），
 * 却未从 utils 导入 → 点击「V5.1 审核确认」时抛
 * `ReferenceError: normalizeNodeTitle is not defined`，编辑后的目录永远提交
 * 不上去，V5.1 流程只能等 `_wait_for_v51_review` 的 3600s 超时。
 * 现在正式导出，供审核确认链路复用。
 */
export function normalizeNodeTitle(raw: unknown): string {
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

  // BUG-FIX 2026-10-11（BUG-08 收尾 · 深递归爆栈）：原实现 `children:
  // outlineDictToTreeNodes(n.children, derivedLevel + 1, seen)` 是原生递归，
  // 2000 层链式目录直接抛 `RangeError: Maximum call stack size exceeded`
  // （utils.outlineNumbering.test.ts 的 2000 层回归用例即在此处炸；
  // collectHeadingPaths / annotateInto 已于同日改为显式栈，本函数是漏改的
  // 并行副本）。现改为**显式栈驱动的先序遍历**，与递归版语义逐条对齐：
  //   · 先序遍历顺序完全一致（节点自身先分配 id 并入 sink，其子树随后
  //     整体处理，再回到下一个兄弟）→ allocTempNodeId / seen.add 的调用
  //     次序不变，临时 ID 分配结果与递归版逐字节相同；
  //   · 每个兄弟数组独立维护 takenOrders + cursor（resolveOrder 的
  //     「1 起找空位、避开已占用值」口径不变）；
  //   · derivedLevel = min(depth, 8) 的封顶规则不变；
  //   · seen 集合跨整树共享（闭包捕获），与递归版传参语义一致。
  type Frame = {
    nodes: any[];
    idx: number;
    depth: number;
    sink: SectionTreeNode[];
    takenOrders: Set<number>;
    cursor: number;
  };

  const makeFrame = (list: any[], d: number, sink: SectionTreeNode[]): Frame => {
    // 先算出本层所有节点的 sort_order：已显式给出的保留，缺省的从 1 起找空位
    const takenOrders = new Set<number>();
    for (const n of list) {
      const so = n && typeof n === "object" ? Number(n.sort_order) : NaN;
      if (Number.isInteger(so) && so > 0) takenOrders.add(so);
    }
    return { nodes: list, idx: 0, depth: d, sink, takenOrders, cursor: 1 };
  };

  const rootOut: SectionTreeNode[] = [];
  const stack: Frame[] = [makeFrame(nodes, depth, rootOut)];

  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    if (frame.idx >= frame.nodes.length) {
      stack.pop();
      continue;
    }
    const n = frame.nodes[frame.idx];
    frame.idx += 1;
    // 与原实现 .filter(对象) 等价：非对象节点跳过，不占用 id / sort_order
    if (n === null || n === undefined || typeof n !== "object") continue;

    const derivedLevel = frame.depth > 8 ? 8 : frame.depth;

    const ownId =
      typeof n.id === "number" && Number.isFinite(n.id) && n.id > 0 ? n.id : null;
    let id = ownId;
    if (id === null || seen.has(id)) {
      // 兜底：后端若下发重复/非法 id，也不得与同树其它节点撞号
      id = allocTempNodeId();
    }
    seen.add(id);

    // sort_order 解析（原 resolveOrder，作用域为本兄弟数组）
    let sortOrder: number;
    const soNum = Number(n.sort_order);
    if (Number.isInteger(soNum) && soNum > 0) {
      sortOrder = soNum;
    } else {
      while (frame.takenOrders.has(frame.cursor)) frame.cursor += 1;
      sortOrder = frame.cursor;
      frame.takenOrders.add(sortOrder);
    }

    const childrenOut: SectionTreeNode[] = [];
    frame.sink.push({
      id,
      title: normalizeNodeTitle(n.title),
      level: Number.isInteger(Number(n.level)) && Number(n.level) > 0 ? Number(n.level) : derivedLevel,
      sort_order: sortOrder,
      section_type: typeof n.section_type === "string" ? n.section_type : "text",
      scoring_point_id: n.scoring_point_id ?? null,
      // 2026-10-20 遗留 3/5：Aligned 锁定/正文模式随 SSE 目录字典透传到
      // 审核弹窗（此前构造器只挑选固定字段，locked 在此被静默丢弃）。
      locked: n.locked === true,
      content_mode: typeof n.content_mode === "string" ? n.content_mode : null,
      // BUG-FIX 2026-10-09（跨模块数据链 / 章节状态机）：
      // 原实现硬编码 `status: "active"` —— 这是一个全仓库无人写入、
      // 也不被任何消费者识别的幽灵状态。它让目录审核弹窗里的每个节点
      // 都被判为「非 pending/非 failed」，于是 collectLeafIds(true)
      // 选不中它们，「补全生成」对刚生成的大纲完全失效。
      // 现改为透传后端真实状态；后端未给或给出未知值时归一化为 'pending'
      //（与 sections 表默认值、以及目录 INSERT 的写入值一致）。
      status: normalizeSectionStatus(n.status, n.has_content),
      children: childrenOut,
      has_content: Boolean(n.has_content),
      word_count: Number.isInteger(Number(n.word_count)) ? Number(n.word_count) : 0,
    } as SectionTreeNode);

    if (Array.isArray(n.children) && n.children.length > 0) {
      stack.push(makeFrame(n.children, derivedLevel + 1, childrenOut));
    }
  }

  return rootOut;
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

/**
 * 章节状态归一化（BUG-FIX 2026-10-09）
 *
 * 后端 sections 表的真实状态机只有五个取值：
 *   pending → generating → completed | failed | cancelled
 * （写入方：sse_db_ops.py 的 _save_section_to_db / _mark_sections_generating /
 *   _rollback_dirty_sections，task_control.py 的 stop_generation；
 *   表默认值 'pending' 见 db.py:914）
 *
 * 历史脏数据里存在 'active'：目录生成的 INSERT 曾硬编码该值。它不属于上述集合，
 * 导致 collectLeafIds(filterPendingFailed=true) 选不中这些章节，
 * 「补全生成」对刚生成的目录完全失效。此处做单向收敛：
 *   · 已知状态原样透传；
 *   · 'active'（或任何未知值）按「是否有正文」归一 —— 有正文记 completed，
 *     否则记 pending，从而重新进入可补全集合。
 * 后端已同步修正写入侧并提供一次性迁移（db_migrations.py 的 active→pending），
 * 此处作为前端侧的兜底，保证新旧数据都能正确渲染与选取。
 */
const KNOWN_SECTION_STATUSES = new Set([
  "pending",
  "generating",
  "completed",
  "failed",
  "cancelled",
]);

export function normalizeSectionStatus(raw: unknown, hasContent?: boolean): SectionTreeNode["status"] {
  const s = typeof raw === "string" ? raw : "";
  if (KNOWN_SECTION_STATUSES.has(s)) return s as SectionTreeNode["status"];
  return hasContent ? "completed" : "pending";
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

/**
 * G7（2026-10-28 遗留加固）：把 WORD_COUNT_TIERS 从后端 `/word-count/tiers`
 * 单一事实来源（word_count_tiers 表，DB 优先）同步到本数组。
 *
 * 此前前端硬编码 800/1200/2000/2500 ±10%，后端已支持运维改 DB 档位，但前端
 * 从未消费（api/index.ts 的 getWordCountTiers 定义后无调用方）——管理员改档位
 * 后前端仍按静态值展示/判定，与后端校验页口径漂移。
 *
 * 策略：**原地 splice 替换**（保持数组引用不变，所有 import 方无需改引用即见
 * 新值）；拉取失败/结构非法则静默保留静态默认（fail-soft，向后兼容）。
 * 静态默认与后端默认逐字一致（800/1200/2000/2500，±10%），零行为变更。
 */
export async function syncWordCountTiers(): Promise<boolean> {
  try {
    // 动态 import 避免 utils <-> api 循环依赖（api/index.ts 依赖 utils 类型）。
    const { getWordCountTiers } = await import("./api");
    const res = await getWordCountTiers();
    const tiers = Array.isArray(res?.tiers) ? res.tiers : null;
    if (!tiers || tiers.length === 0) return false;
    const mapped: WordCountTier[] = [];
    for (const t of tiers) {
      const target = Number(t?.target_words);
      const ratio = Number(t?.tolerance_ratio);
      if (!t?.key || !Number.isFinite(target) || target <= 0 || !Number.isFinite(ratio)) {
        continue; // 结构非法条目跳过，不整体回退
      }
      mapped.push({
        key: t.key as WordCountTier['key'],
        label: String(t?.label ?? t.key),
        targetWords: target,
        toleranceRatio: ratio,
        isDefault: Boolean(t?.is_default),
      });
    }
    if (mapped.length === 0) return false;
    // 原地替换：保持数组引用不变，让所有既有 import 方（SectionEditorPage 等）
    // 无需任何改动即消费新值。
    WORD_COUNT_TIERS.splice(0, WORD_COUNT_TIERS.length, ...mapped);
    return true;
  } catch (err) {
    // fail-soft：网络/后端异常时保留静态默认，绝不因同步失败影响页面。
    console.warn("[WordCountTiers] 同步后端档位失败，使用静态默认:", err);
    return false;
  }
}

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
  node: Pick<SectionTreeNode, 'level' | 'target_words' | 'word_count_tier'> & { children?: SectionTreeNode[] },
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

  const hasChildren = Array.isArray(node.children) && node.children.length > 0;

  // 3) 含下级的 L3：四档（全局档位）作为「子树总字数」
  if (level === 3 && hasChildren) {
    if (globalTierKey) {
      const tier = WORD_COUNT_TIERS.find(t => t.key === globalTierKey);
      if (tier) return tier.targetWords;
    }
    return WORD_COUNT_TIERS.find(t => t.isDefault)?.targetWords ?? 1200;
  }

  // 4) 叶子 L3 自身正文（方案三 2026-10-09）：固定 300 ± 20%（240~360），
  //    不受四档下拉影响；四档仅对含下级目录（L4+）的 L3 作为子树总字数生效。
  if (level === 3) {
    return getL3OwnTarget();
  }

  // 5) L4+ 深层叶子：跟随全局档位，回落默认档位
  if (globalTierKey) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === globalTierKey);
    if (tier) return tier.targetWords;
  }
  return WORD_COUNT_TIERS.find(t => t.isDefault)?.targetWords ?? 1200;
}

/**
 * 按方案三混合口径解析节点字数容差：
 *   - 单节点 word_count_tier → 该档位 ±10%
 *   - 叶子 L3（无下级/无覆盖）→ L3 自身正文 ±20%
 *   - 含下级的 L3（子树总字数）→ ±10%
 *   - 自定义精确目标 / L4+ → ±10%
 */
export function resolveToleranceForNode(
  node: Pick<SectionTreeNode, 'level' | 'target_words' | 'word_count_tier'> & { children?: SectionTreeNode[] },
): number {
  if (node.word_count_tier) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === node.word_count_tier);
    return tier?.toleranceRatio ?? 0.10;
  }
  const hasChildren = Array.isArray(node.children) && node.children.length > 0;
  if ((node.level || 3) === 3) {
    // 叶子 L3 → ±20%；含下级的 L3（子树总字数）→ ±10%
    return hasChildren ? 0.10 : L3_OWN_TOLERANCE_RATIO;
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
 * 字数明细行的「达标区间下界」（BUG-FIX 2026-10-09）
 *
 * `WordCountPage` 的「达成进度」列需要算出「还缺多少字」。此前该列直接用
 * `target_words - word_count`（零容差），与后端 `on_target`（含容差判定）
 * 相互矛盾。此处按与后端 `word_count.py::_target_bounds` 相同的优先级
 * 推导下界：
 *   1. 有档位 → 档位 tolerance_ratio（±10%，可被管理员配置覆盖）
 *   2. L3 自身正文 → L3_OWN_TOLERANCE_RATIO（±20%）
 *   3. 其它 → 0.10
 *
 * 说明：真正的达标结论始终以服务端 `on_target` 为准（见
 * `_is_within_tolerance` 的实现），本函数只用于**展示**缺字数，
 * 使红色进度条的语义与后端判定对齐。
 */
export function resolveProgressLowerBound(item: {
  level?: number | null;
  target_words?: number | null;
  word_count_tier?: string | null;
  tier_target?: number | null;
}): number {
  const target = item.target_words ?? 0;
  if (!target || target <= 0) return 0;
  const tierKey = item.word_count_tier;
  if (tierKey) {
    const tier = WORD_COUNT_TIERS.find(t => t.key === tierKey);
    const tol = tier?.toleranceRatio ?? 0.10;
    return Math.floor(target * (1 - tol));
  }
  if ((item.level ?? 3) === 3) {
    return Math.floor(target * (1 - L3_OWN_TOLERANCE_RATIO));
  }
  return Math.floor(target * (1 - 0.10));
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

/**
 * V5.1 目录生成 13 个阶段标签（单一事实源，页面 UI 与进度归约共用）。
 * 2026-10-19 起与后端 sse_outline.py `_phase_callback` 的 step_map 对齐
 * （《目录生成工作流程优化方案》第五节：并发 + 增量 + 智能自检）：
 *   1  读取项目资料与目录库样本      8  AI 局部自检
 *   2  AI 生成一级目录              9  规则引擎全局校验
 *   3  L1 校验                     10 程序自动修复与重排编号
 *   4  并发生成二级目录            11 生成质量评分报告
 *   5  L2 校验                     12 增量保存
 *   6  并发生成三级目录            13 最终审核通过
 *   7  L3 局部校验
 */
export const OUTLINE_STEPS_V51: string[] = [
  "1、读取项目资料与目录库样本",
  "2、AI 生成一级目录",
  "3、L1 校验（长度/重复/ID连续性）",
  "4、并发生成二级目录（4-6个/章）",
  "5、L2 校验（数量/唯一性）",
  "6、并发生成三级目录（3-5个/二级）",
  "7、L3 局部校验",
  "8、AI 局部自检（仅冲突项）",
  "9、规则引擎全局校验",
  "10、程序自动修复与重排编号",
  "11、生成质量评分报告",
  "12、增量保存（每级完成即落库）",
  "13、最终审核通过",
];

export interface OutlineGeneratingPatch {
  outlineStep?: number;
  progressPercent?: number;
  progressMessage: string;
  /** 进度发生回退（重连重放 / 乱序到达），UI 应保持原值不动 */
  regressed?: boolean;
}

export interface OutlineProgressFloor {
  /** 已展示的最高步骤；新步骤更小时视为回退 */
  step?: number;
  /** 已展示的最高百分比；新百分比更小时视为回退 */
  percent?: number;
}

/**
 * BUG-OUTLINE-03 修复口径：将 generating SSE 事件归约为 UI 进度状态增量。
 * 所有 phase 统一按 outline_step 计算百分比，消除 outline_phase 与其他
 * phase（context/saving/v51）之间的进度跳跃。心跳事件仅更新提示文案，
 * 不改变步骤与百分比。返回对象中缺省的字段表示“保持当前值不变”。
 *
 * BUG-FIX 2026-10-12（单调性下限 floor）：本函数此前是**绝对赋值**，
 * 只要收到一条 `outline_step` 更小的事件（断线重连时服务端重放
 * `state['last_event']`，恰恰就是一条旧进度事件），进度条就会**倒退** ——
 * 用户看到 AI 明明在第 6 步，界面却退回第 3 步的 25%。
 * 传入 `floor`（当前已展示值）后，低于下限的增量会被丢弃并置 `regressed`，
 * 调用方可据此打日志；不传 `floor` 时行为与改动前逐字一致，向后兼容。
 */
export function reduceOutlineGeneratingEvent(
  data: { phase?: string; outline_step?: number; message?: string },
  stepLabels: string[] = OUTLINE_STEPS_V51,
  floor: OutlineProgressFloor = {},
): OutlineGeneratingPatch {
  const totalSteps = stepLabels.length;
  const pct = (step: number) =>
    Math.min(100, Math.round(((step - 1) / totalSteps) * 100));
  const minStep = typeof floor.step === "number" ? floor.step : 0;
  const minPct = typeof floor.percent === "number" ? floor.percent : 0;

  /** 低于下限则退回 undefined（保持现值）并标记 regressed */
  const clamp = (
    step: number,
    percent: number,
  ): Pick<OutlineGeneratingPatch, "outlineStep" | "progressPercent" | "regressed"> => {
    if (step < minStep || percent < minPct) {
      return { regressed: true };
    }
    return { outlineStep: step, progressPercent: percent };
  };

  if (data.phase === "outline_phase" && data.outline_step) {
    const c = clamp(data.outline_step, pct(data.outline_step));
    return {
      ...c,
      progressMessage: data.message || stepLabels[data.outline_step - 1] || "",
    };
  }
  if (data.phase === "heartbeat") {
    return { progressMessage: data.message || "AI 正在生成中，请稍候..." };
  }
  // 其他阶段（context、saving、v51 等）：有 outline_step 时按其计算进度
  if (data.outline_step) {
    return {
      ...clamp(data.outline_step, pct(data.outline_step)),
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
// 全局事实一致性审计 —— 结果判定纯函数（组件级可测化 2026-10-15）
// ============================================================

/** POST /facts/audit 的判定相关字段（仅取判定所需的子集） */
export interface AuditOutcomeInput {
  items?: Array<{ status?: string }>;
  audit_status?: "ok" | "partial" | "failed" | (string & {});
  audited_sections?: number;
  total_sections?: number;
}

/** 判定结果：type 对应 antd message 的方法名，duration 单位为秒 */
export interface AuditVerdict {
  type: "success" | "warning" | "error";
  text: string;
  duration: number;
}

/**
 * 依据审计响应判定用户可见的结论。
 *
 * BUG-FIX 2026-10-15（P-4 静默通过）：判定逻辑原内联在 SummaryReviewPage 的
 * 按钮 onClick 里，其 `else` 分支把「不是 failed 也不是 partial」全部当成通过。
 * 一旦后端漏回 audit_status（契约漂移）或将来新增取值，界面就会**谎报**
 * 「一致性检查通过」—— 最危险的失败形态：用户以为已核验，实际审计可能
 * 根本没跑。现在抽成纯函数：① 可脱离 DOM 单测；② 只有显式 'ok' 才播报通过。
 */
export function classifyAuditOutcome(r: AuditOutcomeInput): AuditVerdict {
  const items = r.items ?? [];
  const inconsistent = items.filter((i) => i?.status === "inconsistent").length;
  const missing = items.filter((i) => i?.status === "missing").length;
  const status = r.audit_status;

  if (status === "failed") {
    return {
      type: "error",
      text: "一致性审计未能执行（AI 调用全部失败），本次结果不代表正文无矛盾",
      duration: 8,
    };
  }
  if (inconsistent > 0) {
    return {
      type: "warning",
      text: `发现 ${inconsistent} 处事实不一致`,
      duration: 6,
    };
  }
  if (status === "partial") {
    return {
      type: "warning",
      text: `审计未覆盖全部正文（${r.audited_sections}/${r.total_sections} 章），未发现不一致不等于全部一致`,
      duration: 8,
    };
  }
  if (missing > 0) {
    return {
      type: "warning",
      text: `正文未发现矛盾，但有 ${missing} 项事实在正文中没有对应表述`,
      duration: 6,
    };
  }
  if (status === "ok") {
    return { type: "success", text: "一致性检查通过", duration: 3 };
  }
  // 未知或缺失：不得谎报通过
  return {
    type: "warning",
    text: `一致性审计状态未知（${status ?? "字段缺失"}），本次结果不代表正文无矛盾`,
    duration: 8,
  };
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
 * 文件解析状态的**前端唯一口径**。
 *
 * BUG-FIX 2026-10-15（TD-A/TD-B/P-5 状态口径漂移）：
 *
 * - 后端 `uploaded_files.status` 实际写入 4 个值：`processing` / `success` /
 *   `failed` / `pending`；`file_parse_tasks.status` 写入 3 个值：`processing` /
 *   `completed` / `failed`，但其 **DDL 默认值是 `'queued'`**（db_migrations.py），
 *   即任何绕过 `_create_parse_task` 直接 INSERT 的路径（历史脏数据、外部脚本、
 *   迁移回填）都可能留下一行 `queued`。此前本表**没有 queued 键**，
 *   `mapFileStatusTag('queued')` 会落到 `|| { color:"default", text: status }`
 *   兜底 —— 界面上直接显示英文原始值，用户看不出这是什么状态。
 * - `uploaded` / `extracting` / `extracted` 三个键后端**从不产生**（2026-09-18
 *   就修过一次），保留是为兼容历史数据；现加注释标注其来源，避免下一个人
 *   以为是现役状态而照抄。
 *
 * 新增状态时必须同时更新这里与 `FILE_STATUSES`（后者是断言集合，被单测锁定）。
 */
const FILE_STATUS_TAG_MAP: Record<string, FileStatusTag> = {
  success: { color: "success", text: "已提取" },
  processing: { color: "processing", text: "提取中" },
  pending: { color: "default", text: "待提取" },
  /** DDL 默认值，见上文说明 */
  queued: { color: "default", text: "排队中" },
  /** ↓↓↓ 以下 3 项后端已不产生，仅兼容历史数据（BUG-FIX 2026-09-18 P1）↓↓↓ */
  uploaded: { color: "default", text: "已上传" },
  extracting: { color: "processing", text: "提取中" },
  extracted: { color: "success", text: "已提取" },
  failed: { color: "error", text: "提取失败" },
};

/** 前端已收录的全部状态键（供静态一致性测试锁定，防止再次漏登记）。 */
export const FILE_STATUSES: readonly string[] = Object.freeze(
  Object.keys(FILE_STATUS_TAG_MAP),
);

export function mapFileStatusTag(status: string): FileStatusTag {
  return FILE_STATUS_TAG_MAP[status] || { color: "default", text: status };
}

/** 提取完成计数：成功状态为 "success"（兼容历史 "extracted"） */
export function countExtractedFiles(files: { status: string }[]): number {
  return files.filter((f) => f.status === "success" || f.status === "extracted")
    .length;
}

// ============================================================
// 上传解析模块 —— 在途状态判定（BUG-FIX 2026-10-26）
// ============================================================

/**
 * 「解析仍在执行中」的文件状态（**唯一口径**）。
 *
 * BUG-FIX 2026-10-26（幽灵任务）：此前 FileUploadPage 把 `pending` 也算作在途
 * （inFlight = processing || queued || pending），据此向 taskStore 注册 running
 * 任务。但后端 `uploaded_files.status` 里的 `pending` 只有一个写入方 ——
 * `clear_project_texts`（清空提取文本时把行重置为「待提取」），而它**不会**
 * 派发任何解析任务。于是「清空提取」会让顶栏凭空多出 N 条「文件解析 0%」，
 * 且永远不会进终态（用户只能靠离开页面触发 stopTracking 清理）。
 *
 * 后端语义对照：
 *   - `processing` —— 已派发后台解析，worker 尚未落终态；
 *   - `queued`     —— file_parse_tasks 的 DDL 默认值（历史脏行），无在途含义，
 *                     但保留在集合内以维持既有轮询语义，不新增行为；
 *   - `pending`    —— 「已入库、从未解析」的静态态，**不是**在途。
 */
export const IN_FLIGHT_FILE_STATUSES: readonly string[] = Object.freeze([
  "processing",
  "queued",
]);

/** 该文件是否仍有解析任务在跑（用于 taskStore 注册 / 轮询判定） */
export function isInFlightFileStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return IN_FLIGHT_FILE_STATUSES.indexOf(status) !== -1;
}

// ============================================================
// 上传解析模块 —— 路由参数解析（BUG-FIX 2026-10-26）
// ============================================================

/**
 * 路由参数 → 合法项目 ID（正整数）。
 *
 * BUG-FIX 2026-10-26：原实现 `id ? Number(id) : null` 对 `Number("1.5")` 返回
 * 1.5（truthy）、`Number("1abc")` 返回 NaN（被 falsy 兜住但理由不对），
 * 非整数会带着小数去请求 `/projects/1.5/files`。SummaryReviewPage 已有等价的
 * `!isNaN && > 0` 守卫，上传页缺同样的防线，现抽成纯函数共用口径。
 */
export function parseProjectId(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n <= 0) return null;
  return n;
}

// ============================================================
// 上传解析模块 —— 「进入要点提取」流程门禁（2026-10-26）
// ============================================================

/**
 * 是否允许从上传页进入「要点提取」（纯函数，组件级可测）。
 *
 * BUG-FIX 2026-10-26（流程入口零门禁）：此前判定只看 `files.length > 0`。
 * 用户只上传 1 张 OCR 失败的扫描件（status='failed'）也能一路点进提取页，
 * 直到点「生成摘要」才被后端 400「请先上传文件」拦住，且提取页没有返回上传页
 * 的引导 —— 空转一次 AI 提交流程才发现走错了门。
 * 现把「没有可用文本」这一事实提前到入口说清，并区分「还在解析」与「已失败」。
 */
export function checkReadyForExtraction(
  files: { status: string; file_role?: string }[],
): { ready: true } | { ready: false; reason: string } {
  const extracted = files.filter(
    (f) => f.status === "success" || f.status === "extracted",
  ).length;
  // BUG-FIX 2026-10-28（P2 数据链断点）：只要有任意 success 文件，原实现
  // 直接放行，即使**主文件仍在异步 OCR（processing）**。此时后端
  // latest_extracted_text 因主文件 extracted_text_path 为空，会把辅助文件
  // 200 行摘要顶替为「正文」喂给 AI，全文事实提取质量静默劣化。现拦截
  // processing/pending 的主文件（file_role==='main'），提示等待主文件解析。
  const mainInFlight = files.filter(
    (f) =>
      f.file_role === "main" &&
      (f.status === "processing" || f.status === "pending" || f.status === "parsing"),
  ).length;
  if (extracted > 0 && mainInFlight === 0) return { ready: true };
  if (mainInFlight > 0) {
    return {
      ready: false,
      reason: "主文件正在解析中，请等主文件解析完成后再进入要点提取",
    };
  }
  const inFlight = files.filter((f) => isInFlightFileStatus(f.status)).length;
  if (inFlight > 0) {
    return { ready: false, reason: "有文件正在解析中，请等解析完成后再进入要点提取" };
  }
  return {
    ready: false,
    reason: "没有已提取到文本的文件，请先点「重新提取」或重新上传可解析的文件",
  };
}

/**
 * 「主文件已换源、旧摘要/评分基于旧文本」的统一提示文案。
 *
 * BUG-FIX 2026-10-26：此前同一段文案在 FileUploadPage 里出现两次
 * （handleToggleRole 手动切角色路径 / handleUpload 上传挤主文件路径），
 * 历史上就是靠这种副本漂移出真实故障。集中到一处常量，两条路径共用。
 */
export const NEEDS_REANALYSIS_MESSAGE =
  "主文件已变更，请到「提取项目」重新生成摘要与评分点";

// ============================================================
// 上传解析模块 —— 批量上传交互状态机（组件级可测化 2026-10-05）
// 从 FileUploadPage.tsx 的 handleUpload / finish 内联逻辑抽取为纯函数，
// 使「批量上传状态错乱」类缺陷可以在无 DOM 环境下单测。
// ============================================================

/** 单份文件的上传结果分类 */
export type UploadOutcome = "ok" | "pending" | "parse_failed" | "duplicate" | "error";

/** 本批次聚合计数：ok=上传并解析成功；pending=已入库、后台异步解析中；parseFailed=HTTP 成功但解析失败；failed=请求失败 */
export interface UploadBatchTotals {
  ok: number;
  pending: number;
  parseFailed: number;
  /** G8（2026-10-28）：内容哈希去重命中数（未新增文件行） */
  duplicate: number;
  failed: number;
}

export const EMPTY_BATCH_TOTALS: UploadBatchTotals = {
  ok: 0,
  pending: 0,
  parseFailed: 0,
  duplicate: 0,
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
  // HARDENING 2026-10-16（批尾汇总语义差）：图片等 OCR 耗时格式自 R-3 起走后端
  // 异步解析，上传响应即时返回 status="processing"（文件已入库、Celery worker
  // 稍后落终态）。此前它落入 "ok"，批尾汇总说「成功 N 个」；若 OCR 随后失败，
  // 汇总与终态不一致 —— 用户以为这批文件都解析成功了。单列 "pending" 口径，
  // 让汇总如实区分「解析已完成」与「后台解析中」。
  if (res.status === "processing") return "pending";
  // G8（2026-10-28 后续建议#4）：后端 save_upload 内容哈希去重命中返回
  // status='duplicate'（HTTP 200）。此前落入 "ok" 被谎报「上传成功」，实际未新增
  // 文件行——用户困惑。单独归类为 "duplicate"，批尾汇总与单文件提示如实反馈。
  if (res.status === "duplicate") return "duplicate";
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
  // HARDENING 2026-10-16：pending 单独计数，不混入 ok（否则批尾汇总无法区分
  // 「解析已完成」与「后台解析中」）
  if (outcome === "pending") {
    return { ...totals, pending: totals.pending + 1 };
  }
  // G8：去重命中单独计数，不混入 ok（批尾汇总才不谎报「全部成功」）
  if (outcome === "duplicate") {
    return { ...totals, duplicate: totals.duplicate + 1 };
  }
  return { ...totals, ok: totals.ok + 1 };
}

/** 本批次已处理文件总数 */
export function totalBatchCount(totals: UploadBatchTotals): number {
  return totals.ok + totals.pending + totals.parseFailed + totals.duplicate + totals.failed;
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
  const { ok, pending, parseFailed, duplicate, failed } = totals;
  if (parseFailed === 0 && failed === 0) {
    // HARDENING 2026-10-16：含后台解析中的文件时如实说明，不再一律宣称
    // 「成功 N 个」（OCR 终态尚未落库，见 classifyUploadResult 注释）
    if (pending > 0) {
      return {
        type: "success",
        text: `批量上传完成，成功 ${ok} 个文件，${pending} 个正在后台解析${duplicate > 0 ? `，${duplicate} 个重复未上传` : ""}`,
      };
    }
    return {
      type: "success",
      text: `批量上传完成，成功 ${ok} 个文件${duplicate > 0 ? `，${duplicate} 个重复未上传` : ""}`,
    };
  }
  const parts: string[] = [`成功 ${ok} 个`];
  if (pending > 0) parts.push(`后台解析中 ${pending} 个`);
  if (parseFailed > 0) parts.push(`解析失败 ${parseFailed} 个`);
  if (duplicate > 0) parts.push(`重复 ${duplicate} 个`);
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

/**
 * 「清空已提取文本」结果文案与提示级别（M-5 2026-10-15）。
 *
 * 后端 DELETE /files/texts 在「texts/ 已真删、但 DB 状态重置失败」时仍返回
 * HTTP 200 + `status:'cleared'`，此前前端无条件 `message.success` 弹
 * 「已重置 0 个文件状态为待提取」—— 磁盘与库已经不一致，用户却看到成功，
 * 且列表继续显示「已提取」。现在 warning 非空即降级为告警并透出后端原因。
 */
export function formatClearTextsResult(r: {
  files_reset?: number | null;
  tasks_cleared?: number | null;
  files_kept_processing?: number | null;
  warning?: string | null;
}): { level: "success" | "warning"; text: string } {
  const warning = (r?.warning || "").trim();
  if (warning) return { level: "warning", text: warning };
  const base = `已重置 ${r?.files_reset ?? 0} 个文件状态为待提取，清理 ${r?.tasks_cleared ?? 0} 条旧解析记录`;
  // BUG-FIX 2026-10-26：后端不再把在途 processing 行重置为 pending，需明确告知
  // 用户这批文件未受本次清空影响（它们的解析仍会完成并重新落盘），否则会误以为
  // 「清空」把所有文件都置空了。老后端无该字段 → kept=0，文案与旧版逐字一致。
  const kept = r?.files_kept_processing ?? 0;
  return {
    level: "success",
    text:
      kept > 0 ? `${base}；${kept} 个正在解析中的文件未受影响` : base,
  };
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
 *
 * 2026-10-15（M-1 · 诊断字段消费）：`latest.error_message` 此前在类型里声明、
 * 接口里返回，却**没有任何消费方**。「解析成功但零文本」（空文件 / 纯图片扫描件
 * 且 OCR 未启用）这类非致命警告只落在文件行的 message 上，聚合状态条一律显示
 * 纯成功口径 —— 后端诊断字段静默失效，前端给出误导性成功。现按 latest 的
 * status/message 追加警告；failed 已有独立计数，不在此重复播报失败原因。
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
  const latest = s.latest || null;
  const latestNote = latest ? (latest.error_message || "").trim() : "";
  if (bad === 0) {
    return latestNote && latest?.status === "completed"
      ? `最近解析完成 ${done} 个（${latestNote}）`
      : `最近解析完成 ${done} 个文件`;
  }
  // BUG-FIX 2026-10-26（失败原因失明）：此前失败分支只播报计数，latest.error_message
  // 仅在「最近一条是 completed」时被消费 —— 批量上传全部失败时用户只看到
  // 「失败 N 个」而看不到原因（磁盘满 / 格式不支持 / OCR 不可用），只能逐个点
  // 预览排查。现失败分支同样透出最近一条失败原因。
  return latestNote && latest?.status === "failed"
    ? `最近解析完成 ${done} 个，失败 ${bad} 个（${latestNote}）`
    : `最近解析完成 ${done} 个，失败 ${bad} 个`;
}

// ============================================================
// 上传解析模块 —— 支持格式单一真相（TD6 2026-10-12）
// ============================================================

/**
 * 本地兜底 accept 列表（与后端 services/files.SUPPORTED_EXTENSIONS 一致）。
 *
 * 它只是**网络失败时的降级值** —— 正常路径下 Upload.Dragger 的 accept 由
 * `GET /projects/{id}/files/supported-formats` 现场下发（后端从
 * SUPPORTED_EXTENSIONS 生成，是唯一真相）。历史上前端把这份列表手抄在
 * 组件 JSX 里，与后端白名单之间没有任何联动也没有测试约束；后端曾在
 * 服务层早已支持 .wps / 图片 OCR 的情况下忘记放开路由白名单，导致这些
 * 格式「入口封死」完全无法上传。现在这条降级路径集中在 utils 一处，
 * 且组件测试会把它与后端常量逐项比对。
 */
export const FALLBACK_ACCEPT: string = [
  ".doc", ".docx", ".pdf", ".xls", ".xlsx", ".txt", ".md",
  ".ppt", ".pptx", ".html", ".htm", ".wps",
  ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
].join(",");

/**
 * 从后端下发的格式清单归一化出可用的 accept 字符串（纯函数，便于组件级测试）。
 *
 * 防御点：
 *   - 后端返回空/缺字段/畸形数组 → 回落 `FALLBACK_ACCEPT`（不能因为格式查询
 *     失败就让用户一个文件都传不了，这是「优化」不能变成「新故障」）。
 *   - 统一补点、统一小写、去重 —— 后端当前已返回规范化的 `accept`，
 *     但这里是前端唯一的最后一道防线，不假设上游永远正确。
 */
export function resolveAccept(
  remote: { accept?: string | null; extensions?: string[] | null } | null | undefined,
  fallback: string = FALLBACK_ACCEPT,
): string {
  const raw =
    typeof remote?.accept === "string" && remote.accept.trim()
      ? remote.accept
      : Array.isArray(remote?.extensions) && remote.extensions.length
        ? remote!.extensions!.join(",")
        : "";
  if (!raw) return fallback;
  const parts = raw
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .map((s) => (s.startsWith(".") ? s : `.${s}`));
  const uniq = Array.from(new Set(parts));
  return uniq.length ? uniq.join(",") : fallback;
}

/**
 * 从后端下发的格式清单解析出**精确**的单文件大小上限（MB，纯函数）。
 *
 * BUG-FIX 2026-10-26（前后端上限漂移）：后端 supported-formats 的
 * `max_file_size_mb` 是 `MAX_FILE_SIZE // (1024*1024)` 整数除法，运维把上限调成
 * 51MB / 25.5MB 这类非整 MB 值时前端拿到的是向下截断值，于是「前端放行、
 * 后端 400」的区间重新出现。优先级：精确浮点值 → 整 MB 值 → 字节值换算 → 本地兜底。
 * 老后端不下发 `max_file_size_exact_mb` 时行为与旧版完全一致。
 */
export function resolveUploadLimitMb(
  remote: {
    max_file_size_exact_mb?: number | null;
    max_file_size_mb?: number | null;
    max_file_size?: number | null;
  } | null | undefined,
  fallbackMb: number = 50,
): number {
  const exact = remote?.max_file_size_exact_mb;
  if (typeof exact === "number" && exact > 0) return exact;
  const mb = remote?.max_file_size_mb;
  if (typeof mb === "number" && mb > 0) return mb;
  const bytes = remote?.max_file_size;
  if (typeof bytes === "number" && bytes > 0) return bytes / (1024 * 1024);
  return fallbackMb;
}

/**
 * OCR 是否已明确关闭（纯函数）。
 *
 * BUG-FIX 2026-10-26（ocr_enabled 零消费）：后端 supported-formats 早在下发
 * `ocr_enabled`，前端却只消费了 `max_file_size_mb` —— OCR 关闭时前端仍把
 * png/jpg 放进 accept 并鼓励上传，用户必然得到「解析成功但文本为空」。
 * 只有后端**明确返回 false** 时才判定为关闭（缺字段/查询失败一律按可用处理，
 * 避免网络抖动把图片入口误封）。
 */
export function isOcrDisabled(
  remote: { ocr_enabled?: boolean | null } | null | undefined,
): boolean {
  return remote?.ocr_enabled === false;
}

// ============================================================
// 上传解析模块 —— 提取文本预览（激活 GET /files/{id}/text 死链）
// ============================================================

/**
 * 提取文本预览的分页裁剪（纯函数）。
 *
 * 为什么需要它：`GET /files/{id}/text` 此前**在前端零调用方**（api 层导出了
 * getFileText 却没有任何组件使用），用户无法看到系统究竟解析出了什么正文，
 * 「提取成功」但提不出要点的场景完全无法自查。这里在展示层做长度上限，
 * 避免把几十万字的招标文件正文整个塞进 Modal 导致页面卡死。
 *
 * @param text 服务端返回的原文
 * @param maxChars 展示上限，默认 20000 字
 * @returns { preview, truncated, total } preview 为裁剪后的文本
 */
export function buildTextPreview(
  text: string | null | undefined,
  maxChars = 20_000,
): { preview: string; truncated: boolean; total: number } {
  const total = (text || "").length;
  if (total <= maxChars) {
    return { preview: text || "", truncated: false, total };
  }
  return {
    preview: (text || "").slice(0, maxChars),
    truncated: true,
    total,
  };
}

/** 提取文本为空时的提示（区分「未提取到」与「该文件根本没有文本」） */
export function formatEmptyPreviewText(
  status: string | null | undefined,
): string {
  if (status === "failed") {
    return "该文件解析失败，没有可预览的提取文本。可点击「重新提取」重试。";
  }
  if (status === "pending") {
    return "该文件尚未提取文本。可点击「重新提取」触发解析。";
  }
  return "该文件没有提取到文本内容（可能为空文件、纯图片扫描件或 OCR 引擎未启用）。";
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
  /**
   * BUG-FIX 2026-10-09（跨模块数据链 / 要点提取字段丢失）：
   * 后端 `normalize_writing_targets`（workflows_analysis.py）除上面 5 个字段外
   * 还会写出 category / source_ref / source_page / confidence 四项结构化溯源字段，
   * 但 `parseScoringPoints` 的 mapPoint 只挑了 5 个字段。
   * 后果：要点提取页用**表格视图**确认时执行
   * `confirmScoring(projectId, JSON.stringify(scoringPoints))`，
   * 这四个字段被整条丢掉并**覆盖回数据库**（raw-JSON 视图则不会丢，
   * 于是同一份数据在两个视图下行为不一致）。
   * 缺失项声明为可选，保持对既有调用方与旧数据的向后兼容；
   * 未声明时按后端默认值补齐（category 默认「评分项」，confidence 默认 1.0）。
   */
  category?: string;
  source_ref?: string;
  source_page?: string | number | null;
  confidence?: number;
  /** 原始条目其余未声明字段（透传保留，避免未来新增字段再次被丢弃） */
  [extra: string]: unknown;
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
    if (typeof p !== "object") return { id: `R${i + 1}`, name: "", weight: "", standard: "", source: "" };
    // BUG-FIX 2026-10-09（跨模块数据链 / 要点提取字段丢失）：
    // 先以原条目为基底整体透传（`...p`），再覆盖已归一化的 5 个核心字段。
    // 这样后端 `normalize_writing_targets` 写出的 category / source_ref /
    // source_page / confidence 以及未来新增的任何字段，都能在
    // 「表格视图确认 → POST /ai/scoring/confirm → 覆盖回 DB」这条链路上存活，
    // 不再被前端静默丢弃。核心字段仍按既有别名规则归一化，行为不变。
    return {
      ...p,
      id: p.id || `R${i + 1}`,
      name: p.name || p.title || "",
      weight: p.weight || p.score || "",
      standard: p.standard || p.criteria || "",
      source: p.source || "",
      category: p.category ?? "评分项",
      source_ref: p.source_ref ?? "",
      source_page: p.source_page ?? null,
      confidence: typeof p.confidence === "number" ? p.confidence : 1.0,
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
// 评分达标状态 —— 与 `services/consistency_rules.SCORING_STATUS_META` 同源
// （BUG-FIX 2026-10-13 · P1）
//
// 问题：`ScoringComplianceTab.tsx` 自带一份写死的 `STATUS_META`
// （unmapped/empty/weak/covered → 文案+配色），与后端命名空间
// `consistency_rules.SCORING_STATUS_META` **各写一份、互不引用**。这正是
// 本文件上一节那段「规则命名空间」注释里逐条列举、并在 2026-10-08 修过一次
// 的漂移模式 —— 只不过严重度那一次修了、评分状态这一份漏了：
// `ConsistencyPage` 拉了 `GET /consistency/rules`，却只 apply 了
// `rules.severity.meta`，`rules.scoring_status.meta` **从未被任何代码消费**，
// 于是后端改文案 / 加状态，评分 Tab 一动不动。
//
// 现按严重度那套完全相同的机制补齐：静态兜底 + 运行时对齐。
// ============================================================

export const DEFAULT_CONSISTENCY_SCORING_STATUS = "unknown";

const BUILTIN_CONSISTENCY_SCORING_STATUS_META: Record<
  string,
  ConsistencySeverityMeta
> = {
  unmapped: { color: "red", label: "未挂载章节" },
  empty: { color: "orange", label: "正文过短" },
  weak: { color: "gold", label: "疑似未答到点" },
  covered: { color: "green", label: "已达标" },
  unknown: { color: "default", label: "未知" },
};

let CONSISTENCY_SCORING_STATUS_META: Record<string, ConsistencySeverityMeta> = {
  ...BUILTIN_CONSISTENCY_SCORING_STATUS_META,
};

/** 消费 `GET /consistency/rules` 的 `scoring_status.meta`；结构不合法时返回 false 且不改动。 */
export function applyConsistencyScoringStatusMeta(
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
  CONSISTENCY_SCORING_STATUS_META = Object.fromEntries(
    entries.map(([key, v]) => [
      key,
      {
        label: (v as ConsistencySeverityMeta).label,
        color: (v as ConsistencySeverityMeta).color,
      },
    ]),
  ) as Record<string, ConsistencySeverityMeta>;
  return true;
}

export function resetConsistencyScoringStatusMeta(): void {
  CONSISTENCY_SCORING_STATUS_META = {
    ...BUILTIN_CONSISTENCY_SCORING_STATUS_META,
  };
}

/**
 * 评分达标状态 → 展示文案/配色。
 * 未知状态回落到 `unknown` 中性标签（对应后端 `SCORING_STATUS_UNKNOWN_META`），
 * 而不是回落到某个真实状态——后者会把「后端新增了一档」伪装成「已达标」。
 */
export function mapConsistencyScoringStatus(
  status: string,
): ConsistencySeverityMeta {
  return (
    CONSISTENCY_SCORING_STATUS_META[status] ||
    CONSISTENCY_SCORING_STATUS_META[DEFAULT_CONSISTENCY_SCORING_STATUS] || {
      color: "default",
      label: "未知",
    }
  );
}

// ============================================================
// 废标条款应答状态 —— 与 `services/consistency_rules.REJECTION_STATUS_META` 同源
// （ENHANCE 2026-10-13 第六轮·统一命名空间）
//
// 问题：`RejectionCoverageTab.tsx` 自带本地 `statusLabel` / `statusColor` 两张
// 写死表（covered/weak/empty → 文案+配色），与后端零共享 —— 与严重度
// （2026-10-08 修）、评分达标状态（2026-10-13 修）是同一类漂移，这是漏网的
// 第三处。现按同一套机制补齐：静态兜底 + 运行时对齐。
// ============================================================

export const DEFAULT_CONSISTENCY_REJECTION_STATUS = "unknown";

const BUILTIN_CONSISTENCY_REJECTION_STATUS_META: Record<
  string,
  ConsistencySeverityMeta
> = {
  covered: { color: "green", label: "已覆盖" },
  weak: { color: "orange", label: "覆盖较弱" },
  empty: { color: "red", label: "未应答" },
  unknown: { color: "default", label: "未知" },
};

let CONSISTENCY_REJECTION_STATUS_META: Record<string, ConsistencySeverityMeta> = {
  ...BUILTIN_CONSISTENCY_REJECTION_STATUS_META,
};

/** 消费 `GET /consistency/rules` 的 `rejection_coverage.status_meta`；结构不合法时返回 false 且不改动。 */
export function applyConsistencyRejectionStatusMeta(
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
  CONSISTENCY_REJECTION_STATUS_META = Object.fromEntries(
    entries.map(([key, v]) => [
      key,
      {
        label: (v as ConsistencySeverityMeta).label,
        color: (v as ConsistencySeverityMeta).color,
      },
    ]),
  ) as Record<string, ConsistencySeverityMeta>;
  return true;
}

/** 恢复内置静态兜底（测试隔离用）。 */
export function resetConsistencyRejectionStatusMeta(): void {
  CONSISTENCY_REJECTION_STATUS_META = {
    ...BUILTIN_CONSISTENCY_REJECTION_STATUS_META,
  };
}

/**
 * 未知状态回落到 `unknown` 中性标签（对应后端 `REJECTION_STATUS_UNKNOWN_META`），
 * 而不是回落到某个真实状态——后者会把「后端新增了一档」伪装成「已覆盖」。
 */
export function mapConsistencyRejectionStatus(
  status: string,
): ConsistencySeverityMeta {
  return (
    CONSISTENCY_REJECTION_STATUS_META[status] ||
    CONSISTENCY_REJECTION_STATUS_META[DEFAULT_CONSISTENCY_REJECTION_STATUS] || {
      color: "default",
      label: "未知",
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
//
// 定位说明（2026-10-14）：本表是**前端兜底**，不再是「唯一事实源」。
// 后端 `services/ai/provider_catalog.py` 才是正本，并通过 `/ai/providers`
// 与 `/ai/provider-configs` 的 `provider_id` 下发稳定机器 id。调用方应优先
// 用 `config.provider_id` 调用 findProviderMeta；只有拿不到 id 时才退到
// provider_name —— 而中文展示名与本表键天然对不上（实测 11 个键仅 2 个命中），
// 这正是本次修复的缺陷。
// ============================================================

export interface ProviderMeta {
  label: string;
  base_url: string;
  model: string;
}

export const TEXT_PROVIDER_META: Record<string, ProviderMeta> = {
  // 注意：agnes 的默认模型与后端 config.AGNES_MODEL 保持一致（后端可被
  // AGNES_MODEL 环境变量覆盖，且 /ai/provider-configs 已直接下发该值，
  // 本条仅在接口不可用时兜底）。修复前这里是 agnes-2.5-flash，与后端
  // agnes-2.0-flash 漂移，会让「一键配置」连错模型。
  agnes: { label: "Agnes AI（新加坡）", base_url: "https://api.agnes-ai.cn/v1", model: "agnes-2.0-flash" },
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

/** 大小写/首尾空白不敏感查找厂商元数据（后端 id 与前端键大小写可能不一致） */
export function findProviderMeta(providerName: string | null | undefined): ProviderMeta | null {
  if (!providerName) return null;
  const target = providerName.trim().toLowerCase();
  const key = Object.keys(TEXT_PROVIDER_META).find(
    (k) => k.toLowerCase() === target,
  );
  return key ? TEXT_PROVIDER_META[key] : null;
}

/** 导出失败分类（从 handleExport 双层 catch 抽取的判定逻辑）：
 *
 * - cancelled：用户主动取消（不降级、不弹错）
 * - conflict：组锁 409 冲突（不降级，单独提示）
 * - background：SSE 已收到 stream_id（构建已在后端启动）后流中断——
 *   不降级同步导出（后端仍持组锁，重跑必然 409 或重复构建一份 DOCX），
 *   改提示「任务仍在后台执行，稍后在导出历史查看」（BUG-FIX 2026-10-27）
 * - fallback：其他 SSE 传输/构建失败（降级同步接口）
 */
export function classifyExportFailure(
  err: any,
  cancelRequested: boolean,
): "cancelled" | "conflict" | "background" | "fallback" {
  if (cancelRequested || err?.__cancelled) return "cancelled";
  if (err?.status === 409 || err?.response?.status === 409 || err?.__conflict) return "conflict";
  if (err?.__stream_started) return "background";
  return "fallback";
}

