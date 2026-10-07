import { useState, useEffect, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Card, Upload, Button, List, Tag, Space, Typography, message, Modal, Table, Progress, Empty, Popconfirm, Spin } from "antd";
import { UploadOutlined, FileTextOutlined, DeleteOutlined, AuditOutlined, ArrowLeftOutlined, CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { listTemplates, uploadTemplate, extractTemplateOutline, deleteTemplate, auditTemplateCoverage } from "../api";

const { Text, Title } = Typography;

export default function TemplatePage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = Number(id);
  const isProjectIdValid = !isNaN(projectId) && projectId > 0;

  const [templates, setTemplates] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [extracting, setExtracting] = useState<number | null>(null);
  const [auditing, setAuditing] = useState<number | null>(null);
  const [auditResult, setAuditResult] = useState<any>(null);
  const [auditVisible, setAuditVisible] = useState(false);

  const loadTemplates = useCallback(async () => {
    if (!isProjectIdValid) return;
    setLoading(true);
    try {
      const r = await listTemplates(projectId);
      setTemplates(r.items || []);
    } catch {
      message.error("加载模板列表失败");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadTemplates();
  }, [projectId]);

  const handleUpload = async (file: File) => {
    try {
      const r = await uploadTemplate(projectId, file);
      message.success(`模板「${r.filename}」上传成功`);
      await loadTemplates();
      // 自动提取目录
      setExtracting(r.template_id);
      try {
        const extractR = await extractTemplateOutline(projectId, r.template_id);
        message.success(`已提取 ${extractR.title_count} 个章节标题`);
      } catch (e: any) {
        message.error(e.response?.data?.detail || "目录提取失败");
      } finally {
        setExtracting(null);
        await loadTemplates();
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || "上传失败");
    }
    return false;
  };

  const handleExtract = async (templateId: number) => {
    setExtracting(templateId);
    try {
      const r = await extractTemplateOutline(projectId, templateId);
      message.success(`已提取 ${r.title_count} 个章节标题`);
      await loadTemplates();
    } catch (e: any) {
      message.error(e.response?.data?.detail || "目录提取失败");
    } finally {
      setExtracting(null);
    }
  };

  const handleDelete = async (templateId: number) => {
    try {
      await deleteTemplate(projectId, templateId);
      message.success("模板已删除");
      await loadTemplates();
    } catch {
      message.error("删除失败");
    }
  };

  const handleAudit = async (templateId: number) => {
    setAuditing(templateId);
    try {
      const r = await auditTemplateCoverage(projectId, templateId);
      setAuditResult(r);
      setAuditVisible(true);
    } catch (e: any) {
      message.error(e.response?.data?.detail || "审计失败");
    } finally {
      setAuditing(null);
    }
  };

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(`/projects/${projectId}/outline`)}>
          返回目录
        </Button>
      </Space>

      <Title level={4}>已有方案扩写</Title>
      <Text type="secondary" style={{ display: "block", marginBottom: 16 }}>
        上传历史标书作为模板，AI 自动提取其目录结构。正文生成时自动参考模板内容，确保覆盖历史方案中的所有章节。
      </Text>

      <Card style={{ marginBottom: 16 }}>
        <Upload.Dragger
          accept=".docx,.pdf,.doc,.txt"
          beforeUpload={handleUpload}
          showUploadList={false}
        >
          <p className="ant-upload-drag-icon">
            <UploadOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽历史标书文件到此区域上传</p>
          <p className="ant-upload-hint">支持 .docx / .pdf / .doc / .txt 格式</p>
        </Upload.Dragger>
      </Card>

      <Card title="已有模板" loading={loading}>
        {templates.length === 0 ? (
          <Empty description="尚未上传模板，请上传历史标书文件" />
        ) : (
          <List
            dataSource={templates}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button
                    key="extract"
                    size="small"
                    icon={<FileTextOutlined />}
                    onClick={() => handleExtract(item.id)}
                    loading={extracting === item.id}
                    disabled={item.status === "ready"}
                  >
                    {item.status === "ready" ? "已提取" : "提取目录"}
                  </Button>,
                  <Button
                    key="audit"
                    size="small"
                    icon={<AuditOutlined />}
                    onClick={() => handleAudit(item.id)}
                    loading={auditing === item.id}
                    disabled={item.status !== "ready"}
                  >
                    覆盖审计
                  </Button>,
                  <Popconfirm key="delete" title="确认删除此模板？" onConfirm={() => handleDelete(item.id)}>
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  avatar={<FileTextOutlined style={{ fontSize: 24, color: "#1677ff" }} />}
                  title={item.filename}
                  description={
                    <Space>
                      <Tag color={item.status === "ready" ? "green" : "processing"}>
                        {item.status === "ready" ? "已就绪" : "待处理"}
                      </Tag>
                      {item.title_count > 0 && <Text type="secondary">{item.title_count} 个章节</Text>}
                      <Text type="secondary">{item.created_at}</Text>
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      <Modal
        title="覆盖审计结果"
        open={auditVisible}
        onCancel={() => setAuditVisible(false)}
        footer={null}
        width={700}
      >
        {auditResult && (
          <>
            <Space style={{ marginBottom: 16 }}>
              <Text strong>总章节：{auditResult.total}</Text>
              <Tag color="green" icon={<CheckCircleOutlined />}>已覆盖：{auditResult.covered}</Tag>
              <Tag color="red" icon={<CloseCircleOutlined />}>缺失：{auditResult.missing}</Tag>
              <Text strong>覆盖率：{auditResult.coverage_rate}%</Text>
            </Space>
            <Progress
              percent={auditResult.coverage_rate}
              status={auditResult.coverage_rate >= 80 ? "success" : "exception"}
            />
            <Table
              dataSource={auditResult.items}
              columns={[
                { title: "章节标题", dataIndex: "title", key: "title" },
                { title: "路径", dataIndex: "path", key: "path" },
                {
                  title: "状态",
                  dataIndex: "status",
                  key: "status",
                  width: 80,
                  render: (v: string) => v === "covered"
                    ? <Tag color="green" icon={<CheckCircleOutlined />}>已覆盖</Tag>
                    : <Tag color="red" icon={<CloseCircleOutlined />}>缺失</Tag>,
                },
              ]}
              rowKey="title"
              size="small"
              pagination={{ pageSize: 15 }}
            />
          </>
        )}
      </Modal>
    </div>
  );
}