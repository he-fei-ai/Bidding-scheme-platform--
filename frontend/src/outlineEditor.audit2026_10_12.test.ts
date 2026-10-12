/**
 * 生成大纲模块 —— 2026-10-12 深度审计回归测试（前端侧）。
 *
 * 覆盖本轮修复的前端缺陷：
 *   FE-1  编号命名空间分裂：V5.1 审核弹窗自算 `index+1.ci+1`，与主目录树
 *         （第一节、/ 一、/ 1、）和后端口径（1 / 1.2 / 1.2.3）三套并存。
 *   FE-2  标题改名后前后端 title 分歧：后端 `_strip_title_numbering` 会剥掉
 *         编号前缀再入库，前端却用原始输入回写本地树 → 双重编号。
 *   FE-3  死代码 + 进度断链：`handleReorder` 不可达、`outlineProgress` 从不
 *         写入（章节进度区块恒不渲染）、`taskStore.updateTask` 从不调用
 *         （全局任务条恒 0%）、提示文案谎称支持拖拽排序。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import type { SectionTreeNode } from "./types";
import {
  annotateOutlineWithNumbers,
  formatV21Heading,
  outlineDictToTreeNodes,
} from "./utils";

const PAGE_SRC = readFileSync(
  resolve(__dirname, "pages/OutlineEditorPage.tsx"),
  "utf-8",
);

// ===========================================================================
// FE-1：编号命名空间统一
// ===========================================================================

describe("FE-1 审核弹窗与主目录树共用同一套编号", () => {
  const dictTree = [
    {
      id: "1",
      title: "施工部署",
      children: [
        {
          id: "1.1",
          title: "资源配置",
          children: [
            { id: "1.1.1", title: "劳动力配置" },
            { id: "1.1.2", title: "机械台班配置" },
          ],
        },
        { id: "1.2", title: "进度控制" },
      ],
    },
    { id: "2", title: "质量管理" },
  ];

  it("审核弹窗复用 annotateOutlineWithNumbers，而不是自算位置号", () => {
    expect(PAGE_SRC).toContain("annotateOutlineWithNumbers(pendingReviewTree)");
    // 旧的 ad-hoc 编号表达式必须已删除
    expect(PAGE_SRC).not.toMatch(/\{\s*index\s*\+\s*1\s*\}\s*\.?\s*\{\s*ci\s*\+\s*1\s*\}/);
  });

  it("三层节点的展示编号与主树完全一致", () => {
    const annotated = annotateOutlineWithNumbers(
      outlineDictToTreeNodes(dictTree),
    );
    const [l1] = annotated;
    const [l2a, l2b] = l1.children;
    const [l3a, l3b] = l2a.children;

    // 主树口径（V3.1）：第一节、/ 一、/ 1、
    expect(l1.number).toBe(formatV21Heading(1, [1], ""));
    expect(l2a.number).toBe(formatV21Heading(2, [1], ""));
    expect(l3a.number).toBe(formatV21Heading(3, [1], ""));

    // 审核弹窗的渲染点全部取 node.number
    expect(PAGE_SRC).toContain("node.number || index + 1");
    expect(PAGE_SRC).toContain("child.number ||");
    expect(PAGE_SRC).toContain("grandchild.number ||");

    // 编号落在不同层级，互不串扰
    const all = [l1, l2a, l2b, l3a, l3b].map((n) => n.number);
    expect(new Set(all).size).toBeGreaterThanOrEqual(3);
  });

  it("SectionTreeNode.number 是类型上的可选展示字段", () => {
    const typesSrc = readFileSync(resolve(__dirname, "types/index.ts"), "utf-8");
    expect(typesSrc).toMatch(/number\?:\s*string/);
  });

  it("审核弹窗中「下移」禁用态绑定到标注树而非可能为 null 的原始树", () => {
    expect(PAGE_SRC).not.toMatch(/index === pendingReviewTree\.length - 1/);
    expect(PAGE_SRC).toMatch(/annotatedReviewTree\?\.length \?\? 0/);
  });
});

// ===========================================================================
// FE-2：标题改名后前后端一致
// ===========================================================================

describe("FE-2 改名以服务端返回的规范标题为准", () => {
  it("handleEditTitle 消费 updateSection 的返回值，而非丢弃后用原始输入回写", () => {
    const i = PAGE_SRC.indexOf("const handleEditTitle");
    expect(i).toBeGreaterThan(-1);
    const body = PAGE_SRC.slice(i, i + 2600);
    expect(body).toMatch(/const\s+saved\s*=\s*await\s+updateSection\(/);
    expect(body).toContain("canonicalTitle");
    // 回写用的是规范标题
    expect(body).toMatch(/updateNodeInTree\(prev,\s*node\.id,\s*\{\s*title:\s*canonicalTitle\s*\}\)/);
  });

  it("后端剥掉编号前缀时给出显式提示（用户不会误以为保存失败）", () => {
    const i = PAGE_SRC.indexOf("const handleEditTitle");
    const body = PAGE_SRC.slice(i, i + 2600);
    expect(body).toContain("编号前缀已自动移除");
  });
});

// ===========================================================================
// FE-3：死代码清理与进度接线
// ===========================================================================

describe("FE-3 死代码清理与跨模块进度接线", () => {
  it("移除不可达的 handleReorder（无任何 JSX 引用）", () => {
    expect(PAGE_SRC).not.toContain("handleReorder");
  });

  it("移除未使用的 VersionHistory import 与失效的 strategy 常量", () => {
    expect(PAGE_SRC).not.toMatch(/import\s+VersionHistory/);
    expect(PAGE_SRC).not.toMatch(/const\s+strategy\s*=/);
  });

  it("提示文案不再谎称支持拖拽排序", () => {
    expect(PAGE_SRC).not.toContain("拖拽目录节点可调整顺序");
  });

  it("真正消费 SSE 的 progress 字段，章节进度区块不再是死 UI", () => {
    expect(PAGE_SRC).toContain("setOutlineProgress((prev)");
    const i = PAGE_SRC.indexOf('data.status === "generating"');
    const body = PAGE_SRC.slice(i, i + 2200);
    expect(body).toMatch(/\.progress\b/);
  });

  it("全局任务条进度随生成推进（taskStore.updateTask 被调用）", () => {
    expect(PAGE_SRC).toContain("taskStore.updateTask(");
    expect(PAGE_SRC).toContain("taskIdRef.current");
    const i = PAGE_SRC.indexOf("taskStore.startTask({");
    expect(i).toBeGreaterThan(-1);
    expect(PAGE_SRC.slice(i - 400, i)).toContain("taskIdRef.current = _taskId");
  });
});

// ===========================================================================
// 编号派生在审核态「未落库临时 id」下的稳健性（既有防线，防回归）
// ===========================================================================

describe("审核态临时 id 下编号仍正确", () => {
  const makeDeep = (): SectionTreeNode[] => [
    {
      id: -1,
      title: "L1a",
      level: 1,
      sort_order: 1,
      section_type: "text",
      scoring_point_id: null,
      status: "pending",
      has_content: false,
      word_count: 0,
      children: [
        {
          id: -1,
          title: "L2a",
          level: 2,
          sort_order: 1,
          section_type: "text",
          scoring_point_id: null,
          status: "pending",
          has_content: false,
          word_count: 0,
          children: [
            {
              id: -1,
              title: "L3a",
              level: 3,
              sort_order: 1,
              section_type: "text",
              scoring_point_id: null,
              status: "pending",
              has_content: false,
              word_count: 0,
              children: [],
            },
          ],
        },
      ],
    },
  ];

  it("id 全部重复时编号仍按位置派生且互不串扰", () => {
    const out = annotateOutlineWithNumbers(makeDeep());
    expect(out[0].number).toBe("第一节、");
    expect(out[0].children[0].number).toBe("一、");
    expect(out[0].children[0].children[0].number).toBe("1、");
  });

  it("超深层级不爆栈", () => {
    let node: SectionTreeNode = {
      id: 1, title: "L2000", level: 1, sort_order: 1, section_type: "text",
      scoring_point_id: null, status: "pending", has_content: false,
      word_count: 0, children: [],
    };
    const root = node;
    for (let i = 2; i <= 2000; i += 1) {
      const child: SectionTreeNode = {
        id: i, title: `层${i}`, level: Math.min(i, 8), sort_order: 1,
        section_type: "text", scoring_point_id: null, status: "pending",
        has_content: false, word_count: 0, children: [],
      };
      node.children = [child];
      node = child;
    }
    expect(() => annotateOutlineWithNumbers([root])).not.toThrow();
  });
});
