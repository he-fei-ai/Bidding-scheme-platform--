import { useEffect, useState, useRef, useMemo } from "react";
import {
  Button, Typography, Tabs, List, Spin, message, Modal, Input, Tag, Space, Tooltip, Divider,
} from "antd";
import {
  EditOutlined, RollbackOutlined, SaveOutlined, CodeOutlined, HistoryOutlined,
} from "@ant-design/icons";
import {
  fetchPrompts, updatePrompt, resetPrompt,
  fetchPromptVersions, rollbackPrompt,
} from "../api";
import type { PromptTemplate, PromptVersionMeta } from "../types";
import type { AxiosError } from "axios";

/** 从后端错误响应提取 detail 文本（变量校验/SSRF 等 400） */
function errDetail(e: unknown): string {
  const ax = e as AxiosError<{ detail?: unknown }>;
  const d = ax?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && "message" in d) return String((d as { message: unknown }).message);
  return "";
}

// 修复：原静态 CATEGORIES 只有 5 类，后端实际有 9 类（废标检查/知识库/行业树/共享规则等），
// 导致那些分类下的提示词在 UI 中不可见、不可编辑。改为动态分类。

// 占位符正则：{变量名} 或 __变量名__，与后端 _VARIABLE_PATTERN 保持一致
const VARIABLE_REGEX = /\{([A-Za-z_]\w{1,})\}|__([A-Z_][A-Z0-9_]*)__/g;

/** 从模板字符串中提取所有变量名（去重、排序）。与后端 extract_variables 对齐。 */
function extractPromptVariables(template: string): string[] {
  const set = new Set<string>();
  let m: RegExpExecArray | null;
  const re = new RegExp(VARIABLE_REGEX.source, "g");
  while ((m = re.exec(template)) !== null) {
    if (m[1]) set.add(m[1]);
    else if (m[2]) set.add(m[2]);
  }
  return Array.from(set).sort();
}
const CATEGORY_COLORS: Record<string, string> = {
  文件解析: "blue",
  目录生成: "green",
  正文生成: "orange",
  配图: "purple",
  工具: "default",
};

export default function PromptEditorPage() {
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");
  const isMountedRef = useRef(true);
  const promptsRef = useRef(prompts);
  const [versions, setVersions] = useState<PromptVersionMeta[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);

  // BUG-FRONT-VARS-01 修复：从当前编辑内容中实时提取占位符变量，
  // 与后端 _VARIABLE_PATTERN 保持一致。避免只显示后端首次返回的静态变量列表，
  // 用户在编辑器里新增/删除 {变量} 占位符后能立即看到变化。
  const liveEditVariables = useMemo(() => extractPromptVariables(editContent), [editContent]);

  // 动态生成分类：已知分类保持固定顺序，未知分类按出现顺序追加
  const categories = useMemo(() => {
    const known = Object.keys(CATEGORY_COLORS);
    const seen = new Set<string>();
    prompts.forEach((p) => { if (p.category) seen.add(p.category); });
    const ordered = known.filter((k) => seen.has(k));
    const extra = [...seen].filter((k) => !known.includes(k));
    return [...ordered, ...extra].map((key) => ({
      key,
      label: key,
      color: CATEGORY_COLORS[key] || "cyan",
    }));
  }, [prompts]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // 保持 ref 与最新 state 同步
  useEffect(() => {
    promptsRef.current = prompts;
  }, [prompts]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPrompts()
      .then((data) => {
        if (cancelled) return;
        setPrompts(data);
      })
      .catch(() => {
        if (cancelled) return;
        message.error("加载提示词失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const openEditor = (item: PromptTemplate) => {
    setEditingKey(item.key);
    setEditContent(item.content);
    loadVersions(item.key);
  };

  const loadVersions = async (key: string) => {
    setVersionsLoading(true);
    setVersions([]);
    try {
      const data = await fetchPromptVersions(key);
      setVersions(data);
    } catch {
      /* 历史版本加载失败不阻断编辑 */
    } finally {
      setVersionsLoading(false);
    }
  };

  const handleSave = async (key: string) => {
    try {
      await updatePrompt(key, editContent);
      message.success("提示词已更新");
      setPrompts((prev) =>
        prev.map((p) => (p.key === key ? { ...p, content: editContent } : p)),
      );
      setEditingKey(null);
      loadVersions(key);
    } catch (e) {
      const detail = errDetail(e);
      if (detail) {
        // 变量校验/共享规则引用等 400：提示并允许强制保存
        Modal.confirm({
          title: "保存被校验拦截",
          content: detail + "\n\n是否忽略校验强制保存？",
          okText: "强制保存",
          cancelText: "返回修改",
          onOk: async () => {
            try {
              await updatePrompt(key, editContent, true);
              message.success("已强制保存");
              setPrompts((prev) =>
                prev.map((p) => (p.key === key ? { ...p, content: editContent } : p)),
              );
              setEditingKey(null);
            } catch {
              message.error("强制保存失败");
            }
          },
        });
      } else {
        message.error("保存失败");
      }
    }
  };

  const handleRollback = async (key: string, versionId: number) => {
    try {
      await rollbackPrompt(key, versionId);
      message.success("已回滚到所选历史版本");
      // 回滚后重新拉取全量（内容变化），并刷新历史
      const data = await fetchPrompts();
      setPrompts(data);
      const cur = data.find((p) => p.key === key);
      if (editingKey === key && cur) setEditContent(cur.content);
      loadVersions(key);
    } catch {
      message.error("回滚失败");
    }
  };

  const handleReset = async (key: string) => {
    try {
      await resetPrompt(key);
      message.success("已重置为默认值");
      setPrompts((prev) =>
        prev.map((p) => (p.key === key ? { ...p, content: p.default_content } : p)),
      );
      if (editingKey === key) {
        // 使用 ref 获取最新的 prompts
        const latestPrompt = promptsRef.current.find((p) => p.key === key);
        setEditContent(latestPrompt?.default_content || "");
      }
    } catch {
      message.error("重置失败");
    }
  };

  if (loading) return <Spin size="large" style={{ display: "block", marginTop: 80, textAlign: "center" }} />;

  const categoryPrompts = (cat: string) =>
    prompts.filter((p) => p.category === cat);

  const tabItems = categories.map((cat) => ({
    key: cat.key,
    label: (
      <span>
        <Tag color={cat.color} style={{ marginRight: 4 }}>{cat.label}</Tag>
        ({categoryPrompts(cat.key).length})
      </span>
    ),
    children: (
      <List
        dataSource={categoryPrompts(cat.key)}
        renderItem={(item) => (
          <List.Item
            actions={[
              <Tooltip title="编辑" key="edit">
                <Button
                  type="link"
                  icon={<EditOutlined />}
                  onClick={() => openEditor(item)}
                />
              </Tooltip>,
              <Tooltip title="重置为默认值" key="reset">
                <Button
                  type="link"
                  danger
                  icon={<RollbackOutlined />}
                  onClick={() => handleReset(item.key)}
                />
              </Tooltip>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <CodeOutlined />
                  <Typography.Text code style={{ fontSize: 12 }}>{item.key}</Typography.Text>
                  <Typography.Text strong>{item.label}</Typography.Text>
                  {item.content !== item.default_content && (
                    <Tag color="orange" style={{ fontSize: 11 }}>已修改</Tag>
                  )}
                </Space>
              }
              description={
                <Typography.Paragraph
                  ellipsis={{ rows: 2 }}
                  style={{ margin: 0, fontSize: 12, color: "#888" }}
                >
                  {item.content.slice(0, 200)}
                </Typography.Paragraph>
              }
            />
          </List.Item>
        )}
      />
    ),
  }));

  const currentPrompt = prompts.find((p) => p.key === editingKey);

  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      <Typography.Title level={4}>
        <CodeOutlined /> 提示词管理
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        自由编辑各模块调用的 AI 提示词内容。修改后保存将立即生效，无需重启服务。
      </Typography.Paragraph>

      <Tabs items={tabItems} />

      <Modal
        title={
          <Space>
            <CodeOutlined />
            <span>编辑提示词</span>
            {currentPrompt && (
              <>
                <Typography.Text code>{currentPrompt.key}</Typography.Text>
                <Typography.Text type="secondary">— {currentPrompt.label}</Typography.Text>
              </>
            )}
          </Space>
        }
        open={!!editingKey}
        onCancel={() => setEditingKey(null)}
        width={960}
        footer={
          <Space>
            <Button
              danger
              icon={<RollbackOutlined />}
              onClick={() => editingKey && handleReset(editingKey)}
            >
              重置为默认
            </Button>
            <Button onClick={() => setEditingKey(null)}>取消</Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={() => editingKey && handleSave(editingKey)}
            >
              保存
            </Button>
          </Space>
        }
        styles={{ body: { maxHeight: "65vh", overflow: "auto" } }}
      >
        {/* 实时提取当前编辑内容中的变量，与后端 _VARIABLE_PATTERN 对齐 */}
        {liveEditVariables.length > 0 && (
          <Space wrap style={{ display: "flex", marginBottom: 10 }}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              占位符变量（{liveEditVariables.length}，实时提取）：
            </Typography.Text>
            {liveEditVariables.map((v) => (
              <Tag key={v} color="blue" style={{ fontFamily: "monospace" }}>
                {v.startsWith("_") ? `__${v}__` : `{${v}}`}
              </Tag>
            ))}
          </Space>
        )}
        <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
          提示词中可使用 <Tag>{"{变量名}"}</Tag> 格式的占位符，运行时会自动替换为实际内容。
        </Typography.Text>
        <Input.TextArea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          rows={20}
          style={{ fontFamily: "monospace", fontSize: 13 }}
        />

        {/* 历史版本（版本控制与回滚） */}
        <Divider orientation="left" style={{ margin: "16px 0 8px" }}>
          <Space><HistoryOutlined /><span>历史版本</span></Space>
        </Divider>
        {versionsLoading ? (
          <Spin size="small" />
        ) : versions.length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            暂无历史版本。编辑保存后，被覆盖的旧内容会自动归档，可在此回滚。
          </Typography.Text>
        ) : (
          <List
            size="small"
            dataSource={versions}
            renderItem={(v) => (
              <List.Item
                actions={[
                  <Button
                    key="rb"
                    type="link"
                    size="small"
                    icon={<RollbackOutlined />}
                    onClick={() =>
                      Modal.confirm({
                        title: "回滚到该版本？",
                        content: "当前内容会先归档，可用同一入口再次回滚。",
                        okText: "回滚",
                        onOk: () => handleRollback(v.key, v.id),
                      })
                    }
                  >
                    回滚
                  </Button>,
                ]}
              >
                <Space size={6} wrap>
                  <Tag>{v.source === "edit" ? "编辑" : v.source === "reset" ? "重置" : "回滚"}</Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {v.created_at}
                  </Typography.Text>
                  {v.changed_by && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      by {v.changed_by}
                    </Typography.Text>
                  )}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {v.content_length} 字
                  </Typography.Text>
                </Space>
              </List.Item>
            )}
          />
        )}
      </Modal>
    </div>
  );
}