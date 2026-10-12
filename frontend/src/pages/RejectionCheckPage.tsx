import { useEffect, useRef, useState } from "react";
import { Button, Card, Input, List, Modal, Space, Spin, Tag, Tooltip, Typography, message, Alert, Progress, Popconfirm, Table, Checkbox, Row, Col, Drawer } from "antd";
import {
  SafetyCertificateOutlined,
  PlusOutlined,
  DeleteOutlined,
  ReloadOutlined,
  FileTextOutlined,
  DownloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  EllipsisOutlined,
  FileExcelOutlined,
  EditOutlined,
} from "@ant-design/icons";
import {
  startRejectionCheck,
  getRejectionStatus,
  getRejectionResult,
  listRejectionTasks,
  markFalsePositive,
  retryRejectionDimension,
  importRejectionFromProject,
  batchDeleteRejectionTasks,
  batchMarkFalsePositive,
  getRejectionTasksByProject,
  getKnowledgeSuggestions,
  getRejectionReportUrl,
  applySpellingFix,
} from "../api";
import { extractApiError } from "../utils";
import type { RejectionCheckTask, RejectionCheckItem, RejectionResultItem, BidDocument } from "../types";

const dimensionLabels: Record<string, string> = {
  rejection: "废标条款",
  spelling: "错别字",
  logic: "逻辑一致性",
};

const severityConfig: Record<string, { color: string; icon: string }> = {
  critical: { color: "red", icon: "!" },
  warning: { color: "orange", icon: "?" },
  info: { color: "blue", icon: "i" },
};

export default function RejectionCheckPage() {
  const [tenderText, setTenderText] = useState("");
  const [bidDocs, setBidDocs] = useState<BidDocument[]>([]);
  const [checkItems, setCheckItems] = useState<RejectionCheckItem[]>([]);
  const [enabledDimensions, setEnabledDimensions] = useState(["rejection", "spelling", "logic"]);
  const [task, setTask] = useState<RejectionCheckTask | null>(null);
  const [status, setStatus] = useState<{
    id?: number; status: string; progress: number; progress_message: string; error: string;
  } | null>(null);
  const pollingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const isMountedRef = useRef(true);
  const [newItemDesc, setNewItemDesc] = useState("");
  const [newItemCategory, setNewItemCategory] = useState("rejection");
  const [showAddItem, setShowAddItem] = useState(false);
  const [selectedProjectId, setSelectedProjectId] = useState<number | undefined>();
  const [tasks, setTasks] = useState<RejectionCheckTask[]>([]);
  const [selectedTaskIds, setSelectedTaskIds] = useState<number[]>([]);
  const [showTaskList, setShowTaskList] = useState(false);
  const [knowledgeSuggestions, setKnowledgeSuggestions] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const loadRecentTask = async () => {
    try {
      const tasks = await listRejectionTasks(selectedProjectId, 1);
      if (tasks.length > 0) {
        setTask(tasks[0]);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    loadRecentTask();
    if (selectedProjectId) {
      loadProjectTasks(selectedProjectId);
      // 加载知识库建议的检查项
      loadKnowledgeSuggestions(selectedProjectId);
    }
  }, [selectedProjectId]);

  useEffect(() => {
    if (task && task.status === "running") {
      const timer = setInterval(async () => {
        try {
          if (!isMountedRef.current) return;
          const s = await getRejectionStatus(task.id);
          setStatus(s);
          if (s.status !== "running") {
            clearInterval(timer);
            pollingTimerRef.current = null;
            if (s.status === "completed") {
              const result = await getRejectionResult(task.id);
              setTask(result);
              message.success("废标检查完成");
            } else if (s.status === "failed") {
              message.error(s.error || "废标检查失败");
            }
          }
        } catch {
          clearInterval(timer);
          pollingTimerRef.current = null;
        }
      }, 2000);
      pollingTimerRef.current = timer;
    }
    return () => {
      if (pollingTimerRef.current) clearInterval(pollingTimerRef.current);
    };
  }, [task?.id, task?.status]);

  const handleStartCheck = async () => {
    if (!tenderText.trim()) {
      message.warning("请输入招标文件内容");
      return;
    }
    if (bidDocs.length === 0) {
      message.warning("请至少添加一份投标文件");
      return;
    }
    setLoading(true);
    try {
      // Combine check items with knowledge suggestions, then clear suggestions
      const allCheckItems = [...checkItems, ...knowledgeSuggestions];
      const res = await startRejectionCheck({
        project_id: selectedProjectId || null,
        tender_text: tenderText,
        bid_documents: bidDocs,
        check_items: allCheckItems,
        enabled_dimensions: enabledDimensions,
      });
      // Clear knowledge suggestions after starting
      setKnowledgeSuggestions([]);
      setTask({
        id: res.task_id,
        status: "running",
        progress: 0,
        progress_message: "检查已启动",
        enabled_dimensions: enabledDimensions,
        bid_documents: bidDocs,
        check_items: allCheckItems,
        results: { rejection: [], spelling: [], logic: [] },
        false_positives: [],
        error: "",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        project_id: selectedProjectId || null,
      });
      setStatus({ status: "running", progress: 0, progress_message: "检查已启动", error: "" });
      message.success("废标检查已启动");
    } catch (e: any) {
      // BUG-FIX 2026-09-15：409 冲突已由响应拦截器用后端中文提示单独弹窗，
      // 这里不再重复提示，避免双重 toast。
      if (e?.response?.status !== 409) {
        message.error(`启动检查失败：${extractApiError(e)}`);
      }
    } finally {
      setLoading(false);
    }
  };

  const toggleTaskList = () => {
    setShowTaskList(!showTaskList);
  };

  const loadProjectTasks = async (projectId: number) => {
    try {
      const res = await getRejectionTasksByProject(projectId, 50);
      if (isMountedRef.current) {
        setTasks(res.items);
      }
    } catch (e) {
      console.error("加载任务列表失败:", e);
    }
  };

  const loadKnowledgeSuggestions = async (projectId: number) => {
    try {
      setLoading(true);
      const res = await getKnowledgeSuggestions(projectId);
      if (res.status === "success" && res.suggestions && res.suggestions.length > 0) {
        setKnowledgeSuggestions(
          res.suggestions.map((s: any) => ({
            id: `kb-${s.id}`,
            description: s.content || s.title,
            category: "废标",
            source: "知识库",
          }))
        );
      }
    } catch (e) {
      console.error("加载知识库建议失败:", e);
    } finally {
      setLoading(false);
    }
  };

  const handleImportFromProject = async (projectId: number) => {
    try {
      const res = await importRejectionFromProject(projectId);
      setTenderText(res.tender_text);
      setBidDocs([{ name: "投标文件", text: res.bid_text }]);
      setSelectedProjectId(projectId);
      loadProjectTasks(projectId);
      loadKnowledgeSuggestions(projectId);
      message.success("已从项目导入招标文件和投标文件");
    } catch (e) {
      message.error("从项目导入失败");
    }
  };

  const handleBulkDelete = async () => {
    if (selectedTaskIds.length === 0) {
      message.warning("请选择要删除的任务");
      return;
    }

    const confirmResult = Modal.confirm({
      title: "确认删除",
      content: `确定要删除 ${selectedTaskIds.length} 个任务吗？此操作不可恢复！`,
      okText: "删除",
      cancelText: "取消",
      okType: "danger",
    });

    try {
      const res = await batchDeleteRejectionTasks(selectedTaskIds);
      if (res.status === "success" && res.success > 0) {
        message.success(`成功删除 ${res.success} 个任务`);
        // 重新加载任务列表
        if (selectedProjectId) {
          await loadProjectTasks(selectedProjectId);
        } else {
          const allTasks = await listRejectionTasks();
          setTasks(allTasks.filter(t => t.status !== 'completed'));
        }
        setSelectedTaskIds([]);
      } else {
        message.error(`删除失败: ${res.message || "未知错误"}`);
      }
    } catch (e) {
      message.error("批量删除失败");
    } finally {
      // Modal automatically closes on click, no need to manually close
    }
  };

  const handleBatchMarkFalsePositive = async () => {
    if (!task) {
      message.warning("请选择一个已完成的任务");
      return;
    }
    if (task.results?.rejection?.length === 0 && task.results?.spelling?.length === 0 && task.results?.logic?.length === 0) {
      message.warning("该任务没有可标记的项目");
      return;
    }

    const allItemIds = [
      ...(task.results?.rejection?.map((r: any) => r.id) || []),
      ...(task.results?.spelling?.map((s: any) => s.id) || []),
      ...(task.results?.logic?.map((l: any) => l.id) || []),
    ];

    if (allItemIds.length === 0) {
      message.warning("没有找到可标记的项目");
      return;
    }

    Modal.confirm({
      title: "批量标记误报",
      content: `确定要将所有 ${allItemIds.length} 个项目标记为误报吗？这些项目将在报告中被隐藏。`,
      okText: "标记",
      cancelText: "取消",
      onOk: async () => {
        try {
          const res = await batchMarkFalsePositive(task.id, allItemIds);
          if (res.status === "success") {
            message.success(`已标记 ${res.false_positives.length} 个项目为误报`);
            // 更新任务结果
            const updatedTask = { ...task, false_positives: res.false_positives };
            setTask(updatedTask);
          }
        } catch (e) {
          message.error("批量标记失败");
        }
      },
    });
  };

  const handleAddItem = () => {
    if (!newItemDesc.trim()) {
      message.warning("请输入检查项描述");
      return;
    }
    const item: RejectionCheckItem = {
      id: `custom-${Date.now()}`,
      description: newItemDesc.trim(),
      category: newItemCategory,
    };
    setCheckItems([...checkItems, item]);
    setNewItemDesc("");
    setShowAddItem(false);
  };

  const handleRemoveItem = (id: string) => {
    setCheckItems(checkItems.filter((item) => item.id !== id));
  };

  const handleToggleDimension = (dim: string) => {
    setEnabledDimensions((prev) =>
      prev.includes(dim) ? prev.filter((d) => d !== dim) : [...prev, dim],
    );
  };

  const handleMarkFalsePositive = async (itemId: string) => {
    if (!task) return;
    try {
      await markFalsePositive(task.id, itemId);
      message.success("已标记为误报");
      const result = await getRejectionResult(task.id);
      setTask(result);
    } catch {
      message.error("操作失败");
    }
  };

  const handleRetryDimension = async (dimension: string) => {
    if (!task) return;
    try {
      await retryRejectionDimension(task.id, dimension);
      message.success(`${dimensionLabels[dimension] || dimension} 检查已重新执行`);
    } catch {
      message.error("重试失败");
    }
  };

  // ENHANCE 2026-09-15：错别字一键修正。
  // 后端只对「已通过原文核验」的条目生效（按 position 精确下标替换），
  // 未核验的一律拒绝并回传原因，避免把错位置的内容改坏。
  const handleApplySpelling = async (itemIds: string[], writebackProject = false) => {
    if (!task?.id || itemIds.length === 0) return;
    try {
      const r = await applySpellingFix({
        task_id: task.id,
        item_ids: itemIds,
        writeback_project: writebackProject,
      });
      if (r.applied_count > 0) {
        message.success(
          `已修正 ${r.applied_count} 处错别字${writebackProject ? "，并写回项目正文" : ""}`,
        );
      }
      if (r.skipped_count > 0) {
        message.warning(`${r.skipped_count} 处未修正：${r.skipped[0]?.reason || "请查看明细"}`);
      }
      const latest = await getRejectionResult(task.id);
      setTask(latest);
    } catch (e: any) {
      message.error(extractApiError(e));
    }
  };

  const renderResults = () => {
    if (!task) return null;
    const allResults = [
      ...(task.results.rejection || []),
      ...(task.results.spelling || []),
      ...(task.results.logic || []),
    ];

    return (
      <div>
        {Object.entries(dimensionLabels).map(([dim, label]) => {
          const dimResults = task.results[dim as keyof typeof task.results] || [];
          return (
            <Card key={dim} title={label} style={{ marginBottom: 16 }}>
              {dimResults.length === 0 ? (
                <Typography.Text type="secondary">暂无结果</Typography.Text>
              ) : (
                <List
                  dataSource={dimResults}
                  renderItem={(item: any) => (
                    <List.Item
                      actions={[
                        <Popconfirm
                          key="fp"
                          title="标记为误报？"
                          onConfirm={() => handleMarkFalsePositive(item.id)}
                        >
                          <Button size="small" type="link">
                            标记误报
                          </Button>
                        </Popconfirm>,
                        <Button key="retry" size="small" type="link" onClick={() => handleRetryDimension(dim)}>
                          重试
                        </Button>,
                        // ENHANCE 2026-09-15：错别字一键修正（仅已核验且未修正的条目）
                        dim === "spelling" && item.original && item.corrected && !item.applied && (
                          <Popconfirm
                            key="fix"
                            title={`把「${item.original}」改为「${item.corrected}」？`}
                            description="将按已核验的位置精确替换任务内的投标文件文本"
                            onConfirm={() => handleApplySpelling([item.id])}
                          >
                            <Button size="small" type="link">
                              一键修正
                            </Button>
                          </Popconfirm>
                        ),
                      ].filter(Boolean)}
                    >
                      <List.Item.Meta
                        title={
                          <Space wrap>
                            <Tag color={severityConfig[item.severity]?.color}>
                              {item.severity}
                            </Tag>
                            {item.description}
                            {item.validated === true && (
                              <Tooltip
                                title={
                                  item.line_hint
                                    ? `证据摘录已在投标文件原文中定位：${item.line_hint}`
                                    : "证据摘录已在投标文件原文中定位"
                                }
                              >
                                <Tag color="green">证据已定位</Tag>
                              </Tooltip>
                            )}
                            {item.validated === false && (
                              <Tooltip title="摘录未能在投标文件原文中定位，可能是模型改写或误报，建议人工重点复核">
                                <Tag color="orange">证据待核验</Tag>
                              </Tooltip>
                            )}
                            {item.code_matched === false && (
                              <Tooltip title="该条目未对应到任何检查项编号，可能是模型自行扩大的检查范围">
                                <Tag>未对应检查项</Tag>
                              </Tooltip>
                            )}
                            {Number(item.merged_count) > 1 && (
                              <Tag color="blue">已合并 {item.merged_count} 条</Tag>
                            )}
                            {/* ENHANCE 2026-09-15：跨分块「疑似缺失」复核结论 */}
                            {item.missing_state && (
                              <Tooltip title={item.missing_state_reason || ""}>
                                <Tag
                                  color={
                                    item.missing_state === "resolved"
                                      ? "default"
                                      : item.missing_state === "confirmed"
                                        ? "red"
                                        : "orange"
                                  }
                                >
                                  {item.missing_state === "resolved"
                                    ? "缺失已排除"
                                    : item.missing_state === "confirmed"
                                      ? "缺失成立"
                                      : "缺失待确认"}
                                </Tag>
                              </Tooltip>
                            )}
                            {item.applied && <Tag color="green">已修正</Tag>}
                          </Space>
                        }
                        description={
                          <div>
                            {item.quote && (
                              <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                                引用原文：{item.quote}
                              </Typography.Paragraph>
                            )}
                            {item.excerpt && (
                              <Typography.Paragraph type="secondary">
                                {item.line_hint && <Typography.Text code>{item.line_hint}</Typography.Text>}
                                {" 附近："}
                                {item.excerpt}
                              </Typography.Paragraph>
                            )}
                            {item.suggestion && (
                              <Typography.Text type="success">
                                建议：{item.suggestion}
                              </Typography.Text>
                            )}
                            {item.location && (
                              <div>
                                <Typography.Text type="secondary">
                                  位置：{item.location}
                                </Typography.Text>
                              </div>
                            )}
                          </div>
                        }
                      />
                    </List.Item>
                  )}
                />
              )}
            </Card>
          );
        })}
      </div>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Typography.Title level={4}>
        <SafetyCertificateOutlined /> 废标项检查
      </Typography.Title>

      {/* 任务管理栏 */}
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Button
            icon={<EllipsisOutlined />}
            onClick={toggleTaskList}
            type="dashed"
          >
            {showTaskList ? '收起任务列表' : '查看历史任务'}
          </Button>
        </Col>
        {selectedProjectId && (
          <Col>
            <span style={{ marginRight: 8, color: '#888' }}>
              项目 {selectedProjectId}:
            </span>
            <Button size="small" onClick={() => loadProjectTasks(selectedProjectId!)}>
              刷新
            </Button>
          </Col>
        )}
      </Row>

      {/* 任务抽屉 */}
      {showTaskList && (
        <Drawer
          title={`历史任务${selectedProjectId ? `（项目 ${selectedProjectId}）` : ''}`}
          placement="right"
          closable
          onClose={() => setShowTaskList(false)}
          open={showTaskList}
          width={800}
        >
          {tasks.length === 0 ? (
            <Typography.Text type="secondary">暂无任务</Typography.Text>
          ) : (
            <>
              <Row gutter={[16, 0]} style={{ marginBottom: 16 }}>
                <Col>
                  <Checkbox
                    indeterminate={selectedTaskIds.length > 0 && selectedTaskIds.length < tasks.length}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedTaskIds(tasks.map(t => t.id));
                      } else {
                        setSelectedTaskIds([]);
                      }
                    }}
                  >
                    全选 ({tasks.length})
                  </Checkbox>
                  {selectedTaskIds.length > 0 && (
                    <Button
                      type="link"
                      onClick={handleBulkDelete}
                      style={{ marginLeft: 8 }}
                    >
                      <DeleteOutlined /> 批量删除 ({selectedTaskIds.length})
                    </Button>
                  )}
                </Col>
              </Row>
              <Table
                dataSource={tasks}
                rowKey="id"
                onRow={(record) => ({
                  onClick: () => {
                    setSelectedTaskIds(prev =>
                      prev.includes(record.id)
                        ? prev.filter(id => id !== record.id)
                        : [...prev, record.id]
                    );
                  },
                })}
                columns={[
                  {
                    title: 'ID',
                    dataIndex: 'key',
                    width: 80,
                  },
                  {
                    title: '项目ID',
                    dataIndex: 'project_id',
                    width: 120,
                  },
                  {
                    title: '状态',
                    dataIndex: 'status',
                    width: 100,
                    render: (status) => (
                      <Tag color={status === 'running' ? 'processing' : status === 'completed' ? 'green' : status === 'failed' ? 'red' : 'default'}>
                        {status === 'running' ? '运行中' : status === 'completed' ? '已完成' : status === 'failed' ? '失败' : '待处理'}
                      </Tag>
                    ),
                  },
                  {
                    title: '进度',
                    dataIndex: 'progress',
                    width: 100,
                    render: (progress) => `${progress}%`,
                  },
                  {
                    title: '创建时间',
                    dataIndex: 'created_at',
                    width: 150,
                    render: (text) => text?.substring(0, 19),
                  },
                  {
                    title: '操作',
                    dataIndex: 'action',
                    width: 150,
                    render: (_, record) => (
                      <Space>
                        {record.status === 'completed' && (
                          <Button
                            size="small"
                            type="link"
                            onClick={() => setTask(record)}
                          >
                            查看详情
                          </Button>
                        )}
                        <Button
                          size="small"
                          type="link"
                          danger
                          onClick={() => batchDeleteRejectionTasks([record.id]).then(() => loadProjectTasks(selectedProjectId || 0))}
                        >
                          删除
                        </Button>
                      </Space>
                    ),
                  },
                ]}
                pagination={{ pageSize: 10 }}
              />
            </>
          )}
        </Drawer>
      )}

      {/* 输入区域 */}
      <Card title="招标文件" style={{ marginBottom: 16 }}>
        <Input.TextArea
          rows={6}
          placeholder="请粘贴招标文件内容（或从项目导入）"
          value={tenderText}
          onChange={(e) => setTenderText(e.target.value)}
        />
        <div style={{ marginTop: 8 }}>
          <Input
            style={{ width: 200, marginRight: 8 }}
            placeholder="项目ID"
            type="number"
            value={selectedProjectId}
            onChange={(e) => setSelectedProjectId(e.target.value ? Number(e.target.value) : undefined)}
          />
          <Button
            icon={<FileTextOutlined />}
            onClick={() => selectedProjectId && handleImportFromProject(selectedProjectId)}
            disabled={!selectedProjectId}
          >
            从项目导入
          </Button>
        </div>
      </Card>

      {/* 维度选择 */}
      <Card title="检查维度" style={{ marginBottom: 16 }}>
        <Space>
          {Object.entries(dimensionLabels).map(([dim, label]) => (
            <Tag.CheckableTag
              key={dim}
              checked={enabledDimensions.includes(dim)}
              onChange={() => handleToggleDimension(dim)}
            >
              {label}
            </Tag.CheckableTag>
          ))}
        </Space>
      </Card>

      {/* 检查项 */}
      <Card
        title="检查项"
        style={{ marginBottom: 16 }}
        extra={
          <Space>
            <Button size="small" icon={<PlusOutlined />} onClick={() => setShowAddItem(true)}>
              添加
            </Button>
            {knowledgeSuggestions.length > 0 && (
               <Button size="small" type="dashed" onClick={() => {
                 setCheckItems([...checkItems, ...knowledgeSuggestions]);
                 setKnowledgeSuggestions([]);
               }}>
                 加载知识库建议 ({knowledgeSuggestions.length})
               </Button>
             )}
          </Space>
        }
      >
        {checkItems.length > 0 ? (
          <List
            size="small"
            dataSource={checkItems}
            renderItem={(item: RejectionCheckItem) => (
              <List.Item
                actions={[
                  <Button
                    key="del"
                    size="small"
                    type="link"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => handleRemoveItem(item.id)}
                  />,
                ]}
              >
                <Tag>{dimensionLabels[item.category] || item.category}</Tag>
                {item.description}
              </List.Item>
            )}
          />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }}>
            {knowledgeSuggestions.length > 0 && (
              <Alert
                type="info"
                message="知识库发现相关检查项"
                description={`有 ${knowledgeSuggestions.length} 条建议可从知识库加载`}
                showIcon
                style={{ marginBottom: 12 }}
              />
            )}
            <Typography.Text type="secondary">暂无自定义检查项，AI 将自动识别废标条款</Typography.Text>
          </Space>
        )}
      </Card>

      {/* 操作按钮 */}
      <Space style={{ marginBottom: 24 }} direction="vertical">
        <Button
          type="primary"
          icon={<SafetyCertificateOutlined />}
          onClick={handleStartCheck}
          loading={loading}
          size="large"
        >
          开始检查
        </Button>
        {task && task.status === "completed" && (
          <Button icon={<ReloadOutlined />} onClick={handleStartCheck} loading={loading}>
            重新检查
          </Button>
        )}
        {task?.status === "completed" && (
          <Row gutter={[16, 0]} style={{ marginTop: 8 }}>
            <Col>
              <Button
                icon={<CheckCircleOutlined />}
                onClick={handleBatchMarkFalsePositive}
                type="dashed"
              >
                批量标记所有误报
              </Button>
            </Col>
            <Col>
              <Button
                icon={<EditOutlined />}
                type="dashed"
                onClick={() => {
                  const pending = (task.results.spelling || []).filter(
                    (item: any) => item.original && item.corrected && !item.applied,
                  );
                  if (pending.length === 0) {
                    message.info("没有可一键修正的错别字条目");
                    return;
                  }
                  handleApplySpelling(pending.map((item: any) => item.id));
                }}
              >
                一键修正全部错别字
              </Button>
            </Col>
            <Col>
              <a href={getRejectionReportUrl(task.id, "docx")} target="_blank" rel="noopener noreferrer">
                <Button icon={<DownloadOutlined />}>下载报告</Button>
              </a>
            </Col>
            {/* ENHANCE 2026-09-15：Excel 报告（检查概览/废标项/错别字/逻辑问题 四 Sheet），
                适合按风险等级筛选复核与整改台账流转 */}
            <Col>
              <a href={getRejectionReportUrl(task.id, "xlsx")} target="_blank" rel="noopener noreferrer">
                <Button icon={<FileExcelOutlined />}>下载 Excel 报告</Button>
              </a>
            </Col>
          </Row>
        )}
      </Space>

      {/* 进度 */}
      {status?.status === "running" && (
        <Card style={{ marginBottom: 24 }}>
          <Space>
            <Spin />
            <Typography.Text>{status.progress_message || "检查中..."}</Typography.Text>
          </Space>
          {status.progress > 0 && <Progress percent={status.progress} style={{ marginTop: 8 }} />}
        </Card>
      )}

      {/* 结果 */}
      {task?.status === "completed" && renderResults()}

      {task?.status === "failed" && (
        <Alert type="error" message="检查失败" description={task.error || '无错误信息'} showIcon />
      )}

      {/* 添加检查项弹窗 */}
      <Modal
        title="添加检查项"
        open={showAddItem}
        onOk={handleAddItem}
        onCancel={() => setShowAddItem(false)}
        okText="添加"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input.TextArea
            rows={3}
            placeholder="请输入检查项描述"
            value={newItemDesc}
            onChange={(e) => setNewItemDesc(e.target.value)}
          />
          <Space>
            {Object.entries(dimensionLabels).map(([dim, label]) => (
              <Tag.CheckableTag
                key={dim}
                checked={newItemCategory === dim}
                onChange={() => setNewItemCategory(dim)}
              >
                {label}
              </Tag.CheckableTag>
            ))}
          </Space>
        </Space>
      </Modal>
    </div>
  );
}