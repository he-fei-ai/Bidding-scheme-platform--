import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Statistic,
  Steps,
  Tag,
  Tooltip,
  Tree,
  Typography,
  message,
} from "antd";
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  FileWordOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  addFeasibilitySource,
  clearFeasibilityWorkspace,
  getFeasibilityChapterKnowledge,
  getFeasibilityKnowledgeDocuments,
  getFeasibilityOutlineTemplates,
  getFeasibilityReport,
  getFeasibilityReportUrl,
  removeFeasibilitySource,
  runFeasibilityAnalysis,
  runFeasibilityContent,
  runFeasibilityOutline,
  runFeasibilityParameters,
  saveFeasibilityAnalysis,
  saveFeasibilityChapter,
  saveFeasibilityChapterKnowledge,
  saveFeasibilityOutline,
  saveFeasibilityOutlineConfig,
  saveFeasibilityParameters,
  saveFeasibilityProjectInfo,
  saveFeasibilityReferenceDocuments,
} from "../api";
import type {
  FeasibilityChapterKnowledge,
  FeasibilityKnowledgeDocument,
  FeasibilityOutlineNode,
  FeasibilityReportState,
  FeasibilityTaskType,
} from "../api";
import { extractApiError } from "../utils";

const { TextArea } = Input;
const { Text, Paragraph } = Typography;

const STEP_ITEMS = [
  { key: "materials", title: "项目资料" },
  { key: "analysis", title: "资料分析" },
  { key: "outline", title: "报告目录" },
  { key: "parameters", title: "关键参数" },
  { key: "content", title: "正文生成" },
];

const TASK_LABELS: Record<FeasibilityTaskType, string> = {
  "feasibility-analysis": "资料分析",
  "feasibility-outline": "报告目录",
  "feasibility-parameters": "关键参数",
  "feasibility-content": "正文生成",
};

const TASK_TYPES = Object.keys(TASK_LABELS) as FeasibilityTaskType[];

// ===== 目录树纯函数（移植易标 OutlinePage 的 mapItems/removeItem/renumberWithMap）=====
// 说明：目录编号既是显示编号也是节点主键，任何增删改排序后都必须整树重新编号，
// 并把「旧编号 → 新编号」映射回传给后端，否则已生成的章节正文会丢或错挂。

/** 深度优先变换每个节点（children 先变换） */
function mapOutline(
  items: FeasibilityOutlineNode[],
  mapper: (node: FeasibilityOutlineNode) => FeasibilityOutlineNode,
): FeasibilityOutlineNode[] {
  return items.map((item) => {
    const withChildren = item.children?.length
      ? { ...item, children: mapOutline(item.children, mapper) }
      : item;
    return mapper(withChildren);
  });
}

/** 删除任意层级节点（返回浅拷贝的新树） */
function removeOutlineNode(items: FeasibilityOutlineNode[], id: string): FeasibilityOutlineNode[] {
  return items
    .filter((item) => item.id !== id)
    .map((item) => (item.children?.length ? { ...item, children: removeOutlineNode(item.children, id) } : item));
}

/** 在指定父节点下追加子节点 */
function appendChildNode(
  items: FeasibilityOutlineNode[],
  parentId: string,
  child: FeasibilityOutlineNode,
): FeasibilityOutlineNode[] {
  return items.map((item) => {
    if (item.id === parentId) {
      return { ...item, children: [...(item.children || []), child] };
    }
    return item.children?.length
      ? { ...item, children: appendChildNode(item.children, parentId, child) }
      : item;
  });
}

/** 同层上移 / 下移（delta = -1 / 1），跨层不移动 */
function moveSiblingNode(items: FeasibilityOutlineNode[], id: string, delta: number): FeasibilityOutlineNode[] {
  const index = items.findIndex((item) => item.id === id);
  if (index >= 0) {
    const target = index + delta;
    if (target < 0 || target >= items.length) return items;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  }
  return items.map((item) =>
    item.children?.length ? { ...item, children: moveSiblingNode(item.children, id, delta) } : item,
  );
}

/** 整树重新编号，返回新树与「旧编号 → 新编号」映射 */
function renumberOutline(
  items: FeasibilityOutlineNode[],
  prefix = "",
): { outline: FeasibilityOutlineNode[]; idMap: Record<string, string> } {
  const idMap: Record<string, string> = {};
  const outline = items.map((item, index) => {
    const id = prefix ? `${prefix}.${index + 1}` : `${index + 1}`;
    const child = item.children?.length ? renumberOutline(item.children, id) : null;
    idMap[item.id] = id;
    if (child) Object.assign(idMap, child.idMap);
    return { ...item, id, children: child?.outline };
  });
  return { outline, idMap };
}

function findOutlineNode(items: FeasibilityOutlineNode[], id: string): FeasibilityOutlineNode | null {
  for (const item of items) {
    if (item.id === id) return item;
    if (item.children?.length) {
      const found = findOutlineNode(item.children, id);
      if (found) return found;
    }
  }
  return null;
}

function collectOutlineIds(items: FeasibilityOutlineNode[], ids: string[] = []): string[] {
  items.forEach((item) => {
    ids.push(item.id);
    if (item.children?.length) collectOutlineIds(item.children, ids);
  });
  return ids;
}

/** 返回包含指定节点的同层数组（用于判断能否上移/下移） */
function findSiblings(items: FeasibilityOutlineNode[], id: string): FeasibilityOutlineNode[] {
  if (items.some((item) => item.id === id)) return items;
  for (const item of items) {
    if (item.children?.length) {
      const found = findSiblings(item.children, id);
      if (found.length) return found;
    }
  }
  return [];
}

interface OutlineTreeNode {
  key: string;
  title: string;
  children?: OutlineTreeNode[];
}

function toOutlineTreeNode(node: FeasibilityOutlineNode): OutlineTreeNode {
  return {
    key: node.id,
    title: `${node.id} ${node.title}`,
    children: node.children?.length ? node.children.map(toOutlineTreeNode) : undefined,
  };
}

/** 目录层级（"2.1.3" → 3），最多三级 */
function outlineLevel(id: string): number {
  return id.split(".").length;
}

export default function FeasibilityReportPage() {
  const [report, setReport] = useState<FeasibilityReportState | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [form] = Form.useForm();
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  const [sourceName, setSourceName] = useState("");
  const [sourceContent, setSourceContent] = useState("");
  const [analysisDraft, setAnalysisDraft] = useState("");
  const [paramsDraft, setParamsDraft] = useState("");
  const [chapterDraft, setChapterDraft] = useState("");
  const [activeChapter, setActiveChapter] = useState<string>("");
  const [templates, setTemplates] = useState<Record<string, { label: string; chapters: string[] }>>({});
  // 目录树形编辑器
  const [selectedNodeId, setSelectedNodeId] = useState<string>("");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [outlineSaving, setOutlineSaving] = useState(false);
  // 参考知识库选材
  const [knowledgeDocs, setKnowledgeDocs] = useState<FeasibilityKnowledgeDocument[]>([]);
  const [chapterKnowledge, setChapterKnowledge] = useState<FeasibilityChapterKnowledge | null>(null);
  const [knowledgeDraft, setKnowledgeDraft] = useState<number[]>([]);

  const refresh = useCallback(async () => {
    try {
      const r = await getFeasibilityReport();
      if (!mountedRef.current) return;
      setReport(r);
      setAnalysisDraft((prev) => prev || r.analysisMarkdown || "");
      setParamsDraft((prev) => prev || r.keyParametersMarkdown || "");
    } catch (e) {
      message.error(`加载可研工作区失败：${extractApiError(e)}`);
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    refresh();
    getFeasibilityOutlineTemplates()
      .then((t) => mountedRef.current && setTemplates(t))
      .catch(() => undefined);
    getFeasibilityKnowledgeDocuments()
      .then((r) => mountedRef.current && setKnowledgeDocs(r.documents))
      .catch(() => undefined);
    return () => {
      mountedRef.current = false;
    };
  }, [refresh]);

  const runningTask = useMemo(() => {
    if (!report) return null;
    return TASK_TYPES.map((t) => report[t]).find((s) => s?.status === "running") || null;
  }, [report]);

  // 有任务在跑时轮询工作区状态（任务行由后端持久化，刷新页面也不丢）
  useEffect(() => {
    if (runningTask) {
      if (!pollRef.current) {
        pollRef.current = setInterval(refresh, 3000);
      }
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [runningTask, refresh]);

  const stepIndex = Math.max(0, STEP_ITEMS.findIndex((s) => s.key === report?.step));

  const guardIdle = () => {
    if (runningTask) {
      message.warning(`${TASK_LABELS[runningTask.type]} 任务正在运行，请等待完成`);
      return false;
    }
    return true;
  };

  const doStart = async (fn: () => Promise<unknown>, successText: string) => {
    if (!guardIdle()) return;
    setStarting(true);
    try {
      await fn();
      message.success(successText);
      await refresh();
    } catch (e: any) {
      if (e?.response?.status !== 409) {
        message.error(`启动失败：${extractApiError(e)}`);
      }
    } finally {
      setStarting(false);
    }
  };

  const leaves = useMemo(() => {
    const out: FeasibilityOutlineNode[] = [];
    const walk = (nodes: FeasibilityOutlineNode[], trail: string[]) =>
      nodes.forEach((n) => {
        const nextTrail = [...trail, n.title];
        if (n.children?.length) walk(n.children, nextTrail);
        else out.push({ ...n, description: nextTrail.join(" > ") });
      });
    walk(report?.outlineData?.outline || [], []);
    return out;
  }, [report?.outlineData]);

  // ===== 目录树编辑（ENHANCE 2026-09-15）=====

  const outlineTree = useMemo(() => report?.outlineData?.outline || [], [report?.outlineData]);
  const outlineNode = useMemo(
    () => (selectedNodeId ? findOutlineNode(outlineTree, selectedNodeId) : null),
    [outlineTree, selectedNodeId],
  );

  // 目录变化后：展开全部节点，并保证选中项仍存在
  useEffect(() => {
    if (!outlineTree.length) return;
    const ids = collectOutlineIds(outlineTree);
    setExpandedKeys((prev) => Array.from(new Set([...prev.filter((k) => ids.includes(k)), ...ids])));
    setSelectedNodeId((prev) => (prev && ids.includes(prev) ? prev : outlineTree[0].id));
  }, [outlineTree]);

  // 选中节点变化时同步「标题 / 写作重点」草稿
  useEffect(() => {
    setEditTitle(outlineNode?.title || "");
    setEditDescription(outlineNode?.description || "");
  }, [outlineNode?.id, outlineNode?.title, outlineNode?.description]);

  /**
   * 保存目录树：整树重新编号 → 回传 idMap 让后端把正文搬过去。
   * affectedIds 传「新树中应失效的节点 id」（后端同时兼容旧 id 匹配）。
   */
  const persistOutline = async (
    nextTree: FeasibilityOutlineNode[],
    reason: string,
    affectedIds: string[] = [],
  ) => {
    if (!guardIdle()) return;
    setOutlineSaving(true);
    try {
      const { outline: numbered, idMap } = renumberOutline(nextTree);
      const next = await saveFeasibilityOutline(
        { outline: numbered },
        reason,
        idMap,
        affectedIds.map((id) => idMap[id] ?? id),
      );
      setReport(next);
      setSelectedNodeId((prev) => (prev ? idMap[prev] ?? "" : ""));
      setExpandedKeys((prev) => prev.map((k) => idMap[k] ?? k));
      message.success(reason === "sort" ? "目录顺序已保存" : "目录已保存");
    } catch (e: any) {
      if (e?.response?.status === 409) {
        message.warning(extractApiError(e));
      } else {
        message.error(`保存目录失败：${extractApiError(e)}`);
      }
    } finally {
      setOutlineSaving(false);
    }
  };

  const addRootNode = () => {
    const nextTree = [...outlineTree, { id: "temp", title: "新章节", description: "" }];
    // 新一级节点的编号 = 根节点数量
    void persistOutline(nextTree, "add-root", [String(nextTree.length)]);
  };

  const addChildNodeById = (parentId: string) => {
    if (outlineLevel(parentId) >= 3) {
      message.warning("最多支持三级目录");
      return;
    }
    const child: FeasibilityOutlineNode = { id: `${parentId}.temp`, title: "新小节", description: "" };
    // 父节点由叶子变成父节点，原正文不再适用 → 置为失效
    void persistOutline(appendChildNode(outlineTree, parentId, child), "add-child", [parentId]);
  };

  const removeNodeById = (id: string) => {
    void persistOutline(removeOutlineNode(outlineTree, id), "delete");
  };

  const moveNode = (id: string, delta: number) => {
    void persistOutline(moveSiblingNode(outlineTree, id, delta), "sort");
  };

  const saveNodeEdit = () => {
    if (!outlineNode) return;
    const title = editTitle.trim();
    if (!title) {
      message.warning("请填写章节标题");
      return;
    }
    const description = editDescription.trim();
    // 写作重点变了，原正文不再匹配 → 置为失效；仅改标题则保留正文
    const descriptionChanged = description !== (outlineNode.description || "");
    void persistOutline(
      mapOutline(outlineTree, (n) => (n.id === outlineNode.id ? { ...n, title, description } : n)),
      "edit",
      descriptionChanged ? [outlineNode.id] : [],
    );
  };

  const saveReferenceDocuments = async (documentIds: number[]) => {
    try {
      const next = await saveFeasibilityReferenceDocuments(documentIds);
      setReport(next);
      message.success(documentIds.length ? `已选 ${documentIds.length} 份参考资料` : "已清空参考资料");
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  // 查看某章节的知识库选材（自动匹配 / 人工指定）
  useEffect(() => {
    if (!activeChapter) {
      setChapterKnowledge(null);
      setKnowledgeDraft([]);
      return;
    }
    let alive = true;
    getFeasibilityChapterKnowledge(activeChapter)
      .then((data) => {
        if (!alive) return;
        setChapterKnowledge(data);
        setKnowledgeDraft(data.selectedItemIds);
      })
      .catch(() => {
        if (!alive) return;
        setChapterKnowledge(null);
        setKnowledgeDraft([]);
      });
    return () => {
      alive = false;
    };
  }, [activeChapter, report?.outlineData]);

  const saveChapterKnowledge = async (itemIds: number[]) => {
    if (!activeChapter) return;
    try {
      const next = await saveFeasibilityChapterKnowledge(activeChapter, itemIds);
      setReport(next);
      const data = await getFeasibilityChapterKnowledge(activeChapter);
      setChapterKnowledge(data);
      setKnowledgeDraft(data.selectedItemIds);
      message.success(itemIds.length ? "章节选材已保存" : "已恢复自动匹配");
    } catch (e) {
      message.error(extractApiError(e));
    }
  };

  const leafCount = leaves.length;
  const doneCount = leaves.filter((l) => (l.content || "").trim()).length;

  if (loading) {
    return (
      <div style={{ padding: 32, textAlign: "center" }}>
        <Spin tip="加载可研工作区..." />
      </div>
    );
  }
  if (!report) return <Empty description="工作区不可用" />;

  const info = report.projectInfo;

  // ===== 步骤 0：项目资料 =====
  const renderMaterials = () => (
    <>
      <Card title="项目基础参数" style={{ marginBottom: 16 }}>
        <Form
          form={form}
          layout="vertical"
          initialValues={info}
          onFinish={async (values) => {
            try {
              await saveFeasibilityProjectInfo(values);
              message.success("项目参数已保存，下游分析/目录/参数/正文已失效");
              await refresh();
            } catch (e) {
              message.error(extractApiError(e));
            }
          }}
        >
          <Space wrap size={16} align="start">
            <Form.Item name="projectName" label="项目名称" rules={[{ required: true, message: "请填写项目名称" }]}>
              <Input style={{ width: 280 }} />
            </Form.Item>
            <Form.Item name="projectType" label="项目类型">
              <Select
                style={{ width: 160 }}
                options={[
                  { value: "government", label: "政府投资项目" },
                  { value: "enterprise", label: "企业投资项目" },
                ]}
              />
            </Form.Item>
            <Form.Item name="industry" label="所属行业">
              <Input style={{ width: 200 }} />
            </Form.Item>
            <Form.Item name="constructionUnit" label="建设单位">
              <Input style={{ width: 220 }} />
            </Form.Item>
            <Form.Item name="location" label="建设地点">
              <Input style={{ width: 200 }} />
            </Form.Item>
            <Form.Item name="constructionPeriodYears" label="建设期（年）">
              <Input style={{ width: 110 }} />
            </Form.Item>
            <Form.Item name="operationPeriodYears" label="运营期（年）">
              <Input style={{ width: 110 }} />
            </Form.Item>
            <Form.Item name="totalInvestment" label="总投资">
              <Input style={{ width: 180 }} placeholder="如 12000 万元" />
            </Form.Item>
            <Form.Item name="fundingSource" label="资金来源">
              <Input style={{ width: 220 }} />
            </Form.Item>
          </Space>
          <Form.Item name="constructionContent" label="建设内容与规模">
            <TextArea rows={3} style={{ maxWidth: 860 }} placeholder="项目建设内容、规模、主要产出等" />
          </Form.Item>
          <Button type="primary" htmlType="submit">
            保存项目参数
          </Button>
        </Form>
      </Card>

      <Card title={`项目资料文件（${report.sourceFiles.length}）`} style={{ marginBottom: 16 }}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="支持粘贴多份资料（可研申报材料、规划文本、批复文件等）。AI 只提取资料事实，缺项统一写【待补充】，不会编造数字。"
        />
        {report.sourceFiles.map((f) => (
          <div key={f.id} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <Tag color="blue">{f.fileName}</Tag>
            <Text type="secondary">{f.markdownChars} 字符</Text>
            <Button
              size="small"
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={async () => {
                try {
                  await removeFeasibilitySource(f.id);
                  message.success("已移除资料");
                  await refresh();
                } catch (e) {
                  message.error(extractApiError(e));
                }
              }}
            />
          </div>
        ))}
        <Space direction="vertical" style={{ width: "100%", marginTop: 8 }} size={8}>
          <Input
            placeholder="资料名称（如：可研申报材料）"
            value={sourceName}
            onChange={(e) => setSourceName(e.target.value)}
            style={{ maxWidth: 360 }}
          />
          <TextArea
            rows={6}
            placeholder="粘贴资料文本内容..."
            value={sourceContent}
            onChange={(e) => setSourceContent(e.target.value)}
          />
          <Button
            icon={<PlusOutlined />}
            disabled={!sourceContent.trim()}
            onClick={async () => {
              try {
                await addFeasibilitySource(sourceName, sourceContent);
                setSourceName("");
                setSourceContent("");
                message.success("资料已添加，下游产物已失效");
                await refresh();
              } catch (e) {
                message.error(extractApiError(e));
              }
            }}
          >
            添加资料
          </Button>
        </Space>
      </Card>

      <Button
        type="primary"
        icon={<PlayCircleOutlined />}
        loading={starting}
        disabled={runningTask !== null || !info.projectName}
        onClick={() =>
          doStart(runFeasibilityAnalysis, "资料分析已启动，可在页面顶部查看进度")
        }
      >
        下一步：AI 资料分析
      </Button>
    </>
  );

  // ===== 步骤 1：资料分析 =====
  const renderAnalysis = () => (
    <>
      <Card title="资料分析（九板块事实）" style={{ marginBottom: 16 }}>
        {report.analysisMarkdown ? (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="可人工补充修正分析结论；保存后目录/参数/正文会重新生效。"
              description={
                (report.referenceDocumentIds || []).length
                  ? `已选 ${report.referenceDocumentIds.length} 份参考知识库文档：分析阶段只把它们当作行业背景与术语参考，不会作为本项目事实来源。`
                  : "未选择参考知识库（可在「报告目录」步骤勾选）：分析只依据项目参数与资料文件。"
              }
            />
            <TextArea
              rows={18}
              value={analysisDraft}
              onChange={(e) => setAnalysisDraft(e.target.value)}
              style={{ marginBottom: 12 }}
            />
            <Space>
              <Button
                type="primary"
                onClick={async () => {
                  try {
                    await saveFeasibilityAnalysis(analysisDraft);
                    message.success("资料分析已保存");
                    await refresh();
                  } catch (e) {
                    message.error(extractApiError(e));
                  }
                }}
              >
                保存分析
              </Button>
              <Button
                icon={<PlayCircleOutlined />}
                loading={starting}
                onClick={() => doStart(runFeasibilityOutline, "目录生成已启动")}
              >
                生成报告目录
              </Button>
            </Space>
          </>
        ) : (
          <Empty description="尚未生成资料分析" />
        )}
      </Card>
    </>
  );

  // ===== 步骤 2：报告目录（树形编辑器 + 参考知识库选材）=====
  const renderOutline = () => {
    const outline = report.outlineData?.outline || [];
    const countNodes = (nodes: FeasibilityOutlineNode[]): number =>
      nodes.reduce((acc, n) => acc + 1 + countNodes(n.children || []), 0);
    const editable = !runningTask;
    const successDocs = knowledgeDocs.filter((doc) => doc.status === "success");
    const treeData = outline.map(toOutlineTreeNode);
    const selectedLevel = outlineNode ? outlineLevel(outlineNode.id) : 0;
    const siblings = outlineNode
      ? findSiblings(outline, outlineNode.id)
      : [];
    const siblingIndex = outlineNode ? siblings.findIndex((n) => n.id === outlineNode.id) : -1;

    return (
      <>
        <Card title="目录生成配置" style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <Space wrap>
              <span>大纲模板：</span>
              <Select
                style={{ width: 340 }}
                value={report.outlineTemplate}
                disabled={!editable}
                onChange={async (v) => {
                  try {
                    await saveFeasibilityOutlineConfig(
                      v,
                      report.targetWords,
                      report.referenceDocumentIds,
                    );
                    message.success("大纲模板已切换");
                    await refresh();
                  } catch (e) {
                    message.error(extractApiError(e));
                  }
                }}
                options={Object.entries(templates).map(([k, v]) => ({ value: k, label: v.label }))}
              />
              <span>目标总字数：</span>
              <InputNumber
                min={1000}
                step={5000}
                value={report.targetWords}
                disabled={!editable}
                onChange={async (v) => {
                  try {
                    await saveFeasibilityOutlineConfig(
                      report.outlineTemplate,
                      v || 30000,
                      report.referenceDocumentIds,
                    );
                    await refresh();
                  } catch (e) {
                    message.error(extractApiError(e));
                  }
                }}
              />
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                loading={starting}
                disabled={!editable}
                onClick={() => doStart(runFeasibilityOutline, "目录生成已启动")}
              >
                {outline.length ? "AI 重新生成目录" : "AI 生成目录"}
              </Button>
            </Space>
            <Space wrap align="center">
              <Text>参考知识库：</Text>
              <Select
                mode="multiple"
                style={{ minWidth: 420, maxWidth: 720 }}
                placeholder="选择用于素材参考的知识库文档（不选则不使用素材）"
                value={report.referenceDocumentIds || []}
                disabled={!editable}
                onChange={(values) => void saveReferenceDocuments(values as number[])}
                optionFilterProp="label"
                options={successDocs.map((doc) => ({
                  value: doc.id,
                  label: `${doc.filename}（${doc.itemCount} 条素材）`,
                }))}
                notFoundContent={<Text type="secondary">知识库中暂无解析成功的文档</Text>}
              />
              <Text type="secondary">
                共 {successDocs.length} 份可用；素材会用于资料分析背景、目录细化、参数与正文写作。
              </Text>
            </Space>
          </Space>
        </Card>

        <Card
          title={`报告目录（${countNodes(outline)} 节 / ${leafCount} 个可写章节）`}
          extra={
            <Space>
              <Button
                size="small"
                icon={<PlusOutlined />}
                disabled={!editable || !outline.length}
                onClick={addRootNode}
              >
                新增一级章节
              </Button>
              <Button size="small" icon={<ReloadOutlined />} onClick={refresh}>
                刷新
              </Button>
            </Space>
          }
        >
          {outline.length === 0 ? (
            <Empty description="尚未生成目录，可先 AI 生成再人工微调" />
          ) : (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div style={{ flex: "1 1 320px", minWidth: 300, maxHeight: 480, overflow: "auto" }}>
                <Tree
                  blockNode
                  selectedKeys={selectedNodeId ? [selectedNodeId] : []}
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys.map(String))}
                  onSelect={(keys) => setSelectedNodeId(keys.length ? String(keys[0]) : "")}
                  treeData={treeData}
                />
              </div>
              <div style={{ flex: "1 1 380px", minWidth: 320 }}>
                {outlineNode ? (
                  <Space direction="vertical" style={{ width: "100%" }} size={8}>
                    <Space wrap size={6}>
                      <Tag color="blue">{outlineNode.id}</Tag>
                      <Text type="secondary">第 {selectedLevel} 级</Text>
                      {siblingIndex >= 0 && (
                        <Text type="secondary">
                          同层第 {siblingIndex + 1}/{siblings.length}
                        </Text>
                      )}
                      {(outlineNode.knowledgeItemIds?.length || 0) > 0 && (
                        <Tag color="purple">指定素材 {outlineNode.knowledgeItemIds?.length}</Tag>
                      )}
                    </Space>
                    <Input
                      addonBefore="章节标题"
                      value={editTitle}
                      disabled={!editable}
                      onChange={(e) => setEditTitle(e.target.value)}
                    />
                    <Input.TextArea
                      rows={4}
                      placeholder="写作重点（会作为 AI 撰写本节的依据）"
                      value={editDescription}
                      disabled={!editable}
                      onChange={(e) => setEditDescription(e.target.value)}
                    />
                    <Space wrap>
                      <Button
                        type="primary"
                        size="small"
                        loading={outlineSaving}
                        disabled={!editable}
                        onClick={saveNodeEdit}
                      >
                        保存修改
                      </Button>
                      <Button
                        size="small"
                        icon={<PlusOutlined />}
                        disabled={!editable || selectedLevel >= 3}
                        onClick={() => addChildNodeById(outlineNode.id)}
                      >
                        添加子章节
                      </Button>
                      <Tooltip title="同层上移（不影响已生成正文）">
                        <Button
                          size="small"
                          icon={<ArrowUpOutlined />}
                          disabled={!editable || siblingIndex <= 0}
                          onClick={() => moveNode(outlineNode.id, -1)}
                        />
                      </Tooltip>
                      <Tooltip title="同层下移（不影响已生成正文）">
                        <Button
                          size="small"
                          icon={<ArrowDownOutlined />}
                          disabled={!editable || siblingIndex < 0 || siblingIndex >= siblings.length - 1}
                          onClick={() => moveNode(outlineNode.id, 1)}
                        />
                      </Tooltip>
                      <Popconfirm
                        title="删除该章节？"
                        description="其子章节与已生成正文会一并删除"
                        onConfirm={() => removeNodeById(outlineNode.id)}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />} disabled={!editable}>
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                    <Alert
                      type="info"
                      showIcon
                      message="改标题保留正文；改写作重点、新增/删除章节会清空关键参数与受影响正文。"
                    />
                  </Space>
                ) : (
                  <Empty description="选择左侧章节后可编辑标题与写作重点" />
                )}
              </div>
            </div>
          )}
        </Card>
      </>
    );
  };

  // ===== 步骤 3：关键参数 =====
  const renderParameters = () => (
    <Card title="关键参数与编制口径" style={{ marginBottom: 16 }}>
      {report.keyParametersMarkdown ? (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="【待补充】项需要人工补齐后再生成正文；保存参数会清空已生成的正文（口径可能已变）。"
          />
          <TextArea
            rows={16}
            value={paramsDraft}
            onChange={(e) => setParamsDraft(e.target.value)}
            style={{ marginBottom: 12 }}
          />
          <Space>
            <Button
              type="primary"
              onClick={async () => {
                try {
                  await saveFeasibilityParameters(paramsDraft);
                  message.success("关键参数已保存");
                  await refresh();
                } catch (e) {
                  message.error(extractApiError(e));
                }
              }}
            >
              保存参数
            </Button>
            <Button
              icon={<PlayCircleOutlined />}
              loading={starting}
              onClick={() => doStart(runFeasibilityContent, "正文生成已启动")}
            >
              进入正文生成
            </Button>
          </Space>
        </>
      ) : (
        <Space direction="vertical">
          <Empty description="尚未生成关键参数" />
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={starting}
            onClick={() => doStart(runFeasibilityParameters, "关键参数生成已启动")}
          >
            AI 生成关键参数
          </Button>
        </Space>
      )}
    </Card>
  );

  // ===== 步骤 4：正文生成 =====
  const renderContent = () => {
    const active = leaves.find((l) => l.id === activeChapter);
    const editable = !runningTask;
    return (
      <>
        <Card style={{ marginBottom: 16 }}>
          <Space wrap size={24}>
            <Statistic title="可写章节" value={leafCount} />
            <Statistic title="已完成" value={doneCount} />
            <Statistic
              title="完成率"
              value={leafCount ? Math.round((doneCount / leafCount) * 100) : 0}
              suffix="%"
            />
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={starting}
              disabled={doneCount === leafCount && leafCount > 0}
              onClick={() =>
                doStart(
                  () => runFeasibilityContent(doneCount > 0, false),
                  "正文生成已启动（撰写 + 审校两阶段）",
                )
              }
            >
              {doneCount > 0 ? "续跑（补写未完成章节）" : "AI 生成全部正文"}
            </Button>
            <a href={getFeasibilityReportUrl()} target="_blank" rel="noopener noreferrer">
              <Button icon={<FileWordOutlined />}>导出 Word 报告</Button>
            </a>
          </Space>
        </Card>

        {runningTask?.type === "feasibility-content" && (
          <Card style={{ marginBottom: 16 }}>
            <Progress
              percent={runningTask.progress}
              status="active"
              format={() => `${TASK_LABELS[runningTask.type]} ${runningTask.progress}%`}
            />
            <Text type="secondary">
              当前阶段：{runningTask.stats?.phase === "human-writing" ? "自然化审校" : "逐章撰写"}
              {runningTask.stats?.current ? ` — 正在处理：${runningTask.stats.current}` : ""}
            </Text>
          </Card>
        )}

        <Card title="章节正文（点击章节查看/编辑）">
          {leafCount === 0 ? (
            <Empty description="目录为空" />
          ) : (
            <div style={{ maxHeight: 380, overflow: "auto", marginBottom: 12 }}>
              {leaves.map((leaf) => (
                <div
                  key={leaf.id}
                  onClick={() => {
                    setActiveChapter(leaf.id);
                    setChapterDraft(leaf.content || "");
                  }}
                  style={{
                    padding: "4px 8px",
                    cursor: "pointer",
                    background: leaf.id === activeChapter ? "#e6f4ff" : undefined,
                  }}
                >
                  {leaf.content?.trim() ? (
                    <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 6 }} />
                  ) : (
                    <Tag style={{ marginRight: 6 }}>待写</Tag>
                  )}
                  {(leaf.knowledgeItemIds?.length || 0) > 0 && (
                    <Tag color="purple" style={{ marginRight: 6 }}>
                      指定素材 {leaf.knowledgeItemIds?.length}
                    </Tag>
                  )}
                  <Text>{leaf.description}</Text>
                </div>
              ))}
            </div>
          )}
          {active && (
            <>
              <Paragraph type="secondary" style={{ marginBottom: 6 }}>
                编辑：{active.description}
              </Paragraph>
              <TextArea
                rows={12}
                value={chapterDraft}
                onChange={(e) => setChapterDraft(e.target.value)}
                style={{ marginBottom: 8 }}
              />
              <Button
                type="primary"
                onClick={async () => {
                  try {
                    await saveFeasibilityChapter(active.id, chapterDraft);
                    message.success("章节已保存");
                    await refresh();
                  } catch (e) {
                    message.error(extractApiError(e));
                  }
                }}
              >
                保存本章
              </Button>
            </>
          )}
        </Card>

        {active && (
          <Card
            title="本章知识库选材"
            style={{ marginTop: 16 }}
            extra={
              <Space>
                <Tag color={chapterKnowledge?.explicit ? "purple" : "default"}>
                  {chapterKnowledge?.explicit ? "人工指定" : "自动匹配"}
                </Tag>
                <Text type="secondary">
                  参考文档 {chapterKnowledge?.referenceDocumentCount ?? (report.referenceDocumentIds || []).length} 份 /
                  素材池 {chapterKnowledge?.itemPoolSize ?? 0} 条
                </Text>
              </Space>
            }
          >
            {!(report.referenceDocumentIds || []).length ? (
              <Alert
                type="info"
                showIcon
                message="尚未选择参考知识库：请到「报告目录」步骤勾选参考资料后，正文会自动吸收相关素材。"
              />
            ) : !chapterKnowledge?.candidates.length ? (
              <Empty description="未匹配到相关素材（可换更具体的章节标题或补充参考文档）" />
            ) : (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="勾选后保存即为本章人工指定素材；不勾选则按章节标题与写作重点自动匹配（取相关度最高的前 3 条）。"
                />
                <Space direction="vertical" style={{ width: "100%", maxHeight: 300, overflow: "auto" }}>
                  {chapterKnowledge.candidates.map((candidate) => (
                    <Checkbox
                      key={candidate.id}
                      checked={knowledgeDraft.includes(candidate.id)}
                      disabled={!editable}
                      onChange={(e) =>
                        setKnowledgeDraft((prev) =>
                          e.target.checked
                            ? [...prev, candidate.id]
                            : prev.filter((id) => id !== candidate.id),
                        )
                      }
                    >
                      <Text>{candidate.title}</Text>
                      <Tag style={{ marginLeft: 8 }}>相关度 {candidate.score}</Tag>
                      <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                        {candidate.documentName}
                        {candidate.summary ? `｜${candidate.summary}` : ""}
                      </Text>
                    </Checkbox>
                  ))}
                </Space>
                <Space style={{ marginTop: 12 }}>
                  <Button
                    type="primary"
                    disabled={!editable}
                    onClick={() => void saveChapterKnowledge(knowledgeDraft)}
                  >
                    保存本章选材
                  </Button>
                  <Button
                    disabled={!editable || !chapterKnowledge.explicit}
                    onClick={() => void saveChapterKnowledge([])}
                  >
                    恢复自动匹配
                  </Button>
                </Space>
              </>
            )}
          </Card>
        )}
      </>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Space>
            <FileWordOutlined style={{ fontSize: 22, color: "#1677ff" }} />
            <Typography.Title level={4} style={{ margin: 0 }}>
              可行性研究报告
            </Typography.Title>
            {info.projectName && <Tag color="blue">{info.projectName}</Tag>}
          </Space>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
            <Popconfirm
              title="确定要重置可研工作区吗？"
              description="将清除全部资料、分析、目录、参数与正文"
              onConfirm={async () => {
                try {
                  await clearFeasibilityWorkspace();
                  setAnalysisDraft("");
                  setParamsDraft("");
                  message.success("已重置");
                  await refresh();
                } catch (e) {
                  message.error(extractApiError(e));
                }
              }}
            >
              <Button danger>重置工作区</Button>
            </Popconfirm>
          </Space>
        </Space>
        <Steps
          size="small"
          current={stepIndex}
          style={{ marginTop: 16 }}
          items={STEP_ITEMS.map((s) => ({ key: s.key, title: s.title }))}
          onChange={(idx) => {
            // 允许回看已完成步骤查看内容；未到的步骤不可跳
            if (idx <= stepIndex + 1) {
              const target = STEP_ITEMS[idx];
              if (report && target) {
                setReport({ ...report, step: target.key as FeasibilityReportState["step"] });
              }
            }
          }}
        />
      </Card>

      {runningTask && (
        <Card style={{ marginBottom: 16 }}>
          <Progress
            percent={runningTask.progress}
            status="active"
            format={() => `${TASK_LABELS[runningTask.type]} ${runningTask.progress}%`}
          />
          <Text type="secondary">
            {runningTask.stats?.current ? `正在处理：${runningTask.stats.current}` : "任务运行中..."}
          </Text>
        </Card>
      )}

      {report["feasibility-content"]?.status === "error" && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="上一次任务失败"
          description={report["feasibility-content"]?.error}
        />
      )}

      <Descriptions size="small" column={4} style={{ marginBottom: 16 }}>
        <Descriptions.Item label="项目名称">{info.projectName || "未填写"}</Descriptions.Item>
        <Descriptions.Item label="总投资">{info.totalInvestment || "【待补充】"}</Descriptions.Item>
        <Descriptions.Item label="建设期">{info.constructionPeriodYears} 年</Descriptions.Item>
        <Descriptions.Item label="资料文件">{report.sourceFiles.length} 份</Descriptions.Item>
      </Descriptions>

      {report.step === "materials" && renderMaterials()}
      {report.step === "analysis" && renderAnalysis()}
      {report.step === "outline" && renderOutline()}
      {report.step === "parameters" && renderParameters()}
      {report.step === "content" && renderContent()}
    </div>
  );
}
