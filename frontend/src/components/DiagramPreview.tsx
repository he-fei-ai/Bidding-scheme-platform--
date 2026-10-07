import { useEffect, useRef, useState, useCallback } from "react";
import { Alert, Button, Space, Typography, Tag, Spin } from "antd";
import {
  ReloadOutlined,
  BarChartOutlined,
  LineChartOutlined,
  PieChartOutlined,
  WarningOutlined,
  FileTextOutlined,
} from "@ant-design/icons";

const { Text, Paragraph } = Typography;

interface DiagramPreviewProps {
  code: string;
  type: "flowchart" | "gantt" | "architecture" | "labor" | "comparison" | "layout" | "timeline";
  onRefresh?: () => void;
  /** 插入到正文回调：返回插入内容字符串 */
  onInsert?: (svgOrHtml: string) => void;
  /** 是否显示"插入"按钮 */
  showInsertButton?: boolean;
}

let mermaidInitialized = false;

// ---------------------------------------------------------------
// Mermaid 错误解析工具
// ---------------------------------------------------------------

/** 解析 Mermaid 渲染错误，提取语法错误位置和详细信息 */
function parseMermaidError(errorMessage: string): {
  summary: string;
  lineNumber: number | null;
  columnNumber: number | null;
  detail: string;
} {
  const result = {
    summary: "渲染失败",
    lineNumber: null as number | null,
    columnNumber: null as number | null,
    detail: errorMessage,
  };

  if (!errorMessage) return result;

  // 匹配 "Parse error on line X" 模式
  const lineMatch = errorMessage.match(/on line\s+(\d+)/i);
  if (lineMatch) {
    result.lineNumber = parseInt(lineMatch[1], 10);
  }

  // 匹配 "at column X" 模式
  const colMatch = errorMessage.match(/at column\s+(\d+)/i);
  if (colMatch) {
    result.columnNumber = parseInt(colMatch[1], 10);
  }

  // 匹配 "Line X: ..." 模式
  const lineColonMatch = errorMessage.match(/Line\s+(\d+)\s*:\s*(.+)/i);
  if (lineColonMatch) {
    result.lineNumber = parseInt(lineColonMatch[1], 10);
    result.detail = lineColonMatch[2].trim();
  }

  // 提取括号内的语法错误摘要
  const bracketMatch = errorMessage.match(/:\s*(.+?)(?:\s*\(|$)/);
  if (bracketMatch && bracketMatch[1].length < 100) {
    result.summary = bracketMatch[1].trim();
  }

  // 截断过长的错误信息
  if (result.detail.length > 300) {
    result.detail = result.detail.slice(0, 300) + "...";
  }

  return result;
}

// ---------------------------------------------------------------
// JSON 转 Mermaid 代码
// ---------------------------------------------------------------

/** 将 JSON 数据转换为 Mermaid 代码 */
function jsonToMermaid(code: string): string | null {
  // Mermaid 字符串字面量转义：处理引号、反斜杠和换行符
  const esc = (s: string): string =>
    s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\r/g, "\\r");

  try {
    const data = JSON.parse(code.trim());
    const chartType = data.type;
    const title = data.title || "";
    const nodes = data.nodes || [];

    if (chartType === "flowchart") {
      const lines = ["flowchart TD"];
      if (nodes.length > 0) {
        for (let i = 0; i < nodes.length; i++) {
          const nid = typeof nodes[i] === "string" ? nodes[i] : nodes[i].label || `节点${i + 1}`;
          lines.push(`    N${i}["${esc(nid)}"]`);
          if (i > 0) {
            lines.push(`    N${i - 1} --> N${i}`);
          }
        }
      }
      return lines.join("\n");
    }

    if (chartType === "gantt") {
      const tasks = data.tasks || [];
      if (Array.isArray(tasks) && tasks.length > 0) {
        const lines = ["gantt", `    title ${esc(title || "进度计划")}`, "    dateFormat  YYYY-MM-DD", "    axisFormat  %m月%d日"];
        const baseDate = new Date(2025, 0, 1);
        for (let i = 0; i < tasks.length; i++) {
          const task = tasks[i];
          let taskName: string;
          let start: number;
          let end: number;
          if (typeof task === "object" && task !== null) {
            taskName = task.name || task.label || `任务${i + 1}`;
            const remark = task.remark || "";
            if (remark) taskName = `${taskName}（${remark}）`;
            start = task.start ?? (i + 1);
            end = task.end ?? (start + 5);
          } else {
            taskName = String(task);
            start = i * 5 + 1;
            end = start + 30;
          }
          const duration = Math.max(1, end - start + 1);
          const startDate = new Date(baseDate);
          startDate.setDate(startDate.getDate() + start - 1);
          const dateStr = startDate.toISOString().split("T")[0];
          lines.push(`    ${esc(taskName)} :a${i + 1}, ${dateStr}, ${duration}d`);
        }
        return lines.join("\n");
      }
    }

    // 对比图：后端契约支持饼图格式（chart:"pie" + data:[{label,value}]）→ Mermaid pie
    if (chartType === "comparison" && Array.isArray(data.data) && data.data.length > 0) {
      const items = data.data
        .map((it: any) => ({
          label: String(it?.label || it?.name || ""),
          value: Number(it?.value) || 0,
        }))
        .filter((it: any) => it.label && it.value > 0);
      if (items.length >= 2) {
        const lines = ["pie showData", `    title ${esc(title || "占比图")}`];
        items.forEach((it: any) => {
          lines.push(`    "${esc(it.label)}" : ${it.value}`);
        });
        return lines.join("\n");
      }
    }

    // 对比图：后端契约为 headers + rows[{label, values:[...]}]（多方案对比表）
    if (chartType === "comparison" && Array.isArray(data.rows) && data.rows.length > 0) {
      const headers = data.headers || ["项目", "数值"];
      const lines = ["flowchart LR"];
      data.rows.forEach((row: any, i: number) => {
        const label = String(row?.[headers[0]] || row?.label || row?.name || `项${i + 1}`);
        const values = Array.isArray(row?.values)
          ? row.values
          : headers.slice(1).map((h: string) => row?.[h]);
        const valueStr = values
          .map((v: any, j: number) => `${String(headers[j + 1] || `值${j + 1}`).slice(0, 6)}:${v ?? 0}`)
          .join(" / ");
        lines.push(`    Node${i}["${esc(label)}\\n${esc(valueStr)}"]`);
        if (i > 0) lines.push(`    Node${i - 1} --- Node${i}`);
      });
      return lines.join("\n");
    }

    if (chartType === "labor" && data.phases && data.categories) {
      const lines = ["gantt", `    title ${esc(title || "劳动力投入计划")}`, "    dateFormat  X"];
      data.phases.forEach((phase: string, p: number) => {
        const rowData = Array.isArray(data.data) ? data.data[p] : [];
        const values = Array.isArray(rowData) ? rowData : (rowData?.values || []);
        const total = values.reduce((sum: number, v: number) => sum + (v || 0), 0);
        lines.push(`    ${esc(phase)} :a${p + 1}, ${p + 1}, ${total > 0 ? total : 1}d`);
      });
      return lines.join("\n");
    }

    // 架构图：后端契约支持 root 包裹格式（{type,title,root:{label,children}}）与顶层树格式
    if (
      chartType === "architecture" &&
      ((data.root && typeof data.root === "object") || data.children || data.label)
    ) {
      const lines = ["flowchart TD"];
      let nodeId = 0;
      function traverse(node: any, parentId?: string) {
        const label = node.label || node.name || node.title || "未命名";
        const currentId = `N${nodeId++}`;
        lines.push(`    ${currentId}["${esc(label)}"]`);
        if (parentId) lines.push(`    ${parentId} --> ${currentId}`);
        if (node.children && Array.isArray(node.children)) {
          node.children.forEach((child: any) => traverse(child, currentId));
        }
      }
      traverse(data.root && typeof data.root === "object" ? data.root : data);
      return lines.join("\n");
    }

    // 布局图：后端契约为 zones[]（id/name/x/y/w/h/color/description）+ connections[]（from/to/label）
    if (chartType === "layout" && Array.isArray(data.zones) && data.zones.length > 0) {
      const lines = ["flowchart LR"];
      const idMap: Record<string, string> = {};
      data.zones.forEach((zone: any, i: number) => {
        const rawId = String(zone?.id || `Z${i + 1}`);
        const safeId = rawId.replace(/[^A-Za-z0-9_]/g, "") || `Z${i + 1}`;
        idMap[rawId] = safeId;
        const label = String(zone?.name || zone?.label || `区域${i + 1}`);
        lines.push(`    ${safeId}["${esc(label)}"]`);
      });
      if (Array.isArray(data.connections)) {
        data.connections.forEach((conn: any) => {
          const rawFrom = String(conn?.from || "");
          const rawTo = String(conn?.to || "");
          const from = idMap[rawFrom] || rawFrom.replace(/[^A-Za-z0-9_]/g, "");
          const to = idMap[rawTo] || rawTo.replace(/[^A-Za-z0-9_]/g, "");
          if (from && to) {
            const edgeLabel = conn?.label ? `-->|${esc(String(conn.label))}|` : "-->";
            lines.push(`    ${from} ${edgeLabel} ${to}`);
          }
        });
      }
      return lines.join("\n");
    }

    // 布局图（旧数据兼容）：nodes + edges 格式
    if ((chartType === "layout") && data.nodes) {
      const lines = ["flowchart LR"];
      data.nodes.forEach((node: any, i: number) => {
        const label = typeof node === "string" ? node : (node.label || node.name || `节点${i + 1}`);
        lines.push(`    N${i}["${esc(String(label))}"]`);
      });
      if (data.edges && Array.isArray(data.edges)) {
        data.edges.forEach((edge: any, i: number) => {
          const from = edge.from ?? edge.source ?? i;
          const to = edge.to ?? edge.target ?? (i + 1);
          const label = edge.label ? `-->|${esc(edge.label)}|` : "-->";
          lines.push(`    N${from} ${label} N${to}`);
        });
      }
      return lines.join("\n");
    }

    // 时间线：后端契约为 milestones[]（date/title/description/status）；使用 Mermaid v11 原生 timeline 图
    if (
      chartType === "timeline" &&
      (Array.isArray(data.milestones) ? data.milestones.length > 0 : Boolean(data.events))
    ) {
      const milestones = Array.isArray(data.milestones) ? data.milestones : data.events;
      const lines = ["timeline", `    title ${esc(title || "项目关键里程碑时间线")}`];
      milestones.forEach((m: any, i: number) => {
        const name = String(m?.title || m?.name || m?.label || `里程碑${i + 1}`);
        const date = String(m?.date || m?.time || `阶段${i + 1}`);
        // 冒号是 timeline 事件分隔符，需替换为全角冒号避免语法冲突
        const safeName = name.replace(/:/g, "：");
        const safeDate = date.replace(/:/g, "：");
        lines.push(`    ${safeDate} : ${safeName}`);
      });
      return lines.join("\n");
    }
  } catch {
    // 解析失败，返回 null
  }
  return null;
}

/** 从代码中提取图表数据（JSON 或 Mermaid 转换） */
function extractChartData(code: string, type: string): any {
  const trimmed = (code || "").trim();
  // 直接 JSON 解析
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return JSON.parse(trimmed);
    } catch { /* fall through */ }
  }
  // Mermaid 代码 → 尝试提取结构化数据
  if (type === "flowchart" && trimmed.startsWith("flowchart")) {
    const nodes: string[] = [];
    const idPattern = /\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\["([^"]+)"\]/g;
    let m;
    while ((m = idPattern.exec(trimmed)) !== null) {
      nodes.push(m[2]);
    }
    return { type: "flowchart", title: "流程图", nodes };
  }
  if (type === "gantt" && trimmed.startsWith("gantt")) {
    const tasks: any[] = [];
    const taskPattern = /([^\s:]+)\s+:.+,?\s*([^,\n]+)/g;
    let t;
    while ((t = taskPattern.exec(trimmed)) !== null) {
      tasks.push({ name: t[1].trim(), duration: t[2].trim() });
    }
    return { type: "gantt", title: "进度计划", tasks, duration: 90 };
  }
  return null;
}

/** 降级时使用原生 React/SVG 图表组件（不做文本表格降级） */
function renderNativeChartFallback(type: string, code: string) {
  const data = extractChartData(code, type);
  if (!data) return null;

  const chartW = 560;
  const chartH = 320;
  const pad = 40;

  switch (type) {
    case "flowchart": {
      const nodes = data?.nodes || [];
      if (!nodes.length) return null;
      const n = nodes.length;
      const nodeW = 130, nodeH = 40, gap = 50;
      const totalW = Math.max(chartW, n * (nodeW + gap));
      const totalH = n * (nodeH + gap);
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "流程图"}
          </div>
          <svg width="100%" viewBox={`0 0 ${totalW} ${totalH}`} style={{ display: "block", margin: "0 auto" }}>
            {nodes.map((node: string, i: number) => {
              const y = pad + i * (nodeH + gap);
              const x = (totalW - nodeW) / 2;
              const isStart = i === 0;
              const isEnd = i === n - 1;
              return (
                <g key={i}>
                  {i > 0 && (
                    <line x1={totalW / 2} y1={y - gap} x2={totalW / 2} y2={y} stroke="#1677ff" strokeWidth={2} markerEnd="url(#arr)" />
                  )}
                  <rect x={x} y={y} width={nodeW} height={nodeH} rx={8}
                    fill={isStart ? "#52c41a" : isEnd ? "#f5222d" : "#e8f0fe"}
                    stroke={isStart ? "#389e0d" : isEnd ? "#cf1322" : "#1677ff"} strokeWidth={2} />
                  <text x={totalW / 2} y={y + nodeH / 2 + 5} textAnchor="middle" fontSize={13} fontWeight={600}
                    fill={isStart || isEnd ? "#fff" : "#1a3a8a"} style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>
                    {(node as string).length > 12 ? (node as string).slice(0, 12) + "…" : node}
                  </text>
                </g>
              );
            })}
            <defs>
              <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                <path d="M0,0 L0,6 L8,3 z" fill="#1677ff" />
              </marker>
            </defs>
          </svg>
          <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
            {n} 个步骤 &nbsp;·&nbsp; 绿色=开始 / 蓝色=过程 / 红色=结束
          </div>
        </div>
      );
    }

    case "gantt": {
      const tasks = data?.tasks || [];
      if (!tasks.length) return null;
      const duration = data.duration || 90;
      const barH = 22, barGap = 6;
      const totalH = pad * 2 + tasks.length * (barH + barGap);
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "进度计划"}
          </div>
          <svg width="100%" viewBox={`0 0 ${chartW} ${totalH}`} style={{ display: "block", margin: "0 auto" }}>
            {tasks.map((t: any, i: number) => {
              const y = pad + i * (barH + barGap);
              return (
                <g key={i}>
                  <text x={8} y={y + 15} fontSize={11} fill="#374151" style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>
                    {String(t.name || t.task || `任务${i + 1}`).slice(0, 14)}
                  </text>
                  <rect x={pad} y={y} width={chartW - pad * 2} height={barH} fill="#f5f5f5" rx={3} />
                  <rect x={pad + 4} y={y + 2} width={Math.max(8, (chartW - pad * 2 - 8) * 0.6)} height={barH - 4}
                    fill="url(#ganttGrad)" rx={2} />
                </g>
              );
            })}
            <defs>
              <linearGradient id="ganttGrad" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stopColor="#1677ff" />
                <stop offset="100%" stopColor="#4096ff" />
              </linearGradient>
            </defs>
          </svg>
          <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
            总工期：{duration} 天 &nbsp;·&nbsp; {tasks.length} 个任务
          </div>
        </div>
      );
    }

    case "architecture": {
      const root = data?.root || data;
      if (!root || (!root.label && !root.name)) return null;
      type Node = { id: string; label: string; depth: number; children: Node[] };
      const build = (n: any, depth: number, idx: { v: number }): Node => ({
        id: `n${idx.v++}`, label: String(n?.label || n?.name || ""), depth,
        children: Array.isArray(n?.children) ? n.children.map((c: any) => build(c, depth + 1, idx)) : [],
      });
      const tree = build(root, 0, { v: 0 });
      const layers: Node[][] = [];
      const collect = (n: Node) => { if (!layers[n.depth]) layers[n.depth] = []; layers[n.depth].push(n); n.children.forEach(collect); };
      collect(tree);
      const nodeW = 160, nodeH = 42, hGap = 40, vGap = 60;
      let maxWidth = 0;
      layers.forEach((ns) => { const w = ns.length * nodeW + (ns.length - 1) * hGap; maxWidth = Math.max(maxWidth, w); });
      const positions: Record<string, { x: number; y: number }> = {};
      layers.forEach((ns, d) => {
        const layerW = ns.length * nodeW + (ns.length - 1) * hGap;
        const sx = (maxWidth - layerW) / 2;
        ns.forEach((n, i) => { positions[n.id] = { x: pad + sx + i * (nodeW + hGap), y: pad + d * (nodeH + vGap) }; });
      });
      const lines: { x1: number; y1: number; x2: number; y2: number }[] = [];
      const walk = (n: Node) => { n.children.forEach((c) => {
        const a = positions[n.id], b = positions[c.id];
        if (a && b) lines.push({ x1: a.x + nodeW / 2, y1: a.y + nodeH, x2: b.x + nodeW / 2, y2: b.y });
        walk(c);
      }); };
      walk(tree);
      const depthColors = [
        { fill: "#FBBF24", text: "#7C2D12", border: "#92400E" },
        { fill: "#2B579A", text: "#FFFFFF", border: "#1E3A5F" },
        { fill: "#4472C4", text: "#FFFFFF", border: "#1E3A5F" },
        { fill: "#5B9BD5", text: "#FFFFFF", border: "#1E3A5F" },
      ];
      const totalH2 = pad * 2 + layers.length * (nodeH + vGap);
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || root.label || "组织架构图"}
          </div>
          <svg width="100%" viewBox={`0 0 ${pad * 2 + maxWidth} ${totalH2}`} style={{ display: "block", margin: "0 auto" }}>
            {lines.map((l, i) => {
              const midY = (l.y1 + l.y2) / 2;
              return <path key={i} d={`M${l.x1},${l.y1}L${l.x1},${midY}L${l.x2},${midY}L${l.x2},${l.y2}`} stroke="#64748B" strokeWidth={1.5} fill="none" />;
            })}
            {Object.entries(positions).map(([id, pos]) => {
              const find = (node: Node): Node | null => {
                if (node.id === id) return node;
                for (const c of node.children) { const f = find(c); if (f) return f; }
                return null;
              };
              const n = find(tree); if (!n) return null;
              const colors = depthColors[Math.min(n.depth, depthColors.length - 1)];
              const isRoot = n.depth === 0;
              return (
                <g key={id} transform={`translate(${pos.x}, ${pos.y})`}>
                  <rect x={isRoot ? -2 : 0} y={isRoot ? -2 : 0}
                    width={isRoot ? nodeW + 4 : nodeW} height={isRoot ? nodeH + 4 : nodeH}
                    rx={isRoot ? 10 : 8} fill={colors.fill} stroke={colors.border} strokeWidth={isRoot ? 3 : 1.5} />
                  <text x={nodeW / 2} y={nodeH / 2 + 5} textAnchor="middle" fontSize={isRoot ? 13 : 12}
                    fontWeight={700} fill={colors.text} style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>
                    {(n.label || "").length > 14 ? (n.label || "").slice(0, 14) + "…" : n.label}
                  </text>
                </g>
              );
            })}
          </svg>
          <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>
            层级深度：{layers.length} &nbsp;·&nbsp; 节点总数：{Object.keys(positions).length}
          </div>
        </div>
      );
    }

    case "labor": {
      const phases = data?.phases || data?.time || [];
      const categories = data?.categories || data?.types || [];
      const rows: any[] = data?.data || [];
      if (!phases.length || !categories.length || !rows.length) return null;
      const matrix = rows.map((r: any) => {
        if (Array.isArray(r)) return r.map((v: any) => Number(v) || 0);
        if (Array.isArray(r?.values)) return r.values.map((v: any) => Number(v) || 0);
        return categories.map(() => 0);
      });
      const W = 560, H = 200, padX = 50, padY = 20;
      const chartW = W - padX * 2, chartH = H - padY * 2;
      const colW = chartW / Math.max(phases.length, 1);
      const maxVal = Math.max(...matrix.flatMap(r => r), 1);
      const PALETTE = ["#2B579A", "#E8A33D", "#70AD47", "#C00000", "#7030A0", "#4472C4", "#9966CC"];
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "劳动力投入及工种分布图"}
          </div>
          <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
            {phases.map((_: any, p: number) => {
              const cx = padX + p * colW + colW / 2;
              const barW = colW * 0.6;
              let cum = 0;
              return (
                <g key={p}>
                  {matrix[p].map((v: number, c: number) => {
                    const h = (v / maxVal) * chartH;
                    const y = padY + chartH - cum - h;
                    cum += h;
                    if (h <= 0) return null;
                    return <rect key={c} x={cx - barW / 2} y={y} width={barW} height={h} fill={PALETTE[c % PALETTE.length]} stroke="#fff" strokeWidth={1} />;
                  })}
                  <text x={cx} y={H - 4} textAnchor="middle" fontSize={10} fill="#374151" style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>{String(phases[p]).slice(0, 6)}</text>
                </g>
              );
            })}
          </svg>
          <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 10, justifyContent: "center" }}>
            {categories.map((cat: string, i: number) => (
              <div key={i} style={{ display: "flex", alignItems: "center", fontSize: 11, color: "#374151" }}>
                <div style={{ width: 10, height: 10, background: PALETTE[i % PALETTE.length], borderRadius: 2, marginRight: 4 }} />{cat}
              </div>
            ))}
          </div>
        </div>
      );
    }

    case "comparison": {
      let items: { label: string; value: number }[];
      // 饼图格式：{chart:"pie", data:[{label,value}]}
      if (Array.isArray(data?.data) && data.data.length > 0) {
        items = (data.data as any[])
          .map((it: any) => ({ label: String(it?.label || it?.name || ""), value: Number(it?.value) || 0 }))
          .filter((it: any) => it.label && it.value > 0);
      } else {
        // 表格格式：{headers, rows:[{label, values:[...]}]}
        const rows2 = data?.rows || data?.items || [];
        if (!rows2.length) return null;
        const headers = data?.headers || ["项目", "数值"];
        const multiCol = rows2.some((r: any) => Array.isArray(r?.values) && r.values.length > 1);
        // 多列对比表（多方案对比）不适合单值饼图降级，交由 Mermaid 主路径渲染
        if (multiCol) return null;
        items = rows2.map((r: any) => ({
          label: String(r[headers[0]] || r.label || r.name || r[0] || ""),
          value: Array.isArray(r?.values)
            ? (Number(r.values[0]) ?? 0)
            : Number(r[headers[1]] ?? r.value ?? r[1] ?? 0),
        })).filter((it: any) => it.value > 0);
      }
      if (!items.length) return null;
      const total = items.reduce((s: number, it: any) => s + it.value, 0);
      const W = 560, H = 260, padX2 = 60, padY2 = 20;
      const chartW2 = W - padX2 * 2, chartH2 = H - padY2 * 2 - 30;
      const maxVal = Math.max(...items.map((it: any) => it.value), 1);
      const barW = chartW2 / items.length * 0.6;
      const PALETTE2 = ["#2B579A", "#E8A33D", "#70AD47", "#C00000", "#7030A0"];
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "对比/占比图"}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 16, alignItems: "center" }}>
            <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", margin: "0 auto" }}>
              {(() => {
                let cumAngle = -Math.PI / 2;
                return items.map((it: any, i: number) => {
                  const angle = (it.value / total) * Math.PI * 2;
                  const startAngle = cumAngle;
                  const endAngle = cumAngle + angle;
                  cumAngle = endAngle;
                  const cx = W / 2, cy = H / 2 - 10, r = 90;
                  const x1 = cx + r * Math.cos(startAngle), y1 = cy + r * Math.sin(startAngle);
                  const x2 = cx + r * Math.cos(endAngle), y2 = cy + r * Math.sin(endAngle);
                  const large = angle > Math.PI ? 1 : 0;
                  const pct = (it.value / total) * 100;
                  return (
                    <path key={i} d={`M${cx},${cy}L${x1},${y1}A${r},${r}0${large},1${x2},${y2}Z`}
                      fill={PALETTE2[i % PALETTE2.length]} stroke="#fff" strokeWidth={1.5} />
                  );
                });
              })()}
            </svg>
            <div>
              {items.map((it: any, i: number) => (
                <div key={i} style={{ display: "flex", alignItems: "center", marginBottom: 6, fontSize: 12 }}>
                  <div style={{ width: 12, height: 12, background: PALETTE2[i % PALETTE2.length], borderRadius: 2, marginRight: 8 }} />
                  <div style={{ flex: 1, color: "#1E3A5F", fontWeight: 500 }}>{it.label}</div>
                  <div style={{ width: 50, textAlign: "right", color: "#374151" }}>{it.value.toFixed(1)}</div>
                  <div style={{ width: 40, textAlign: "right", color: PALETTE2[i % PALETTE2.length], fontWeight: 700 }}>{((it.value / total) * 100).toFixed(1)}%</div>
                </div>
              ))}
              <div style={{ fontSize: 11, color: "#999", textAlign: "right", marginTop: 4 }}>总计：{total.toFixed(1)}</div>
            </div>
          </div>
        </div>
      );
    }

    case "layout": {
      const zones = data?.zones || data?.areas || [];
      if (!zones.length) return null;
      const n = zones.length;
      const cols = Math.max(1, Math.ceil(Math.sqrt(n * 1.5)));
      const zoneW = 140, zoneH = 70, hGap = 16, vGap = 20;
      const padding2 = 20;
      const totalW3 = padding2 * 2 + cols * zoneW + (cols - 1) * hGap;
      const totalH3 = padding2 * 2 + Math.ceil(n / cols) * (zoneH + vGap);
      const catColors: Record<string, { fill: string; border: string }> = {
        "生产区": { fill: "#EAF2F8", border: "#2B579A" }, "生活区": { fill: "#FFF7E6", border: "#D97706" },
        "办公区": { fill: "#EAFBEA", border: "#16A34A" }, "材料堆放区": { fill: "#FEF3C7", border: "#CA8A04" },
        "加工区": { fill: "#F3E8FF", border: "#7C3AED" }, "default": { fill: "#F1F5F9", border: "#1E3A5F" },
      };
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "施工现场总平面布置图"}
          </div>
          <svg width="100%" viewBox={`0 0 ${totalW3} ${totalH3}`} style={{ display: "block", margin: "0 auto" }}>
            {zones.map((z: any, i: number) => {
              const r = Math.floor(i / cols), c = i % cols;
              const x = padding2 + c * (zoneW + hGap), y = padding2 + r * (zoneH + vGap);
              const colors = catColors[z.category || z.type || "default"] || catColors["default"];
              return (
                <g key={i}>
                  <rect x={x} y={y} width={zoneW} height={zoneH} rx={6} fill={colors.fill} stroke={colors.border} strokeWidth={1.5} />
                  <text x={x + zoneW / 2} y={y + 20} textAnchor="middle" fontSize={12} fontWeight={700} fill={colors.border} style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>
                    {String(z.name || z.label || `区域${i + 1}`).slice(0, 10)}
                  </text>
                  <text x={x + zoneW / 2} y={y + 38} textAnchor="middle" fontSize={10} fill={colors.border} opacity={0.8}>{z.category || z.type || "区域"}</text>
                </g>
              );
            })}
          </svg>
          <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>区域数：{n}</div>
        </div>
      );
    }

    case "timeline": {
      const milestones = data?.milestones || data?.events || data?.points || [];
      if (!milestones.length) return null;
      const nodeW = 140, axisY = 60, pad3 = 30;
      const n2 = milestones.length;
      const innerW = Math.max(600, n2 * nodeW + (n2 - 1) * 16);
      const totalW4 = pad3 * 2 + innerW;
      const totalH4 = 160;
      const groupColors = [
        { border: "#2B579A", fill: "#EAF2F8" }, { border: "#16A34A", fill: "#EAFBEA" },
        { border: "#D97706", fill: "#FFF7E6" }, { border: "#7C3AED", fill: "#F3E8FF" },
      ];
      const step = n2 > 1 ? (innerW - 20) / (n2 - 1) : 0;
      return (
        <div style={{ margin: "12px 0", padding: 16, background: "#fff", border: "1px solid #d6e4ff", borderRadius: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12, color: "#1a3a8a", textAlign: "center", borderBottom: "1px solid #e6f0ff", paddingBottom: 10 }}>
            {data.title || "项目关键里程碑时间线"}
          </div>
          <svg width="100%" viewBox={`0 0 ${totalW4} ${totalH4}`} style={{ display: "block", margin: "0 auto" }}>
            <line x1={pad3} y1={axisY} x2={totalW4 - pad3 - 10} y2={axisY} stroke="#475569" strokeWidth={3} />
            <polygon points={`${totalW4 - pad3},${axisY} ${totalW4 - pad3 - 12},${axisY - 6} ${totalW4 - pad3 - 12},${axisY + 6}`} fill="#475569" />
            {milestones.map((m: any, i: number) => {
              const cx = n2 === 1 ? (pad3 + innerW / 2) : pad3 + i * step;
              const isUp = i % 2 === 0;
              const boxY = isUp ? axisY - 52 : axisY + 8;
              const colors = groupColors[i % groupColors.length];
              return (
                <g key={i}>
                  <line x1={cx} y1={isUp ? boxY + 46 : axisY} x2={cx} y2={isUp ? axisY - 1 : boxY} stroke={colors.border} strokeWidth={1.5} />
                  <rect x={cx - nodeW / 2} y={boxY} width={nodeW} height={46} rx={6} fill={colors.fill} stroke={colors.border} strokeWidth={1.5} />
                  <text x={cx} y={boxY + 16} textAnchor="middle" fontSize={11} fontWeight={700} fill={colors.border} style={{ fontFamily: "'Microsoft YaHei', sans-serif" }}>
                    {String(m?.name || m?.title || `里程碑${i + 1}`).slice(0, 12)}
                  </text>
                  <text x={cx} y={boxY + 32} textAnchor="middle" fontSize={9} fill="#374151">{m?.date || ""}</text>
                </g>
              );
            })}
          </svg>
          <div style={{ marginTop: 8, fontSize: 11, color: "#999", textAlign: "center" }}>{n2} 个里程碑</div>
        </div>
      );
    }

    default:
      return null;
  }
}

async function doRender(
  container: HTMLDivElement | null,
  code: string,
  type: string,
  renderIdRef: { current: number },
  id: number,
): Promise<{ loading: false; error: string | null }> {
  try {
    const mermaidModule = await import("mermaid");
    const mermaid = mermaidModule.default || mermaidModule;
    if (!container) {
      return { loading: false, error: "图表容器尚未就绪，请点击重试" };
    }
    if (!mermaidInitialized) {
      mermaid.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "strict",
        fontFamily: "'Microsoft YaHei', sans-serif",
        themeVariables: {
          primaryColor: "#1677ff",
          primaryTextColor: "#333",
          primaryBorderColor: "#1677ff",
          lineColor: "#666",
          secondaryColor: "#e6f4ff",
          tertiaryColor: "#f5f5f5",
        },
      });
      mermaidInitialized = true;
    }

    if (id !== renderIdRef.current) return { loading: false, error: null };

    const renderId = `mermaid-${type}-${Date.now()}`;
    container.innerHTML = "";
    const { svg } = await mermaid.render(renderId, code);
    if (id !== renderIdRef.current) return { loading: false, error: null };
    container.innerHTML = svg;
    return { loading: false, error: null };
  } catch (e: any) {
    if (id !== renderIdRef.current) return { loading: false, error: null };
    const msg = e?.message || String(e);
    return { loading: false, error: msg };
  }
}

export default function DiagramPreview({ code, type, onRefresh, onInsert, showInsertButton = false }: DiagramPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mermaidFailed, setMermaidFailed] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const renderIdRef = useRef(0);
  const codeRef = useRef(code);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    codeRef.current = code;
  }, [code]);

  // 重置 retry 计数当 code 变化时
  useEffect(() => {
    setRetryCount(0);
  }, [code]);

  const renderDiagram = useCallback(() => {
    let resolvedCode = codeRef.current;
    
    // 尝试将 JSON 转换为 Mermaid 代码
    if (resolvedCode && resolvedCode.trim().startsWith("{")) {
      const converted = jsonToMermaid(resolvedCode);
      if (converted) {
        resolvedCode = converted;
      }
    }

    if (!resolvedCode || !resolvedCode.trim()) {
      setLoading(false);
      setError("无图表数据");
      setMermaidFailed(true);
      return;
    }

    if (resolvedCode.startsWith("%%") || resolvedCode.startsWith("//")) {
      setLoading(false);
      setError("图表数据格式异常，请重新生成");
      setMermaidFailed(true);
      return;
    }

    // 取消上一次未完成的渲染
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);
    setMermaidFailed(false);
    const currentRenderId = ++renderIdRef.current;

    const performRender = async () => {
      const result = await doRender(
        containerRef.current,
        resolvedCode,
        type,
        renderIdRef,
        currentRenderId,
      );
      if (controller.signal.aborted) return;
      if (currentRenderId !== renderIdRef.current) return;
      if (result.error) {
        setError(result.error);
        // 渲染失败后启用原生 React 图表降级（非文本表格）
        setMermaidFailed(true);
      }
      setLoading(false);
    };

    performRender();

    return () => {
      controller.abort();
    };
  }, [type, retryCount]);

  useEffect(() => {
    const cleanup = renderDiagram();
    return cleanup;
  }, [code, type, retryCount, renderDiagram]);

  // 重试处理
  const handleRetry = useCallback(() => {
    setRetryCount((prev) => prev + 1);
  }, []);

  // 插入到正文处理
  const handleInsert = useCallback(() => {
    if (!onInsert || !containerRef.current) return;
    const svg = containerRef.current.querySelector("svg");
    if (svg) {
      const svgHtml = new XMLSerializer().serializeToString(svg);
      onInsert(svgHtml);
    }
  }, [onInsert]);

  // 解析错误信息
  const parsedError = error ? parseMermaidError(error) : null;

  // 图表类型图标和标签映射
  const typeConfig: Record<string, { icon: React.ReactNode; label: string; color: string }> = {
    flowchart: { icon: <BarChartOutlined />, label: "流程图", color: "#1677ff" },
    gantt: { icon: <LineChartOutlined />, label: "甘特图", color: "#52c41a" },
    comparison: { icon: <PieChartOutlined />, label: "对比图", color: "#faad14" },
    architecture: { icon: <BarChartOutlined />, label: "架构图", color: "#722ed1" },
    labor: { icon: <LineChartOutlined />, label: "劳动力图", color: "#13c2c2" },
    layout: { icon: <BarChartOutlined />, label: "布局图", color: "#eb2f96" },
    timeline: { icon: <LineChartOutlined />, label: "时间线", color: "#fa8c16" },
  };

  const config = typeConfig[type] || { icon: <BarChartOutlined />, label: type, color: "#666" };

  return (
    <div>
      {/* 图表类型标识 + 加载状态 */}
      <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Tag color={config.color} icon={config.icon}>{config.label}</Tag>
        {loading && <Spin size="small" style={{ marginRight: 4 }} />}
        {loading && <Text type="secondary" style={{ fontSize: 12 }}>渲染中...</Text>}
        {!loading && error && (
          <Tag color="error" icon={<WarningOutlined />} style={{ fontSize: 11 }}>
            渲染失败
          </Tag>
        )}
        {retryCount > 0 && !loading && (
          <Tag color="processing" style={{ fontSize: 11 }}>
            已重试 {retryCount} 次
          </Tag>
        )}
      </div>

      {/* 加载指示器 */}
      {loading && (
        <div
          style={{
            textAlign: "center",
            padding: 40,
            background: "#fafafa",
            borderRadius: 8,
            border: "1px solid #f0f0f0",
          }}
        >
          <Spin size="large" />
          <div style={{ marginTop: 16, color: "#999", fontSize: 13 }}>
            正在生成图表可视化...
          </div>
          <div style={{ marginTop: 8, color: "#ccc", fontSize: 11 }}>
            {type === "flowchart" && "正在渲染流程图节点和连线"}
            {type === "gantt" && "正在渲染甘特图时间轴和任务条"}
            {type === "architecture" && "正在渲染架构层级结构"}
            {type === "comparison" && "正在渲染对比数据"}
            {type === "layout" && "正在渲染布局图"}
            {type === "timeline" && "正在渲染时间线"}
            {type === "labor" && "正在渲染劳动力分布"}
          </div>
        </div>
      )}
      
      {/* 错误提示 - 详细错误信息 */}
      {error && (
        <Alert
          type={mermaidFailed ? "warning" : "error"}
          message={
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              <Space>
                <Text strong style={{ fontSize: 13 }}>
                  {parsedError?.summary || "Mermaid 渲染失败"}
                </Text>
                {parsedError?.lineNumber && (
                  <Tag color="red" style={{ fontSize: 11, marginLeft: 8 }}>
                    第 {parsedError.lineNumber} 行
                    {parsedError.columnNumber ? `，第 ${parsedError.columnNumber} 列` : ""}
                  </Tag>
                )}
              </Space>
            </Space>
          }
          description={
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              {/* 详细错误信息 */}
              {parsedError?.detail && parsedError.detail !== parsedError?.summary && (
                <div
                  style={{
                    background: "#fff1f0",
                    border: "1px solid #ffa39e",
                    borderRadius: 4,
                    padding: "8px 12px",
                    fontSize: 12,
                    fontFamily: "monospace",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                    maxHeight: 120,
                    overflow: "auto",
                    color: "#cf1322",
                  }}
                >
                  {parsedError.detail}
                </div>
              )}

              {/* 语法错误位置提示 */}
              {parsedError?.lineNumber && (
                <div style={{ fontSize: 12, color: "#666" }}>
                  错误位置：第 {parsedError.lineNumber} 行
                  {parsedError.columnNumber ? `，第 ${parsedError.columnNumber} 列附近` : ""}
                  。请检查该行附近的 Mermaid 语法是否正确。
                </div>
              )}

              {/* 降级使用原生 React/SVG 图表组件（不做文本表格降级） */}
              {mermaidFailed && renderNativeChartFallback(type, code)}

              {/* 操作按钮组 */}
              <Space>
                <Button
                  size="small"
                  type="primary"
                  icon={<ReloadOutlined />}
                  onClick={handleRetry}
                  loading={loading}
                >
                  重试渲染
                </Button>
                {onRefresh && (
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={onRefresh}
                  >
                    重新生成图表
                  </Button>
                )}
                {showInsertButton && onInsert && (
                  <Button
                    size="small"
                    icon={<FileTextOutlined />}
                    onClick={handleInsert}
                    disabled={loading || !!error}
                  >
                    插入到正文
                  </Button>
                )}
              </Space>
            </Space>
          }
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}
      
      {/* 渲染成功 - 显示图表 */}
      {/* BUG 修复：容器必须常驻 DOM（原先受 !loading 条件渲染，doRender 执行时
          containerRef.current 必为 null，导致 Mermaid 渲染永远失败并走原生降级）。
          改为常驻挂载 + CSS 隐藏，加载/出错时不可见但 ref 始终有效。 */}
      {!mermaidFailed && (
        <div
          ref={containerRef}
          style={{
            overflow: "auto",
            padding: 16,
            background: "#fff",
            borderRadius: 8,
            border: "1px solid #f0f0f0",
            minHeight: 200,
            display: loading || error ? "none" : undefined,
          }}
        />
      )}

      {/* 降级使用原生 React/SVG 图表（非文本表格） */}
      {mermaidFailed && !error && (
        <div style={{ marginBottom: 12 }}>
          <Alert
            type="info"
            message="Mermaid 渲染不可用，已自动切换至原生图表展示"
            description={
              <Space direction="vertical" size="small">
                {renderNativeChartFallback(type, code)}
                {onRefresh && (
                  <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>
                    重新生成
                  </Button>
                )}
              </Space>
            }
            showIcon
          />
        </div>
      )}
      
      {/* 原始代码折叠显示 */}
      {!loading && (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: "pointer", fontSize: 12, color: "#999" }}>查看原始代码</summary>
          <div style={{ marginTop: 4 }}>
            <Text
              code
              style={{ fontSize: 11, whiteSpace: "pre-wrap", display: "block", maxHeight: 150, overflow: "auto" }}
            >
              {code}
            </Text>
          </div>
        </details>
      )}
    </div>
  );
}