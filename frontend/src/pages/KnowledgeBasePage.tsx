import { useEffect, useState } from "react";
import { Button, Card, Empty, Input, List, Modal, Popconfirm, Select, Space, Spin, Tag, Typography, message, Upload } from "antd";
import { DatabaseOutlined, DeleteOutlined, SearchOutlined, UploadOutlined, FileTextOutlined } from "@ant-design/icons";
import {
  searchKnowledgeItems,
  listKnowledgeDocuments,
  uploadKnowledgeDocument,
  deleteKnowledgeDocument,
  deleteKnowledgeItem,
  getKnowledgeStats,
  listKnowledgeIndustries,
  listKnowledgeTags,
} from "../api";
import { extractApiError } from "../utils";
import type { KnowledgeItem, KnowledgeDocument } from "../types";

const statusColors: Record<string, string> = {
  pending: "default",
  copying: "processing",
  converting: "processing",
  extracting: "processing",
  matching: "processing",
  recovering: "processing",
  saving: "processing",
  success: "success",
  error: "error",
};

export default function KnowledgeBasePage() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [searchIndustry, setSearchIndustry] = useState("");
  const [searchTag, setSearchTag] = useState("");
  const [total, setTotal] = useState(0);
  const [selectedItem, setSelectedItem] = useState<KnowledgeItem | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [stats, setStats] = useState<{ total_documents: number; success_documents: number; total_items: number; total_usage: number } | null>(null);
  const [industries, setIndustries] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [docFilter, setDocFilter] = useState<string>("");

  const loadItems = async () => {
    setLoading(true);
    try {
      const res = await searchKnowledgeItems({
        keyword: searchKeyword || undefined,
        industry: searchIndustry || undefined,
        tag: searchTag || undefined,
        limit: 50,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch {
      message.error("加载知识条目失败");
    } finally {
      setLoading(false);
    }
  };

  const loadDocuments = async () => {
    try {
      const docs = await listKnowledgeDocuments(50);
      setDocuments(docs);
    } catch {
      // ignore
    }
  };

  const loadStats = async () => {
    try {
      const s = await getKnowledgeStats();
      setStats(s);
    } catch {
      // ignore
    }
  };

  const loadIndustries = async () => {
    try {
      const { industries: inds } = await listKnowledgeIndustries();
      setIndustries(inds);
    } catch {
      // ignore
    }
  };

  const loadTags = async () => {
    try {
      const { tags: tgs } = await listKnowledgeTags();
      setTags(tgs);
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    loadItems();
    loadDocuments();
    loadStats();
    loadIndustries();
    loadTags();
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      loadItems();
    }, 300);
    return () => clearTimeout(timer);
  }, [searchKeyword, searchIndustry, searchTag]);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      // 清理可能未完成的搜索 debounce
    };
  }, []);

  const handleUpload = async (file: File) => {
    setUploading(true);
    try {
      const result = await uploadKnowledgeDocument(file, searchIndustry || "");
      if (result.status === "success") {
        message.success(`${file.name} 上传成功`);
      } else {
        message.warning(`${file.name} 上传中，处理可能需要一些时间`);
      }
      loadDocuments();
    } catch (e: any) {
      message.error(`上传失败：${extractApiError(e)}`);
    } finally {
      setUploading(false);
    }
    return false;
  };

  const handleDeleteDocument = async (docId: number, filename: string) => {
    try {
      await deleteKnowledgeDocument(docId);
      message.success(`文件 "${filename}" 已删除`);
      loadDocuments();
    } catch {
      message.error("删除失败");
    }
  };

  const handleDeleteItem = async (itemId: number) => {
    try {
      await deleteKnowledgeItem(itemId);
      message.success("知识条目已删除");
      loadItems();
    } catch {
      message.error("删除失败");
    }
  };

  const handleViewDetail = (item: KnowledgeItem) => {
    setSelectedItem(item);
    setDetailOpen(true);
  };

  const industryOptions = industries.map((i) => ({ label: i, value: i }));
  const tagOptions = tags.map((t) => ({ label: t, value: t }));

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <Space>
          <DatabaseOutlined style={{ fontSize: 28, color: "#1677ff" }} />
          <Typography.Title level={3} style={{ margin: 0 }}>知识库</Typography.Title>
        </Space>
      </div>

      {stats && (
        <Card style={{ marginBottom: 24 }}>
          <Space wrap>
            <div>
              <Typography.Text type="secondary">文档总数</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total_documents}</div>
            </div>
            <div>
              <Typography.Text type="secondary">成功文档</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600, color: "#52c41a" }}>{stats.success_documents}</div>
            </div>
            <div>
              <Typography.Text type="secondary">知识条目</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total_items}</div>
            </div>
            <div>
              <Typography.Text type="secondary">总使用次数</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total_usage}</div>
            </div>
          </Space>
        </Card>
      )}

      <Card title="搜索知识条目" style={{ marginBottom: 24 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input
            placeholder="搜索标题、摘要或内容..."
            prefix={<SearchOutlined />}
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
            allowClear
          />
          <Space>
            <Select
              placeholder="按行业筛选"
              style={{ width: 200 }}
              value={searchIndustry || undefined}
              onChange={(val) => setSearchIndustry(val || "")}
              options={industryOptions}
              allowClear
            />
            <Select
              placeholder="按标签筛选"
              style={{ width: 200 }}
              value={searchTag || undefined}
              onChange={(val) => setSearchTag(val || "")}
              options={tagOptions}
              allowClear
            />
          </Space>
        </Space>
      </Card>

      <Card title={`知识条目（共 ${total} 条）`} style={{ marginBottom: 24 }}>
        {items.length === 0 ? (
          <Empty description="暂无知识条目" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            loading={loading}
            dataSource={items}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button key="delete" type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => handleDeleteItem(item.id)}>
                    删除
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Typography.Text strong>{item.title}</Typography.Text>
                      {item.industry && <Tag color="blue">{item.industry}</Tag>}
                      {item.tags?.map((tag) => (
                        <Tag key={tag} color="purple">{tag}</Tag>
                      ))}
                    </Space>
                  }
                  description={
                    <div>
                      <Typography.Text type="secondary">{item.summary}</Typography.Text>
                      <div style={{ marginTop: 4 }}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          关键词：{item.keywords?.join(", ") || "无"}
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 16 }}>
                          来源：{item.source_ref}
                        </Typography.Text>
                      </div>
                    </div>
                  }
                />
                <Button type="link" onClick={() => handleViewDetail(item)}>
                  查看详情
                </Button>
              </List.Item>
            )}
          />
        )}
      </Card>

      <Card title="上传文档" style={{ marginBottom: 24 }}>
        <Upload.Dragger
          multiple
          showUploadList={false}
          beforeUpload={(file) => {
            handleUpload(file);
            return false;
          }}
          disabled={uploading}
          accept=".docx,.pdf,.doc,.txt,.md"
        >
          <p className="ant-upload-drag-icon">
            <UploadOutlined />
          </p>
          <p className="ant-upload-text">拖拽文档到此处或点击上传</p>
          <p className="ant-upload-hint">支持 .docx、.pdf、.doc、.txt、.md 格式</p>
        </Upload.Dragger>
      </Card>

      <Card title="已上传文档">
        {documents.length === 0 ? (
          <Empty description="暂无已上传文档" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            dataSource={documents}
            renderItem={(doc) => (
              <List.Item
                actions={[
                  <Popconfirm
                    key="del"
                    title="确认删除此文档？"
                    onConfirm={() => handleDeleteDocument(doc.id, doc.filename)}
                    okText="删除"
                    cancelText="取消"
                  >
                    <Button type="text" danger size="small" icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  avatar={<FileTextOutlined style={{ fontSize: 24, color: "#1677ff" }} />}
                  title={doc.filename}
                  description={
                    <Space>
                      <Tag color={statusColors[doc.status] || "default"}>{doc.status}</Tag>
                      <Typography.Text type="secondary">覆盖率：{(doc.coverage * 100).toFixed(1)}%</Typography.Text>
                      <Typography.Text type="secondary">创建时间：{doc.created_at}</Typography.Text>
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      <Modal
        title={selectedItem?.title || ""}
        open={detailOpen}
        onCancel={() => {
          setDetailOpen(false);
          setSelectedItem(null);
        }}
        footer={null}
        width={720}
      >
        {selectedItem && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Space wrap>
              {selectedItem.industry && <Tag color="blue">{selectedItem.industry}</Tag>}
              {selectedItem.tags?.map((tag) => (
                <Tag key={tag} color="purple">{tag}</Tag>
              ))}
            </Space>
            <div>
              <Typography.Text strong>摘要：</Typography.Text>
              <Typography.Paragraph>{selectedItem.summary}</Typography.Paragraph>
            </div>
            <div>
              <Typography.Text strong>关键词：</Typography.Text>
              <Space wrap>
                {selectedItem.keywords?.map((kw) => (
                  <Tag key={kw}>{kw}</Tag>
                ))}
              </Space>
            </div>
            <div>
              <Typography.Text strong>来源：</Typography.Text>
              <Typography.Text type="secondary">{selectedItem.source_ref}</Typography.Text>
            </div>
            <div>
              <Typography.Text strong>内容：</Typography.Text>
              <div style={{
                maxHeight: 400, overflow: "auto", padding: 12, background: "#f6f8fa",
                borderRadius: 4, whiteSpace: "pre-wrap", wordBreak: "break-word",
              }}>
                {selectedItem.content}
              </div>
            </div>
            <Typography.Text type="secondary">创建时间：{selectedItem.created_at}</Typography.Text>
          </Space>
        )}
      </Modal>
    </div>
  );
}
