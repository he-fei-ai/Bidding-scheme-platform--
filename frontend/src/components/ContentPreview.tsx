import { Table } from "antd";
import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import React from "react";

/** 生成唯一 SVG 元素 ID，避免同页面多图表 gradient ID 冲突 */
let _svgIdCounter = 0;
const uniqueSvgId = (prefix: string) => `${prefix}_${++_svgIdCounter}`;

/** 章节绑定的图表数据（键为图表类型，值为后端落库的 JSON/Mermaid 字符串） */
export type SectionChartMap = Partial<
  Record<
    "flowchart" | "gantt" | "architecture" | "labor" | "comparison" | "layout" | "timeline",
    string | null | undefined
  >
>;

interface ContentPreviewProps {
  content: string;
  /**
   * 章节绑定的真实图表数据（sections.*_json）。
   * 正文里的「配图位置提示」标记（`<<CHART:类型>>`）会就地取用这里的真实数据渲染；
   * 数据缺失时只显示中性占位提示，**绝不伪造图表内容**。
   */
  sectionCharts?: SectionChartMap | null;
}

const CHART_TYPE_LIST = [
  "flowchart",
  "gantt",
  "architecture",
  "labor",
  "comparison",
  "layout",
  "timeline",
] as const;

const CHART_TYPE_LABELS: Record<string, string> = {
  flowchart: "流程图",
  gantt: "甘特图",
  architecture: "架构图",
  labor: "劳动力图",
  comparison: "对比图",
  layout: "布局图",
  timeline: "里程碑时间线",
};

/**
 * 从描述/标题推断图表类型（与后端 content_cleaning.CHART_TYPE_KEYWORDS 同源口径）。
 * 仅用于「配图位置提示」的兜底识别，不做任何数据构造。
 */
function inferChartTypeFromText(text: string): string | null {
  const lowered = (text || "").toLowerCase();
  if (!lowered) return null;
  const keywordMap: Record<string, string[]> = {
    gantt: ["甘特", "进度", "工期", "计划", "阶段", "时点", "时序", "节奏", "里程碑"],
    flowchart: ["流程", "工艺", "步骤", "操作", "验收", "检验", "绘制", "施工"],
    architecture: ["架构", "组织", "体系", "职责", "矩阵", "角色", "岗位", "wbs"],
    labor: ["劳动力", "人员", "工种", "班组", "人数", "培训", "考核"],
    comparison: ["对比", "比选", "方案", "权重", "优缺点", "利弊", "占比"],
    layout: ["布置", "平面", "规划", "场地", "塔吊", "总平面", "分区"],
    timeline: ["时间线", "关键节点", "事件序列"],
  };
  let best: string | null = null;
  let bestScore = 0;
  Object.entries(keywordMap).forEach(([type, kws]) => {
    const score = kws.filter((kw) => lowered.includes(kw)).length;
    if (score > bestScore) {
      bestScore = score;
      best = type;
    }
  });
  return best;
}

// =============================================================
// 通用配色（与后端商业级 v2 渲染器保持一致）
// =============================================================
const PALETTE = [
  "#2B579A", "#E8A33D", "#70AD47", "#C00000", "#7030A0",
  "#4472C4", "#9966CC", "#A8D8EA", "#FFB6B9", "#FAE3D9",
  "#BBDED6", "#8B9DC3", "#DFB2E0", "#9DC3E6", "#F4A261",
];

const CHART_FONT = "'Microsoft YaHei', 'SimSun', sans-serif";

function parseTableMarkup(text: string): { title: string; headers: string[]; rows: string[][] } | null {
  const cleaned = text
    .replace(/^<<表格[:：]/, "")
    .replace(/>>$/, "")
    .trim();
  const lines = cleaned.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length < 2) return null;

  const title = lines[0].includes("|") ? "" : lines[0];
  const dataStart = title ? 1 : 0;
  if (dataStart >= lines.length) return null;

  const headers = lines[dataStart].split("|").map((h) => h.trim()).filter(Boolean);
  const rows = lines.slice(dataStart + 1).map((line) =>
    line.split("|").map((c) => c.trim()).filter(Boolean),
  ).filter((r) => r.length > 0);

  if (headers.length === 0) return null;
  return { title, headers, rows };
}

function TableBlock({ text }: { text: string }) {
  const parsed = parseTableMarkup(text);
  if (!parsed) return <pre style={{ fontSize: 13, whiteSpace: "pre-wrap", color: "#999" }}>{text}</pre>;

  const { title, headers, rows } = parsed;
  const columns = headers.map((h, i) => ({
    title: h,
    dataIndex: i,
    key: i,
    render: (v: string) => v || "",
  }));

  const dataSource = rows.map((row, i) => {
    const obj: Record<string, string> = { key: String(i) };
    headers.forEach((h, j) => {
      obj[j] = row[j] || "";
    });
    return obj;
  });

  return (
    <div style={{ margin: "12px 0" }}>
      {title && (
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8, color: "#333" }}>
          {title}
        </div>
      )}
      <Table
        size="small"
        bordered
        pagination={false}
        columns={columns}
        dataSource={dataSource}
        style={{ fontSize: 13 }}
      />
    </div>
  );
}

// =============================================================
// 1. 流程图（扁平化：直角矩形 + 淡紫色系 + 深灰箭头）
// =============================================================
function FlowchartBlock({ data }: { data: any }) {
  const nodes: string[] = data?.nodes || [];
  const title: string = data?.title || "流程图";

  if (nodes.length === 0) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#ffffff", border: "1px solid #D8D0F5", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 14, color: "#2D2D3A", textAlign: "center", borderBottom: "1px solid #EDE9FE", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 0 }}>
        {nodes.map((node: string, idx: number) => (
          <React.Fragment key={idx}>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                margin: "0 4px",
              }}
            >
              <div
                style={{
                  background: "#EDE9FE",
                  border: "1px solid #9B8BE0",
                  borderRadius: 0,
                  padding: "12px 18px",
                  minWidth: 110,
                  maxWidth: 180,
                  textAlign: "center",
                  fontSize: 14,
                  fontWeight: 400,
                  color: "#2D2D3A",
                  boxShadow: "none",
                }}
              >
                {node}
              </div>
              <div style={{ fontSize: 10, color: "#999", marginTop: 4 }}>
                步骤 {idx + 1}
              </div>
            </div>
            {idx < nodes.length - 1 && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  padding: "0 4px",
                  color: "#333333",
                  fontSize: 22,
                  fontWeight: "bold",
                  marginTop: -10,
                }}
              >
                →
              </div>
            )}
          </React.Fragment>
        ))}
      </div>
      <div style={{ marginTop: 10, fontSize: 11, color: "#999", textAlign: "center" }}>
        绿色 = 起始节点 / 蓝色 = 处理节点 / 红色 = 结束节点
      </div>
    </div>
  );
}

// =============================================================
// 2. 甘特图（商业级：横向横道图 + 时间轴 + 任务表）
// =============================================================
function GanttBlock({ data }: { data: any }) {
  const title: string = data?.title || "进度计划";
  const duration: number = data?.duration || data?.totalDays || 90;
  const unit: string = data?.unit || "天";
  const tasks: any[] = data?.tasks || [];

  if (tasks.length === 0) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  const columns = [
    { title: "#", dataIndex: "idx", key: "idx", width: 40 },
    { title: "工作内容", dataIndex: "name", key: "name", width: 160 },
    { title: "开始(天)", dataIndex: "start", key: "start", width: 70 },
    { title: "完成(天)", dataIndex: "end", key: "end", width: 70 },
    {
      title: "进度计划",
      dataIndex: "bar",
      key: "bar",
      render: (_: any, record: any) => {
        const pct = ((record.end - record.start + 1) / duration) * 100;
        const offset = (record.start / duration) * 100;
        const isMilestone = record.start === record.end;
        return (
          <div style={{ position: "relative", height: 26, width: "100%", minWidth: 240, background: "#fafafa", border: "1px solid #e8e8e8", borderRadius: 3 }}>
            {isMilestone ? (
              <div
                style={{
                  position: "absolute",
                  left: `${offset}%`,
                  top: 4,
                  width: 18,
                  height: 18,
                  background: "#FBBF24",
                  border: "2px solid #92400E",
                  transform: "translateX(-50%) rotate(45deg)",
                  borderRadius: 2,
                }}
              />
            ) : (
              <div
                style={{
                  position: "absolute",
                  left: `${offset}%`,
                  width: `${Math.max(pct, 2)}%`,
                  height: 18,
                  top: 3,
                  background: "linear-gradient(90deg, #1677ff, #4096ff)",
                  borderRadius: 3,
                  minWidth: 4,
                  boxShadow: "0 1px 3px rgba(22,119,255,0.3)",
                }}
              >
                {pct > 8 && (
                  <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 11, fontWeight: 600, textShadow: "0 1px 2px rgba(0,0,0,0.3)" }}>
                    {record.end - record.start + 1}{unit}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      },
    },
    { title: "备注", dataIndex: "remark", key: "remark", width: 100 },
  ];

  const dataSource = tasks.map((t: any, i: number) => ({
    key: i,
    idx: i + 1,
    name: t.name || t.task || `任务 ${i + 1}`,
    start: t.start ?? 0,
    end: t.end ?? t.start ?? 0,
    remark: t.remark || (t.start === t.end ? "◆里程碑" : ""),
  }));

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center" }}>
        {title}
      </div>
      <div style={{ marginBottom: 10, fontSize: 12, color: "#666", textAlign: "center" }}>
        总工期：<span style={{ color: "#1677ff", fontWeight: 700 }}>{duration} {unit}</span>
        &nbsp;·&nbsp;
        任务数：<span style={{ color: "#1677ff", fontWeight: 700 }}>{tasks.length}</span>
        &nbsp;·&nbsp;
        里程碑：<span style={{ color: "#FBBF24", fontWeight: 700 }}>◆</span>
      </div>
      <Table
        size="small"
        bordered
        pagination={false}
        columns={columns}
        dataSource={dataSource}
        style={{ fontSize: 13 }}
      />
    </div>
  );
}

// =============================================================
// 3. 架构图（商业级：树形可视化，渐变连线，根节点突出）
// =============================================================
function ArchitectureBlock({ data }: { data: any }) {
  // 兼容两种结构：根为 {label, children} 或 {root: {...}}
  const root = data?.root && typeof data.root === "object" ? data.root : data;
  const title: string = data?.title || root?.label || root?.name || "组织架构图";

  if (!root || (!root.label && !root.name)) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  // 递归展平节点并计算布局
  type Node = { id: string; label: string; depth: number; children: Node[] };
  const build = (n: any, depth: number, idx: { v: number }): Node => {
    const label = String(n?.label || n?.name || n?.title || "").trim();
    return {
      id: `n${idx.v++}`,
      label,
      depth,
      children: Array.isArray(n?.children)
        ? n.children.filter((c: any) => c && typeof c === "object").map((c: any) => build(c, depth + 1, idx))
        : [],
    };
  };
  const tree = build(root, 0, { v: 0 });

  // 计算每层节点数 + 位置
  const layers: Node[][] = [];
  const collect = (n: Node) => {
    if (!layers[n.depth]) layers[n.depth] = [];
    layers[n.depth].push(n);
    n.children.forEach(collect);
  };
  collect(tree);

  // 节点位置
  const nodeW = 200;
  const nodeH = 50;
  const hGap = 50;
  const vGap = 70;
  const padding = 30;
  const titleH = 50;

  const positions: Record<string, { x: number; y: number }> = {};
  let maxWidth = 0;
  layers.forEach((nodes, depth) => {
    const layerW = nodes.length * nodeW + (nodes.length - 1) * hGap;
    maxWidth = Math.max(maxWidth, layerW);
  });
  layers.forEach((nodes, depth) => {
    const layerW = nodes.length * nodeW + (nodes.length - 1) * hGap;
    const startX = (maxWidth - layerW) / 2;
    nodes.forEach((n, i) => {
      positions[n.id] = {
        x: padding + startX + i * (nodeW + hGap),
        y: padding + titleH + depth * (nodeH + vGap),
      };
    });
  });

  const totalH = padding * 2 + titleH + layers.length * (nodeH + vGap);
  const totalW = padding * 2 + maxWidth;

  // 配色
  const depthColors = [
    { fill: "#FBBF24", text: "#7C2D12", border: "#92400E" }, // 根
    { fill: "#2B579A", text: "#FFFFFF", border: "#1E3A5F" }, // 一级
    { fill: "#4472C4", text: "#FFFFFF", border: "#1E3A5F" }, // 二级
    { fill: "#5B9BD5", text: "#FFFFFF", border: "#1E3A5F" }, // 三级
    { fill: "#A8D8EA", text: "#1E3A5F", border: "#1E3A5F" }, // 四级
  ];

  // 计算连线
  const lines: { x1: number; y1: number; x2: number; y2: number }[] = [];
  const walk = (n: Node) => {
    n.children.forEach((c) => {
      const a = positions[n.id];
      const b = positions[c.id];
      if (a && b) {
        lines.push({
          x1: a.x + nodeW / 2,
          y1: a.y + nodeH,
          x2: b.x + nodeW / 2,
          y2: b.y,
        });
      }
      walk(c);
    });
  };
  walk(tree);

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ overflow: "auto" }}>
        <svg width={totalW} height={totalH} style={{ display: "block", margin: "0 auto", maxWidth: "100%" }}>
          {/* 连线 */}
          {lines.map((l, i) => {
            const midY = (l.y1 + l.y2) / 2;
            return (
              <g key={i}>
                <path
                  d={`M ${l.x1} ${l.y1} L ${l.x1} ${midY} L ${l.x2} ${midY} L ${l.x2} ${l.y2}`}
                  stroke="#64748B"
                  strokeWidth={1.5}
                  fill="none"
                />
                <polygon
                  points={`${l.x2 - 4},${l.y2 - 8} ${l.x2 + 4},${l.y2 - 8} ${l.x2},${l.y2 - 1}`}
                  fill="#64748B"
                />
              </g>
            );
          })}
          {/* 节点 */}
          {Object.entries(positions).map(([id, pos]) => {
            const n = (() => {
              const find = (node: Node): Node | null => {
                if (node.id === id) return node;
                for (const c of node.children) {
                  const f = find(c);
                  if (f) return f;
                }
                return null;
              };
              return find(tree);
            })();
            if (!n) return null;
            const colors = depthColors[Math.min(n.depth, depthColors.length - 1)];
            const isRoot = n.depth === 0;
            return (
              <g key={id} transform={`translate(${pos.x}, ${pos.y})`}>
                {isRoot ? (
                  <rect
                    x={-2}
                    y={-2}
                    width={nodeW + 4}
                    height={nodeH + 4}
                    rx={10}
                    fill={colors.fill}
                    stroke={colors.border}
                    strokeWidth={3}
                  />
                ) : (
                  <rect
                    x={0}
                    y={0}
                    width={nodeW}
                    height={nodeH}
                    rx={8}
                    fill={colors.fill}
                    stroke={colors.border}
                    strokeWidth={1.5}
                  />
                )}
                <text
                  x={nodeW / 2}
                  y={nodeH / 2 + 5}
                  textAnchor="middle"
                  fontSize={isRoot ? 14 : 13}
                  fontWeight={700}
                  fill={colors.text}
                  style={{ fontFamily: CHART_FONT }}
                >
                  {truncate(n.label, 16)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
        层级深度：{layers.length} &nbsp;·&nbsp; 节点总数：{Object.keys(positions).length}
      </div>
    </div>
  );
}

// =============================================================
// 4. 劳动力图（商业级四合一：工种柱状 + 阶段堆叠 + 时段曲线 + 图例）
// =============================================================
function LaborBlock({ data }: { data: any }) {
  const title: string = data?.title || "劳动力投入及工种分布图";
  const phases: string[] = data?.phases || data?.time || [];
  const categories: string[] = data?.categories || data?.types || [];
  const rows: any[] = data?.data || [];

  if (!phases.length || !categories.length || !rows.length) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  // 规范化数据
  const matrix: number[][] = rows.map((r) => {
    if (Array.isArray(r)) return r.map((v) => Number(v) || 0);
    if (Array.isArray(r?.values)) return r.values.map((v: any) => Number(v) || 0);
    return categories.map(() => 0);
  });

  // 补齐
  for (const row of matrix) {
    while (row.length < categories.length) row.push(0);
    if (row.length > categories.length) row.length = categories.length;
  }

  const catTotals = categories.map((_, c) => matrix.reduce((s, r) => s + (r[c] || 0), 0));
  const phaseTotals = matrix.map((r) => r.reduce((s, v) => s + v, 0));
  const maxCat = Math.max(...catTotals, 1);
  const maxPhase = Math.max(...phaseTotals, 1);

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 16 }}>
        {/* 左：工种柱状图 */}
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1E3A5F", marginBottom: 8 }}>① 各工种总投入（人·日）</div>
          {categories.map((cat, i) => {
            const val = catTotals[i];
            const pct = (val / maxCat) * 100;
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", marginBottom: 6, fontSize: 12 }}>
                <div style={{ width: 80, color: "#1E3A5F", fontWeight: 500 }}>{cat}</div>
                <div style={{ flex: 1, position: "relative", height: 18, background: "#f5f5f5", borderRadius: 3 }}>
                  <div
                    style={{
                      position: "absolute",
                      left: 0,
                      top: 0,
                      height: "100%",
                      width: `${pct}%`,
                      background: PALETTE[i % PALETTE.length],
                      borderRadius: 3,
                      minWidth: 2,
                    }}
                  />
                </div>
                <div style={{ width: 40, textAlign: "right", color: "#374151", fontWeight: 600 }}>{val}</div>
              </div>
            );
          })}
        </div>
        {/* 右：阶段堆叠 + 曲线 */}
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1E3A5F", marginBottom: 8 }}>② 各阶段工种分布（堆叠）</div>
          <StackedBars phases={phases} matrix={matrix} categories={categories} max={maxPhase} />
          <div style={{ fontSize: 13, fontWeight: 600, color: "#1E3A5F", marginTop: 14, marginBottom: 8 }}>③ 各阶段总人数变化</div>
          <LineChart phases={phases} values={phaseTotals} max={maxPhase} />
        </div>
      </div>
      {/* 图例 */}
      <div style={{ marginTop: 14, paddingTop: 10, borderTop: "1px dashed #e0e0e0", display: "flex", flexWrap: "wrap", gap: 12 }}>
        {categories.map((cat, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", fontSize: 12, color: "#374151" }}>
            <div style={{ width: 12, height: 12, background: PALETTE[i % PALETTE.length], borderRadius: 2, marginRight: 5 }} />
            {cat}
          </div>
        ))}
      </div>
    </div>
  );
}

function StackedBars({ phases, matrix, categories, max }: { phases: string[]; matrix: number[][]; categories: string[]; max: number }) {
  const W = 480;
  const H = 140;
  const padX = 30;
  const padY = 18;
  const chartW = W - padX * 2;
  const chartH = H - padY * 2;
  const colW = chartW / phases.length;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", background: "#fafafa", borderRadius: 4 }}>
      {/* Y 轴 */}
      {[0, 0.25, 0.5, 0.75, 1].map((p, i) => {
        const y = padY + chartH * (1 - p);
        return (
          <g key={i}>
            <line x1={padX} y1={y} x2={W - padX} y2={y} stroke="#E5E7EB" strokeWidth={0.5} />
            <text x={padX - 4} y={y + 3} textAnchor="end" fontSize={9} fill="#64748B">{Math.round(max * p)}</text>
          </g>
        );
      })}
      {phases.map((_, p) => {
        const cx = padX + p * colW + colW / 2;
        const barW = colW * 0.6;
        let cum = 0;
        // BUG-FIX 2026-10-08（P0 白屏）：matrix[p] 在载荷不完整时可能为 undefined
        // （phases 比 data 行数长），`matrix[p].map` 直接抛 TypeError。
        // App.tsx 的 ErrorBoundary 会把**整个 SPA**（含登录壳）替换成「页面出现问题」
        // 且 hasError 永不复位 → 一次畸形图表数据就让整个标签页报废、编辑内容全丢。
        const stack = matrix[p] || [];
        return (
          <g key={p}>
            {stack.map((v, c) => {
              const h = (v / max) * chartH;
              const y = padY + chartH - cum - h;
              cum += h;
              if (h <= 0) return null;
              return (
                <rect
                  key={c}
                  x={cx - barW / 2}
                  y={y}
                  width={barW}
                  height={h}
                  fill={PALETTE[c % PALETTE.length]}
                  stroke="#fff"
                  strokeWidth={1}
                />
              );
            })}
            <text
              x={cx}
              y={padY + chartH + 12}
              textAnchor="middle"
              fontSize={9}
              fill="#374151"
              style={{ fontFamily: CHART_FONT }}
            >
              {truncate(phases[p], 8)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function LineChart({ phases, values, max }: { phases: string[]; values: number[]; max: number }) {
  const W = 480;
  const H = 110;
  const padX = 30;
  const padY = 12;
  const chartW = W - padX * 2;
  const chartH = H - padY * 2;
  const points = values.map((v, i) => {
    const x = padX + (i / Math.max(phases.length - 1, 1)) * chartW;
    const y = padY + chartH - (v / max) * chartH;
    return { x, y, v };
  });
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  // BUG-FIX 2026-10-08（P0 白屏）：values 为空数组时 points 为空，
  // `points[points.length - 1].x` 抛 TypeError → 整个 SPA 被 ErrorBoundary 接管。
  const fillPath = points.length
    ? `${path} L ${points[points.length - 1].x} ${padY + chartH} L ${points[0].x} ${padY + chartH} Z`
    : "";
  const gradId = uniqueSvgId("laborFillGrad");
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", background: "#fafafa", borderRadius: 4 }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#1677ff" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#1677ff" stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {[0, 0.5, 1].map((p, i) => {
        const y = padY + chartH * (1 - p);
        return <line key={i} x1={padX} y1={y} x2={W - padX} y2={y} stroke="#E5E7EB" strokeWidth={0.5} />;
      })}
      <path d={fillPath} fill={`url(#${gradId})`} />
      <path d={path} stroke="#2B579A" strokeWidth={2} fill="none" />
      {points.map((p, i) => (
        <g key={i}>
          <circle cx={p.x} cy={p.y} r={3.5} fill="#1D4ED8" stroke="#fff" strokeWidth={1.5} />
          <text x={p.x} y={p.y - 7} textAnchor="middle" fontSize={9} fill="#1E3A5F" fontWeight={600}>{p.v}</text>
          <text x={p.x} y={padY + chartH + 10} textAnchor="middle" fontSize={9} fill="#374151" style={{ fontFamily: CHART_FONT }}>
            {truncate(phases[i] || "", 6)}
          </text>
        </g>
      ))}
    </svg>
  );
}

// =============================================================
// 5. 对比图（商业级：饼图 + 柱状图 组合）
// =============================================================
function ComparisonBlock({ data }: { data: any }) {
  const title: string = data?.title || "对比/占比图";
  const headers: string[] = data?.headers || ["对比项", "数值", "占比"];

  // 提取数据行
  let rows: { label: string; values: any[] }[] = [];
  if (Array.isArray(data?.rows)) {
    rows = data.rows.map((r: any) => {
      if (Array.isArray(r)) return { label: String(r[0] || ""), values: r };
      if (r && typeof r === "object") {
        const labelKey = headers[0] || "对比项";
        const label = String(r[labelKey] || r.label || r.name || "");
        // 后端契约：rows 为 {label, values:[...]}，values 与 headers.slice(1) 按位置对应
        if (Array.isArray(r.values)) {
          return { label, values: r.values.map((v: any) => (v === undefined || v === null ? 0 : v)) };
        }
        const values = headers.slice(1).map((h) => r[h]);
        return { label, values: values.length ? values : [r.value || 0] };
      }
      return { label: String(r), values: [] };
    });
  } else if (Array.isArray(data?.items)) {
    rows = (data.items as any[]).map((it) => ({
      label: String(it.label || it.name || ""),
      values: [Number(it.value) || 0],
    }));
  } else if (Array.isArray(data?.data)) {
    rows = (data.data as any[]).map((it) => ({
      label: String(it.label || it.name || ""),
      values: [Number(it.value) || 0],
    }));
  }

  if (rows.length === 0) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  // 如果是单数值列（占比图），使用饼图 + 柱状图组合
  if (rows.every((r) => r.values.length === 1)) {
    const items = rows.map((r) => ({ label: r.label, value: Number(r.values[0]) || 0 })).filter((it) => it.value > 0);
    if (items.length === 0) return <ChartJsonFallback data={data} title={title} />;
    return <PieBarChart title={title} items={items} />;
  }

  // 多列对比，使用表格 + 柱状图
  return <TableBarChart title={title} headers={headers} rows={rows} />;
}

function PieBarChart({ title, items }: { title: string; items: { label: string; value: number }[] }) {
  const total = items.reduce((s, it) => s + it.value, 0);
  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 16, alignItems: "center" }}>
        <PieChart items={items} total={total} />
        <div>
          {items.map((it, i) => {
            const pct = (it.value / total) * 100;
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", marginBottom: 8, fontSize: 12 }}>
                <div style={{ width: 14, height: 14, background: PALETTE[i % PALETTE.length], borderRadius: 2, marginRight: 8 }} />
                <div style={{ flex: 1, color: "#1E3A5F", fontWeight: 500 }}>{it.label}</div>
                <div style={{ width: 80, textAlign: "right", color: "#374151" }}>{it.value.toFixed(1)}</div>
                <div style={{ width: 50, textAlign: "right", color: PALETTE[i % PALETTE.length], fontWeight: 700 }}>{pct.toFixed(1)}%</div>
              </div>
            );
          })}
        </div>
      </div>
      <div style={{ marginTop: 10, fontSize: 11, color: "#999", textAlign: "right" }}>
        总计：<span style={{ color: "#1677ff", fontWeight: 700 }}>{total.toFixed(1)}</span>
      </div>
    </div>
  );
}

function PieChart({ items, total }: { items: { label: string; value: number }[]; total: number }) {
  const W = 220;
  const H = 220;
  const cx = W / 2;
  const cy = H / 2;
  const r = 90;
  let cumAngle = -Math.PI / 2; // 12 点方向开始
  const slices = items.map((it, i) => {
    const angle = (it.value / total) * Math.PI * 2;
    const startAngle = cumAngle;
    const endAngle = cumAngle + angle;
    cumAngle = endAngle;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const large = angle > Math.PI ? 1 : 0;
    const d = `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
    // 标签位置
    const midAngle = (startAngle + endAngle) / 2;
    const lx = cx + r * 0.65 * Math.cos(midAngle);
    const ly = cy + r * 0.65 * Math.sin(midAngle);
    const pct = (it.value / total) * 100;
    return { d, color: PALETTE[i % PALETTE.length], lx, ly, pct, label: it.label };
  });
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", margin: "0 auto" }}>
      {slices.map((s, i) => (
        <g key={i}>
          <path d={s.d} fill={s.color} stroke="#fff" strokeWidth={1.5} />
          {s.pct >= 5 && (
            <text x={s.lx} y={s.ly} textAnchor="middle" fontSize={11} fontWeight={700} fill="#fff" style={{ textShadow: "0 1px 2px rgba(0,0,0,0.4)" }}>
              {s.pct.toFixed(1)}%
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}

function TableBarChart({ title, headers, rows }: { title: string; headers: string[]; rows: { label: string; values: any[] }[] }) {
  // 多列对比
  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <Table
        size="small"
        bordered
        pagination={false}
        dataSource={rows.map((r, i) => ({ key: i, ...Object.fromEntries(headers.map((h, j) => [h, j === 0 ? r.label : String(r.values[j - 1] ?? "")])) }))}
        columns={headers.map((h) => ({ title: h, dataIndex: h, key: h }))}
        style={{ fontSize: 13 }}
      />
    </div>
  );
}

// =============================================================
// 6. 布局图（商业级：区域网格 + 分类配色 + 连接箭头）
// =============================================================
function LayoutBlock({ data }: { data: any }) {
  const title: string = data?.title || "施工现场总平面布置图";
  const zones: any[] = data?.zones || data?.areas || [];

  if (!zones.length) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  // 规范化
  const normZones = zones.map((z, i) => {
    if (typeof z === "string") return { name: z, category: "default", desc: "" };
    return {
      name: String(z?.name || z?.label || `区域 ${i + 1}`),
      category: String(z?.category || z?.type || "default"),
      desc: String(z?.description || z?.desc || ""),
    };
  });

  const categoryColors: Record<string, { fill: string; border: string }> = {
    "生产区": { fill: "#EAF2F8", border: "#2B579A" },
    "生活区": { fill: "#FFF7E6", border: "#D97706" },
    "办公区": { fill: "#EAFBEA", border: "#16A34A" },
    "材料堆放区": { fill: "#FEF3C7", border: "#CA8A04" },
    "加工区": { fill: "#F3E8FF", border: "#7C3AED" },
    "危险品区": { fill: "#FEE2E2", border: "#DC2626" },
    "临时设施": { fill: "#DBEAFE", border: "#2563EB" },
    "运输通道": { fill: "#F1F5F9", border: "#475569" },
    "default": { fill: "#F1F5F9", border: "#1E3A5F" },
  };

  // 网格布局
  const n = normZones.length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(n * 1.5)));
  const rows = Math.ceil(n / cols);
  const zoneW = 200;
  const zoneH = 90;
  const hGap = 24;
  const vGap = 32;
  const padding = 30;
  const titleH = 44;
  const legendH = 50;

  const totalW = padding * 2 + cols * zoneW + (cols - 1) * hGap;
  const totalH = padding * 2 + titleH + rows * zoneH + (rows - 1) * vGap + legendH;

  // 收集分类
  const allCats: string[] = [];
  normZones.forEach((z) => {
    if (!allCats.includes(z.category)) allCats.push(z.category);
  });

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ overflow: "auto" }}>
        <svg width={totalW} height={totalH} style={{ display: "block", margin: "0 auto", maxWidth: "100%" }}>
          {normZones.map((z, i) => {
            const r = Math.floor(i / cols);
            const c = i % cols;
            const x = padding + c * (zoneW + hGap);
            const y = padding + titleH + r * (zoneH + vGap);
            const colors = categoryColors[z.category] || categoryColors["default"];
            return (
              <g key={i}>
                <rect
                  x={x}
                  y={y}
                  width={zoneW}
                  height={zoneH}
                  rx={10}
                  fill={colors.fill}
                  stroke={colors.border}
                  strokeWidth={2}
                />
                <text
                  x={x + zoneW / 2}
                  y={y + 24}
                  textAnchor="middle"
                  fontSize={14}
                  fontWeight={700}
                  fill={colors.border}
                  style={{ fontFamily: CHART_FONT }}
                >
                  {truncate(z.name, 12)}
                </text>
                <text
                  x={x + zoneW / 2}
                  y={y + 44}
                  textAnchor="middle"
                  fontSize={10}
                  fill={colors.border}
                  opacity={0.8}
                >
                  {z.category}
                </text>
                {z.desc && (
                  <text
                    x={x + zoneW / 2}
                    y={y + 62}
                    textAnchor="middle"
                    fontSize={10}
                    fill="#374151"
                    style={{ fontFamily: CHART_FONT }}
                  >
                    {truncate(z.desc, 18)}
                  </text>
                )}
                <text
                  x={x + 8}
                  y={y + 14}
                  fontSize={9}
                  fill={colors.border}
                  opacity={0.6}
                  fontWeight={600}
                >
                  #{i + 1}
                </text>
              </g>
            );
          })}
          {/* 图例 */}
          {allCats.map((cat, i) => {
            const colors = categoryColors[cat] || categoryColors["default"];
            const lx = padding + (i % 4) * 130;
            const ly = totalH - legendH + 16 + Math.floor(i / 4) * 18;
            return (
              <g key={cat}>
                <rect x={lx} y={ly} width={14} height={12} fill={colors.fill} stroke={colors.border} strokeWidth={1.5} />
                <text x={lx + 20} y={ly + 10} fontSize={10} fill="#1E3A5F" style={{ fontFamily: CHART_FONT }}>{cat}</text>
              </g>
            );
          })}
        </svg>
      </div>
      <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
        区域数：{normZones.length} &nbsp;·&nbsp; 分类数：{allCats.length}
      </div>
    </div>
  );
}

// =============================================================
// 7. 时间轴/里程碑图（商业级：横向时间轴 + 上下交错 + 菱形节点 + 阶段分组）
// =============================================================
function TimelineBlock({ data }: { data: any }) {
  const title: string = data?.title || "项目关键里程碑时间线";
  const milestones: any[] = data?.milestones || data?.events || data?.points || [];

  if (!milestones.length) {
    return <ChartJsonFallback data={data} title={title} />;
  }

  // 规范化
  const items = milestones.map((m, i) => {
    if (typeof m === "string") return { name: m, date: "", desc: "", group: "" };
    return {
      name: String(m?.name || m?.title || m?.label || `里程碑 ${i + 1}`),
      date: String(m?.date || m?.time || m?.day || ""),
      desc: String(m?.description || m?.desc || ""),
      group: String(m?.group || m?.phase || ""),
    };
  });

  const n = items.length;
  const padding = 30;
  const titleH = 50;
  const nodeW = 160;
  const axisH = 100;
  const minInnerW = 720;
  const innerW = Math.max(minInnerW, n * nodeW + (n - 1) * 20);
  const totalW = padding * 2 + innerW;
  const totalH = titleH + axisH + 60;

  // 阶段配色
  const groupColors: { border: string; fill: string }[] = [
    { border: "#2B579A", fill: "#EAF2F8" },
    { border: "#16A34A", fill: "#EAFBEA" },
    { border: "#D97706", fill: "#FFF7E6" },
    { border: "#7C3AED", fill: "#F3E8FF" },
    { border: "#DC2626", fill: "#FEE2E2" },
  ];
  const allGroups: string[] = [];
  items.forEach((it) => {
    if (it.group && !allGroups.includes(it.group)) allGroups.push(it.group);
  });

  const innerLeft = padding;
  const innerRight = totalW - padding;
  const step = n > 1 ? (innerRight - innerLeft) / (n - 1) : 0;
  const axisY = titleH + 30;
  const tlGradId = uniqueSvgId("timelineGrad");

  return (
    <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
        {title}
      </div>
      <div style={{ overflow: "auto" }}>
        <svg width={totalW} height={totalH} style={{ display: "block", margin: "0 auto", maxWidth: "100%" }}>
          {/* 主轴 */}
          <defs>
            <linearGradient id={tlGradId} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="#94A3B8" />
              <stop offset="100%" stopColor="#475569" />
            </linearGradient>
          </defs>
          <line
            x1={innerLeft}
            y1={axisY}
            x2={innerRight - 12}
            y2={axisY}
            stroke={`url(#${tlGradId})`}
            strokeWidth={4}
          />
          <polygon
            points={`${innerRight},${axisY} ${innerRight - 16},${axisY - 8} ${innerRight - 16},${axisY + 8}`}
            fill="#475569"
          />
          {/* 里程碑 */}
          {items.map((m, i) => {
            const cx = n === 1 ? (innerLeft + innerRight) / 2 : innerLeft + i * step;
            const isUp = i % 2 === 0;
            const boxY = isUp ? axisY - 60 : axisY + 12;
            const boxH = 50;
            const groupIdx = m.group && allGroups.includes(m.group) ? allGroups.indexOf(m.group) : i;
            const colors = groupColors[groupIdx % groupColors.length];
            return (
              <g key={i}>
                {/* 连接线 */}
                <line
                  x1={cx}
                  y1={isUp ? boxY + boxH : axisY}
                  x2={cx}
                  y2={isUp ? axisY - 2 : boxY}
                  stroke={colors.border}
                  strokeWidth={1.5}
                />
                {/* 节点卡片 */}
                <rect
                  x={cx - nodeW / 2}
                  y={boxY}
                  width={nodeW}
                  height={boxH}
                  rx={6}
                  fill={colors.fill}
                  stroke={colors.border}
                  strokeWidth={1.5}
                />
                <text
                  x={cx}
                  y={boxY + 16}
                  textAnchor="middle"
                  fontSize={12}
                  fontWeight={700}
                  fill={colors.border}
                  style={{ fontFamily: CHART_FONT }}
                >
                  {truncate(m.name, 14)}
                </text>
                {m.date && (
                  <text x={cx} y={boxY + 30} textAnchor="middle" fontSize={10} fill="#1E3A5F" fontWeight={600}>
                    {m.date}
                  </text>
                )}
                {m.desc && (
                  <text
                    x={cx}
                    y={boxY + 43}
                    textAnchor="middle"
                    fontSize={9}
                    fill="#374151"
                    style={{ fontFamily: CHART_FONT }}
                  >
                    {truncate(m.desc, 18)}
                  </text>
                )}
                {/* 菱形节点 */}
                <polygon
                  points={`${cx},${axisY - 8} ${cx + 8},${axisY} ${cx},${axisY + 8} ${cx - 8},${axisY}`}
                  fill="#FBBF24"
                  stroke="#92400E"
                  strokeWidth={2}
                />
                <text x={cx} y={axisY + 4} textAnchor="middle" fontSize={10} fontWeight={700} fill="#1E3A5F">
                  {i + 1}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      {/* 阶段图例 */}
      {allGroups.length > 0 && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed #e0e0e0", display: "flex", flexWrap: "wrap", gap: 14, justifyContent: "center" }}>
          {allGroups.map((g, i) => {
            const colors = groupColors[i % groupColors.length];
            return (
              <div key={g} style={{ display: "flex", alignItems: "center", fontSize: 12, color: "#374151" }}>
                <div style={{ width: 12, height: 12, background: colors.fill, border: `1.5px solid ${colors.border}`, borderRadius: 2, marginRight: 5 }} />
                {g}
              </div>
            );
          })}
        </div>
      )}
      <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
        里程碑：{n} 个 &nbsp;·&nbsp; 阶段：{allGroups.length || 0} 个
      </div>
    </div>
  );
}

// =============================================================
// 通用降级展示（当数据结构不匹配时显示 JSON）
// =============================================================
function ChartJsonFallback({ data, title }: { data: any; title?: string }) {
  return (
    <div style={{ margin: "12px 0", padding: 12, background: "#fafafa", border: "1px solid #f0f0f0", borderRadius: 6 }}>
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6, color: "#1a3a8a" }}>
        {title || data?.title || "图表"}
      </div>
      <div style={{ fontSize: 11, color: "#999", marginBottom: 6 }}>图表数据（详见导出 DOCX）</div>
      <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", color: "#666", margin: 0, maxHeight: 200, overflow: "auto", background: "#fff", padding: 8, borderRadius: 4, border: "1px solid #e8e8e8" }}>
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}

function truncate(text: string, maxLen: number): string {
  if (!text) return "";
  return text.length > maxLen ? text.slice(0, maxLen) + "…" : text;
}

// =============================================================
// 解析 CHART 块（兼容多种格式）
// =============================================================
function parseChartPayload(raw: string): {
  type: string;
  data: any;
  /** true 表示这是「配图位置提示」（标记内无数据），数据需从章节绑定字段取 */
  hint?: boolean;
  /** 提示标记里给出的标题（可能为空） */
  title?: string;
} {
  let text = raw.trim();
  // 去除包裹符 <<>> 或 [[]]
  if (text.startsWith("<<") && text.endsWith(">>")) {
    text = text.slice(2, -2).trim();
  } else if (text.startsWith("[[") && text.endsWith("]]")) {
    text = text.slice(2, -2).trim();
  }

  const isConcreteType = (t: string) => (CHART_TYPE_LIST as readonly string[]).includes(t);

  // 提取前缀类型
  const colonIdx = text.indexOf(":");
  if (colonIdx > 0) {
    const prefix = text.slice(0, colonIdx).trim().toLowerCase();
    if (prefix === "chart" || isConcreteType(prefix)) {
      const rest = text.slice(colonIdx + 1).trim();
      // 处理双层包裹格式：<<CHART:flowchart:{...}>> → 内层前缀 "flowchart" 优先
      let innerPrefix = prefix;
      let parseRest = rest;
      const innerColon = rest.indexOf(":");
      if (innerColon > 0) {
        const innerP = rest.slice(0, innerColon).trim().toLowerCase();
        if (isConcreteType(innerP)) {
          innerPrefix = innerP;
          parseRest = rest.slice(innerColon + 1).trim();
        }
      }

      // ---- 配图位置提示：无数据、仅标注类型 ----
      // `<<CHART:GANTT>>` / `<<CHART:{gantt}>>` / `<<GANTT:>>` / `<<GANTT:进度计划>>`
      const braceStripped = parseRest.replace(/^\{+/, "").replace(/\}+$/, "").trim();
      const token = braceStripped.toLowerCase();
      if (isConcreteType(token)) {
        return { type: token, data: null, hint: true };
      }
      if (!parseRest) {
        return isConcreteType(innerPrefix)
          ? { type: innerPrefix, data: null, hint: true }
          : { type: "", data: null, hint: true };
      }

      // 尝试解析 JSON（真实内联数据）
      try {
        const data = JSON.parse(parseRest);
        const innerType = (data?.type || innerPrefix).toString().toLowerCase();
        return { type: innerType, data };
      } catch {
        // 非 JSON：纯文本标题 → 关键词推断类型（与后端同源口径），仍属「位置提示」
        const inferred = isConcreteType(innerPrefix) ? innerPrefix : inferChartTypeFromText(braceStripped);
        if (inferred) {
          return { type: inferred, data: null, hint: true, title: braceStripped };
        }
        return { type: isConcreteType(innerPrefix) ? innerPrefix : "", data: null, hint: true, title: braceStripped };
      }
    }
  }

  // 整段尝试 JSON
  try {
    const data = JSON.parse(text);
    const innerType = (data?.type || "flowchart").toString().toLowerCase();
    return { type: innerType, data };
  } catch {
    return { type: "flowchart", data: { nodes: text.split(/[，,、\n]/).map((s) => s.trim()).filter(Boolean) } };
  }
}

// =============================================================
// 配图位置提示：取用章节绑定的真实图表数据
// =============================================================
let _mermaidInitialized = false;

async function renderMermaidInto(container: HTMLDivElement, code: string): Promise<string | null> {
  try {
    const mod: any = await import("mermaid");
    const mermaid = mod.default || mod;
    if (!_mermaidInitialized) {
      mermaid.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "strict",
        fontFamily: "'Microsoft YaHei', sans-serif",
        themeVariables: {
          primaryColor: "#EDE9FE",
          primaryTextColor: "#2D2D3A",
          primaryBorderColor: "#9B8BE0",
          lineColor: "#333333",
          secondaryColor: "#EDE9FE",
          tertiaryColor: "#F5F5F5",
          fontSize: "14px",
        },
      });
      _mermaidInitialized = true;
    }
    const { svg } = await mermaid.render(uniqueSvgId("mermaid-inline"), code);
    container.innerHTML = svg;
    return null;
  } catch (e: any) {
    return e?.message || String(e);
  }
}

/** Mermaid 内联渲染（懒加载 mermaid.js，与图表 Tab 使用同一渲染引擎） */
function MermaidInlineBlock({ code, title }: { code: string; title?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const renderIdRef = useRef(0);

  useEffect(() => {
    const myId = ++renderIdRef.current;
    const container = containerRef.current;
    if (!container || !code) return;
    container.innerHTML = "";
    setError(null);
    renderMermaidInto(container, code).then((err) => {
      if (myId === renderIdRef.current) setError(err);
    });
    return () => {
      renderIdRef.current += 1;
      if (container) container.innerHTML = "";
    };
  }, [code]);

  if (error) return <ChartPendingNotice title={title || "图表"} message="图表渲染失败，请重新生成或查看导出文档" />;

  return (
    <div style={{ margin: "12px 0", textAlign: "center" }}>
      {title ? <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8, color: "#1a3a8a" }}>{title}</div> : null}
      <div ref={containerRef} style={{ overflowX: "auto" }} />
    </div>
  );
}

/** 中性提示：不伪造图表数据（无降级原则） */
function ChartPendingNotice({
  title,
  message,
}: {
  title: string;
  message?: string;
}) {
  return (
    <div
      style={{
        margin: "12px 0",
        padding: "10px 14px",
        background: "#fafafa",
        border: "1px dashed #d9d9d9",
        borderRadius: 6,
        fontSize: 12,
        color: "#8c8c8c",
        textAlign: "center",
      }}
    >
      {title ? `${title}：` : ""}
      {message || "图表位置已标注，图表数据生成后自动显示"}
    </div>
  );
}

/** 章节绑定图表数据 → 规范化的可渲染形态 */
function resolveSectionChart(
  sectionCharts: SectionChartMap | null | undefined,
  type: string,
): { type: string; data: any; mermaid: string | null } | null {
  if (!sectionCharts) return null;
  const keys = type && type !== "chart" ? [type] : CHART_TYPE_LIST;
  for (const key of keys as readonly string[]) {
    const raw = (sectionCharts as Record<string, string | null | undefined>)[key];
    if (!raw) continue;
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object") {
        const code = typeof obj.code === "string" ? obj.code.trim() : "";
        if (code) return { type: (obj.type || key).toString().toLowerCase(), data: obj, mermaid: code };
        return { type: (obj.type || key).toString().toLowerCase(), data: obj, mermaid: null };
      }
    } catch {
      // 非 JSON：视为纯 Mermaid 代码
      return { type: key, data: null, mermaid: raw.trim() };
    }
  }
  return null;
}

function ChartBlock({ text, sectionCharts }: { text: string; sectionCharts?: SectionChartMap | null }) {
  const parsed = parseChartPayload(text);
  let { type, data } = parsed;

  if (parsed.hint) {
    const chart = resolveSectionChart(sectionCharts, type);
    const label = CHART_TYPE_LABELS[type] || "配图";
    if (!chart) return <ChartPendingNotice title={parsed.title || label} />;
    if (chart.mermaid) {
      return (
        <MermaidInlineBlock
          code={chart.mermaid}
          title={(chart.data && chart.data.title) || parsed.title || label}
        />
      );
    }
    data = chart.data;
    type = chart.type || type;
  }

  switch (type) {
    case "gantt":
      return <GanttBlock data={data} />;
    case "flowchart":
      return <FlowchartBlock data={data} />;
    case "architecture":
      return <ArchitectureBlock data={data} />;
    case "labor":
      return <LaborBlock data={data} />;
    case "comparison":
      return <ComparisonBlock data={data} />;
    case "layout":
      return <LayoutBlock data={data} />;
    case "timeline":
      return <TimelineBlock data={data} />;
    default:
      return <ChartJsonFallback data={data} title={data?.title || type} />;
  }
}

type PreviewBlock = { type: "table" | "chart" | "text" | "mermaid"; value: string };

/** 图表围栏起始行：与后端 content_cleaning.CHART_FENCE_OPEN_PATTERN 同口径
 *  （支持 ``` / ~~~ 两种围栏与 chart-json / chart_json / chartjson 三种标签）。 */
const CHART_FENCE_OPEN = /^(?:`{3,}|~{3,})[ \t]*(mermaid|chart[-_]?json)[ \t]*(?:\{[^}]*\})?[ \t]*$/i;
const FENCE_CLOSE = /^(?:`{3,}|~{3,})[ \t]*$/;
const CHART_FENCE_HARD_LINE_LIMIT = 5000;
/** 标记聚合上限：与后端 CHART_MARKER 40 行 / 表格 60 行对齐 */
const MARKER_AGGREGATE_LIMIT = 60;

function parseContent(content: string): PreviewBlock[] {
  const blocks: PreviewBlock[] = [];
  const lines = content.split("\n");
  let i = 0;

  /** 把一行按 text 块追加（相邻 text 合并） */
  const pushText = (line: string) => {
    const last = blocks[blocks.length - 1];
    if (last && last.type === "text") last.value += "\n" + line;
    else blocks.push({ type: "text", value: line });
  };

  while (i < lines.length) {
    const line = lines[i];

    // ---- BUG-FIX 2026-10-08（P0 围栏原文暴露）----
    // 正文生成提示词明确要求 AI 用 ```mermaid / ```chart-json 围栏输出图表
    // （backend/app/services/ai/prompts/content.py），而本函数此前**只认**
    // <<表格…>> / <<CHART:…>> 标记，没有任何围栏分支 →
    // 围栏整块落入 text 分支被 pre-wrap 原样显示，用户在章节编写页看到的是
    // "```mermaid / graph TD / A-->B" 这种源码，而不是图表。
    const fenceOpen = CHART_FENCE_OPEN.exec(line.trim());
    if (fenceOpen) {
      const lang = fenceOpen[1].toLowerCase();
      const body: string[] = [];
      let j = i + 1;
      let closed = false;
      while (j < lines.length) {
        if (FENCE_CLOSE.test(lines[j].trim())) {
          closed = true;
          break;
        }
        // 遇到下一个围栏起始行 → 视为本块未闭合
        if (/^(?:`{3,}|~{3,})/.test(lines[j].trim())) break;
        body.push(lines[j]);
        j += 1;
        if (j - i > CHART_FENCE_HARD_LINE_LIMIT) break;
      }
      const payload = body.join("\n").trim();
      if (closed && payload) {
        if (/^mermaid$/.test(lang)) {
          blocks.push({ type: "mermaid", value: payload });
        } else {
          try {
            const obj = JSON.parse(payload);
            // 归一成后端/parseChartPayload 认识的标记形态，交给既有 ChartBlock 通道
            blocks.push({ type: "chart", value: `<<CHART:${JSON.stringify(obj)}>>` });
          } catch {
            // 载荷非法：绝不把 JSON 源码当正文显示（与后端"坏块删除"口径一致）
            i = j + 1;
            continue;
          }
        }
        i = j + 1;
        continue;
      }
      // 未闭合 / 空载荷：按后端口径删除坏块，绝不把源码写进预览
      i = j + 1;
      continue;
    }

    if (/^<<表格[:：]/.test(line)) {
      // BUG-FIX 2026-10-08（P1 未闭合标记吞后文）：原实现无上限地聚合到
      // 文末（只判断 endsWith(">>")），AI 输出被 token 截断时一个缺失的 ">>"
      // 会把**后面所有正文**吞进表格块 → 预览里下方内容整片消失且无任何报错。
      // 现在三重退出条件：空行、新标记起始、聚合上限。
      const tableLines: string[] = [line];
      i++;
      while (i < lines.length && tableLines.length <= MARKER_AGGREGATE_LIMIT) {
        const tline = lines[i].trim();
        if (!tline) break; // 空行 = 块边界
        if (/^<<|^\[\[/.test(tline)) break; // 新的标记起始
        tableLines.push(tline);
        i++;
        if (tline.endsWith(">>")) break;
      }
      blocks.push({ type: "table", value: tableLines.join("\n") });
      continue;
    }

    // 检测所有图表类型（含 chart/flowchart/gantt/architecture/labor/comparison/layout/timeline）
    if (/^<<(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:/i.test(line) ||
        /^\[\[(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:/i.test(line)) {
      // 单行图表
      if ((line.trim().endsWith(">>") || line.trim().endsWith("]]")) && !line.includes("\n")) {
        blocks.push({ type: "chart", value: line });
        i++;
        continue;
      }
      // 多行图表（JSON 中可能包含换行）
      // BUG-FIX 2026-10-08（P1）：同表格 —— 加上空行/新标记/行数上限三重退出条件，
      // 避免一个未闭合标记把整章正文吞进 chart 块。
      const chartLines: string[] = [line];
      i++;
      const isDoubleBracket = line.trim().startsWith("[[");
      const endMarker = isDoubleBracket ? "]]" : ">>";
      while (i < lines.length && chartLines.length <= MARKER_AGGREGATE_LIMIT) {
        const nline = lines[i].trim();
        if (!nline) break;
        if (/^<<|^\[\[/.test(nline)) break;
        chartLines.push(lines[i]);
        if (nline.endsWith(endMarker)) {
          i++;
          break;
        }
        i++;
      }
      blocks.push({ type: "chart", value: chartLines.join("\n") });
      continue;
    }

    pushText(line);
    i++;
  }

  return blocks;
}

export default React.memo(function ContentPreview({ content, sectionCharts }: ContentPreviewProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [showScrollTopBtn, setShowScrollTopBtn] = useState(false);

  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const target = e.target as HTMLDivElement;
    setShowScrollTopBtn(target.scrollTop > 200);
  }, []);

  const scrollToTop = useCallback(() => {
    scrollContainerRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = scrollContainerRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, []);

  // BUG 修复：正文生成期间 SSE 进度事件触发父组件频繁 re-render，
  // 原 ContentPreview 每次渲染都全量调用 parseContent(content)（解析所有图表/表格/标题），
  // 其中包含大量 SVG 字符串重建和 layout 计算，是生成期间 UI 卡顿主因。
  // 用 React.memo + useMemo 缓存 blocks，content 相同时跳过整颗子树渲染。
  const blocks = useMemo(() => parseContent(content), [content]);

  if (!content) {
    return <div style={{ color: "#999", padding: 20, textAlign: "center" }}>暂无内容</div>;
  }

  return (
    <div style={{ position: "relative", flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div
        ref={scrollContainerRef}
        className="content-preview-scroll"
        onScroll={handleScroll}
        style={{
          flex: 1,
          fontSize: 14,
          lineHeight: 1.8,
          fontFamily: "'Microsoft YaHei', 'SimSun', serif",
          overflow: "auto",
          overflowX: "auto",
          overflowY: "auto",
          padding: "20px 24px",
          background: "#fff",
          borderRadius: 6,
          border: "1px solid #d9d9d9",
          minHeight: 300,
          maxHeight: "100%",
          scrollBehavior: "smooth",
          boxShadow: "inset 0 1px 3px rgba(0,0,0,0.04)",
        }}
      >
        {blocks.map((block, idx) => {
          if (block.type === "table") {
            return <TableBlock key={idx} text={block.value} />;
          }
          if (block.type === "chart") {
            return <ChartBlock key={idx} text={block.value} sectionCharts={sectionCharts} />;
          }
          if (block.type === "mermaid") {
            // BUG-FIX 2026-10-08（P0）：正文围栏里的 mermaid 源码直接出图，
            // 不再以 pre-wrap 文本显示给用户。
            return <MermaidInlineBlock key={idx} code={block.value} title="" />;
          }
          return (
            <p key={idx} style={{ margin: "6px 0", whiteSpace: "pre-wrap", textAlign: "justify" }}>
              {block.value}
            </p>
          );
        })}
      </div>

      <div
        style={{
          position: "absolute",
          right: 16,
          bottom: 16,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          opacity: showScrollTopBtn ? 1 : 0,
          transition: "opacity 0.3s ease",
          pointerEvents: showScrollTopBtn ? "auto" : "none",
        }}
      >
        <button
          onClick={scrollToTop}
          title="返回顶部"
          style={{
            width: 36,
            height: 36,
            borderRadius: "50%",
            border: "1px solid #d9d9d9",
            background: "#fff",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            color: "#666",
            boxShadow: "0 2px 8px rgba(0,0,0,0.12)",
            transition: "all 0.2s ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "#1677ff";
            e.currentTarget.style.color = "#fff";
            e.currentTarget.style.borderColor = "#1677ff";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "#fff";
            e.currentTarget.style.color = "#666";
            e.currentTarget.style.borderColor = "#d9d9d9";
          }}
        >
          ↑
        </button>
        <button
          onClick={scrollToBottom}
          title="滚动到底部"
          style={{
            width: 36,
            height: 36,
            borderRadius: "50%",
            border: "1px solid #d9d9d9",
            background: "#fff",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            color: "#666",
            boxShadow: "0 2px 8px rgba(0,0,0,0.12)",
            transition: "all 0.2s ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "#1677ff";
            e.currentTarget.style.color = "#fff";
            e.currentTarget.style.borderColor = "#1677ff";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "#fff";
            e.currentTarget.style.color = "#666";
            e.currentTarget.style.borderColor = "#d9d9d9";
          }}
        >
          ↓
        </button>
      </div>
    </div>
  );
});
