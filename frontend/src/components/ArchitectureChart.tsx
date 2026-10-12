import React, { useMemo, useRef } from "react";

// ============================================================
//  配置
// ============================================================
const CONFIG = {
  rootWidth: 260,
  rootHeight: 50,
  level2Width: 210,
  level2VerticalWidth: 160,
  level2Height: 50,

  // L3 横向模式（L3 节点 ≤ 6 个时使用）
  level3Horizontal: {
    width: 140,
    height: 50,
    gap: 10,
  },

  // L3 竖向模式（L3 节点 > 6 个时使用）
  level3Vertical: {
    width: 60,
    height: 200,
    gap: 8,
  },

  level1Y: 0,
  level2Y: 120,
  level3Y: 240,
  padding: 40,
  busOffset: 30,
  arrowSize: 5,
};

// ============================================================
//  类型
// ============================================================
export interface ArchNodeData {
  id: string;
  label: string;
  variant: "root" | "level2" | "level3" | "level3 highlight";
  children?: ArchNodeData[];
}

interface PositionedNode {
  id: string;
  label: string;
  variant: string;
  x: number;
  y: number;
  width: number;
  height: number;
  depth: number;
  parentId: string | null;
}

interface Props {
  data: ArchNodeData;
  title?: string;
}

// ============================================================
//  工具函数
// ============================================================

/** 判断布局模式 */
function getLayoutMode(l3Count: number): "horizontal" | "vertical" {
  return l3Count > 6 ? "vertical" : "horizontal";
}

/** 计算节点尺寸 */
function getSize(variant: string, mode: "horizontal" | "vertical") {
  if (variant === "root") {
    return { w: CONFIG.rootWidth, h: CONFIG.rootHeight };
  }
  if (variant === "level2") {
    // 竖排模式下 L2 适度收窄，配合竖排叶子进一步压缩画布宽度
    const w = mode === "vertical" ? CONFIG.level2VerticalWidth : CONFIG.level2Width;
    return { w, h: CONFIG.level2Height };
  }
  if (mode === "vertical") {
    return { w: CONFIG.level3Vertical.width, h: CONFIG.level3Vertical.height };
  }
  return { w: CONFIG.level3Horizontal.width, h: CONFIG.level3Horizontal.height };
}

// ============================================================
//  布局算法
// ============================================================
function layoutTree(root: ArchNodeData, mode: "horizontal" | "vertical") {
  const nodeMap: Record<string, PositionedNode> = {};
  let leafIndex = 0;

  function layoutNode(node: ArchNodeData, depth: number, parentId: string | null): number {
    const { w: width, h: height } = getSize(node.variant, mode);

    let x: number;
    if (!node.children || node.children.length === 0) {
      // 叶子节点：横向依次排列
      const gap = mode === "vertical" ? CONFIG.level3Vertical.gap : CONFIG.level3Horizontal.gap;
      const stride = width + gap;
      x = CONFIG.padding + width / 2 + leafIndex * stride;
      leafIndex++;
    } else {
      // 非叶子：居中于子节点
      const childXs = node.children.map(c => layoutNode(c, depth + 1, node.id));
      x = (Math.min(...childXs) + Math.max(...childXs)) / 2;
    }

    const y = CONFIG.padding + height / 2 +
      (depth === 0 ? CONFIG.level1Y :
       depth === 1 ? CONFIG.level2Y : CONFIG.level3Y);

    nodeMap[node.id] = {
      id: node.id,
      label: node.label,
      variant: node.variant,
      x,
      y,
      width,
      height,
      depth,
      parentId,
    };
    return x;
  }

  layoutNode(root, 0, null);
  return nodeMap;
}

// ============================================================
//  连线构建
// ============================================================
function buildEdges(nodeMap: Record<string, PositionedNode>) {
  const childrenByParent: Record<string, PositionedNode[]> = {};
  Object.values(nodeMap).forEach(n => {
    if (n.parentId) {
      (childrenByParent[n.parentId] ||= []).push(n);
    }
  });

  let pathD = "";
  const arrowPositions: { x: number; y: number }[] = [];

  Object.entries(childrenByParent).forEach(([parentId, children]) => {
    const parent = nodeMap[parentId];
    if (!parent) return;

    const startX = parent.x;
    const startY = parent.y + parent.height / 2;
    const busY = startY + CONFIG.busOffset;

    const leftX = Math.min(...children.map(c => c.x));
    const rightX = Math.max(...children.map(c => c.x));

    // 主干线
    pathD += `M ${startX} ${startY} L ${startX} ${busY} `;
    // 母线
    if (children.length > 1) {
      pathD += `M ${leftX} ${busY} L ${rightX} ${busY} `;
    }
    // 各子节点分支
    children.forEach(c => {
      const childTopY = c.y - c.height / 2;
      const arrowSpace = CONFIG.arrowSize * 1.6;
      pathD += `M ${c.x} ${busY} L ${c.x} ${childTopY - arrowSpace} `;
      arrowPositions.push({ x: c.x, y: childTopY });
    });
  });

  return { pathD, arrowPositions };
}

// ============================================================
//  节点样式
// ============================================================
function getNodeStyle(variant: string, mode: "horizontal" | "vertical"): React.CSSProperties {
  const base: React.CSSProperties = {
    position: "absolute",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 4,
    color: "#fff",
    textAlign: "center",
    transition: "transform .2s, box-shadow .2s",
    zIndex: 2,
    userSelect: "none",
    boxSizing: "border-box",
    cursor: "default",
  };

  if (variant === "root") {
    return {
      ...base,
      background: "#1f3a63",
      fontSize: 14,
      fontWeight: 600,
      padding: "0 12px",
      lineHeight: 1.3,
    };
  }

  if (variant === "level2") {
    return {
      ...base,
      background: "#4a7cc7",
      fontSize: 13,
      fontWeight: 500,
      padding: "0 12px",
      lineHeight: 1.3,
    };
  }

  // L3 节点
  const isHighlight = variant.includes("highlight");

  if (mode === "vertical") {
    return {
      ...base,
      background: isHighlight ? "#f5b800" : "#4a7cc7",
      color: isHighlight ? "#1f3a63" : "#fff",
      fontSize: 13,
      fontWeight: isHighlight ? 600 : 500,

      // 关键：垂直文字排列
      writingMode: "vertical-rl" as any,
      textOrientation: "upright" as any,
      letterSpacing: 2,

      // 内边距（上下留白给文字垂直延伸）
      padding: "15px 6px",

      // 长文本自动换行
      whiteSpace: "normal",
      wordBreak: "break-all",
      lineHeight: 1.4,
    };
  }

  // L3 横向模式
  return {
    ...base,
    background: isHighlight ? "#f5b800" : "#4a7cc7",
    color: isHighlight ? "#1f3a63" : "#fff",
    fontSize: 12,
    fontWeight: isHighlight ? 600 : 500,
    padding: "0 8px",
    lineHeight: 1.3,
  };
}

// ============================================================
//  悬浮效果处理
// ============================================================
function useHoverEffect() {
  return {
    onMouseEnter: (e: React.MouseEvent<HTMLDivElement>) => {
      e.currentTarget.style.transform = "translateY(-2px)";
      e.currentTarget.style.boxShadow = "0 6px 18px rgba(30, 80, 160, .28)";
    },
    onMouseLeave: (e: React.MouseEvent<HTMLDivElement>) => {
      e.currentTarget.style.transform = "";
      e.currentTarget.style.boxShadow = "";
    },
  };
}

// ============================================================
//  组件
// ============================================================
const ArchitectureChart: React.FC<Props> = ({ data, title }) => {
  // 统计 L3 叶子节点数
  const l3Count = useMemo(() => {
    let count = 0;
    function countLeaves(node: ArchNodeData) {
      if (!node.children || node.children.length === 0) {
        count++;
        return;
      }
      node.children.forEach(countLeaves);
    }
    countLeaves(data);
    return count;
  }, [data]);

  const mode = getLayoutMode(l3Count);

  // 布局计算
  const { nodeMap, canvasW, canvasH } = useMemo(() => {
    const map = layoutTree(data, mode);
    const allNodes = Object.values(map);
    const w = Math.max(...allNodes.map(n => n.x + n.width / 2)) + CONFIG.padding;
    const h = Math.max(...allNodes.map(n => n.y + n.height / 2)) + CONFIG.padding;
    return { nodeMap: map, canvasW: Math.max(600, w), canvasH: h };
  }, [data, mode]);

  // 连线
  const { pathD, arrowPositions } = useMemo(
    () => buildEdges(nodeMap),
    [nodeMap]
  );

  const allNodes = Object.values(nodeMap);
  const hoverHandlers = useHoverEffect();
  const containerRef = useRef<HTMLDivElement>(null);

  return (
    <div
      ref={containerRef}
      style={{ position: "relative", display: "inline-block", maxWidth: "100%" }}
    >
      {/* 标题 */}
      {title && (
        <div
          style={{
            fontSize: 18,
            fontWeight: "bold",
            color: "#1f3a63",
            marginBottom: 24,
            letterSpacing: 1,
            textAlign: "center",
          }}
        >
          {title}
        </div>
      )}

      {/* 画布 */}
      <div
        style={{
          position: "relative",
          width: canvasW,
          height: canvasH,
          overflow: "auto",
          background: "#fff",
          borderRadius: 8,
          border: "1px solid #f0f0f0",
          padding: 16,
        }}
      >
        {/* SVG 连线层 */}
        <svg
          width="100%"
          height="100%"
          style={{
            position: "absolute",
            top: 16,
            left: 16,
            pointerEvents: "none",
            zIndex: 1,
          }}
        >
          <path
            d={pathD}
            fill="none"
            stroke="#5a7ca8"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {arrowPositions.map((pos, i) => {
            const s = CONFIG.arrowSize;
            return (
              <polygon
                key={`arrow-${i}`}
                points={`${pos.x},${pos.y} ${pos.x - s},${pos.y - s * 1.4} ${pos.x + s},${pos.y - s * 1.4}`}
                fill="#5a7ca8"
              />
            );
          })}
        </svg>

        {/* 节点层 */}
        {allNodes.map(n => (
          <div
            key={n.id}
            style={{
              ...getNodeStyle(n.variant, mode),
              width: n.width,
              height: n.height,
              left: n.x - n.width / 2,
              top: n.y - n.height / 2,
            }}
            {...hoverHandlers}
            title={n.label}
          >
            {n.label}
          </div>
        ))}
      </div>

      {/* 布局模式指示 */}
      <div
        style={{
          marginTop: 8,
          fontSize: 11,
          color: "#999",
          textAlign: "center",
        }}
      >
        层级深度：{Math.max(...allNodes.map(n => n.depth)) + 1}
        &nbsp;·&nbsp;
        节点总数：{allNodes.length}
        &nbsp;·&nbsp;
        L3 布局：{mode === "vertical" ? "竖排" : "横排"}
      </div>
    </div>
  );
};

export default ArchitectureChart;
