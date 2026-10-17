import React, { useEffect, useState } from "react";
import { Table, Button, Space, message, Popconfirm, Typography, Tag, Card, Empty, Input, Modal, Tooltip } from "antd";
import {
  PlusOutlined,
  UploadOutlined,
  ReloadOutlined,
  DeleteOutlined,
  SearchOutlined,
  FolderOutlined,
  ProjectOutlined,
  FileDoneOutlined,
  EditOutlined,
  CheckSquareOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import type { Project } from "../types";
import {
  fetchProjects,
  createProject,
  deleteProject,
  fetchSections,
} from "../api";
import PageHeader from "@/components/PageHeader";

const { Text } = Typography;

function countTreeNodes(nodes: { children?: any[] }[]): number {
  return nodes.reduce(
    (sum, n) => sum + 1 + (n.children?.length ? countTreeNodes(n.children) : 0),
    0,
  );
}

/** 为项目生成稳定的柔和强调色（基于名称哈希） */
const ACCENTS = ["#1677ff", "#7c3aed", "#0d9488", "#d97706", "#dc2626", "#2563eb"];
function accentOf(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return ACCENTS[Math.abs(h) % ACCENTS.length];
}

export default function ProjectListPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [projectStats, setProjectStats] = useState<Record<number, number>>({});
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [renaming, setRenaming] = useState<{ project: Project; name: string } | null>(null);
  const [renamingLoading, setRenamingLoading] = useState(false);

  const loadProjects = async () => {
    setLoading(true);
    try {
      const result = await fetchProjects();
      const list = result?.items ?? [];
      setProjects(list);
      // 并行加载各项目章节数（失败不阻塞列表）
      const stats: Record<number, number> = {};
      await Promise.all(
        list.map(async (p) => {
          try {
            const tree = await fetchSections(p.id);
            stats[p.id] = Array.isArray(tree) ? countTreeNodes(tree) : 0;
          } catch {
            stats[p.id] = 0;
          }
        }),
      );
      setProjectStats(stats);
    } catch (e: any) {
      message.error(e.message || "加载项目列表失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadProjects();
  }, []);

  const handleCreate = async () => {
    try {
      const project = await createProject({ name: `招投标项目 ${new Date().toLocaleString("zh-CN", { hour12: false })}` });
      if (project && project.id) {
        message.success("项目创建成功，请上传招标文件");
        navigate(`/projects/${project.id}/files`);
      }
    } catch (e: any) {
      message.error(e.message || "创建项目失败");
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteProject(id);
      message.success("项目已删除");
      loadProjects();
    } catch (e: any) {
      message.error(e.message || "删除失败");
    }
  };

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    setBatchDeleting(true);
    try {
      const ids = selectedRowKeys as number[];
      await Promise.all(ids.map((id) => deleteProject(id)));
      message.success(`已删除 ${ids.length} 个项目`);
      setSelectedRowKeys([]);
      loadProjects();
    } catch (e: any) {
      message.error(e.message || "批量删除失败");
    } finally {
      setBatchDeleting(false);
    }
  };

  const handleRenameConfirm = async () => {
    if (!renaming || !renaming.name.trim()) {
      message.warning("请输入项目名称");
      return;
    }
    setRenamingLoading(true);
    try {
      const { updateProject } = await import("../api");
      await updateProject(renaming.project.id, { name: renaming.name.trim() });
      message.success("项目名称已更新");
      setRenaming(null);
      loadProjects();
    } catch (e: any) {
      message.error(e.message || "重命名失败");
    } finally {
      setRenamingLoading(false);
    }
  };

  const filteredProjects = projects.filter((p) =>
    p.name.toLowerCase().includes(searchTerm.toLowerCase()),
  );

  return (
    <div>
      <PageHeader
        icon={<ProjectOutlined />}
        title="我的项目"
        description="管理招投标项目：新建项目后上传招标文件，系统将自动提取要点并生成方案大纲。"
        extra={
          <>
            {selectedRowKeys.length > 0 && (
              <Popconfirm
                title={`确定删除选中的 ${selectedRowKeys.length} 个项目？`}
                description="将同时删除项目下全部文件与数据，操作不可恢复。"
                onConfirm={handleBatchDelete}
                okText="批量删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
              >
                <Button danger icon={<CheckSquareOutlined />} loading={batchDeleting}>
                  批量删除 ({selectedRowKeys.length})
                </Button>
              </Popconfirm>
            )}
            <Input
              placeholder="搜索项目名称"
              prefix={<SearchOutlined style={{ color: "#b4bccb" }} />}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              style={{ width: 220 }}
              allowClear
            />
            <Button icon={<ReloadOutlined />} onClick={loadProjects} loading={loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
              新建项目
            </Button>
          </>
        }
      />

      {!loading && projects.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: 14,
            marginBottom: 22,
          }}
        >
          <Card size="small" style={{ borderRadius: 12 }} styles={{ body: { padding: "16px 18px" } }}>
            <Text type="secondary" style={{ fontSize: 12 }}>项目总数</Text>
            <div className="tnum" style={{ fontSize: 26, fontWeight: 650, lineHeight: 1.3, color: "#1f2733" }}>
              {projects.length}
            </div>
          </Card>
          <Card size="small" style={{ borderRadius: 12 }} styles={{ body: { padding: "16px 18px" } }}>
            <Text type="secondary" style={{ fontSize: 12 }}>章节累计</Text>
            <div className="tnum" style={{ fontSize: 26, fontWeight: 650, lineHeight: 1.3, color: "#1677ff" }}>
              {Object.values(projectStats).reduce((a, b) => a + b, 0)}
            </div>
          </Card>
          <Card size="small" style={{ borderRadius: 12 }} styles={{ body: { padding: "16px 18px" } }}>
            <Text type="secondary" style={{ fontSize: 12 }}>已就绪项目</Text>
            <div className="tnum" style={{ fontSize: 26, fontWeight: 650, lineHeight: 1.3, color: "#16a34a" }}>
              {projects.filter((p) => (projectStats[p.id] ?? 0) > 0).length}
            </div>
          </Card>
          <Card size="small" style={{ borderRadius: 12, background: "linear-gradient(135deg, #e8f1ff 0%, #f3f8ff 100%)", borderColor: "#d4e4ff" }} styles={{ body: { padding: "16px 18px" } }}>
            <Text style={{ fontSize: 12, color: "#3d6fc4" }}>开始一个新项目</Text>
            <Button
              type="link"
              icon={<PlusOutlined />}
              onClick={handleCreate}
              style={{ padding: 0, height: "auto", marginTop: 4, fontWeight: 600 }}
            >
              立即创建
            </Button>
          </Card>
        </div>
      )}

      <Table
        dataSource={filteredProjects}
        rowKey="id"
        loading={loading}
        pagination={filteredProjects.length > 8 ? { pageSize: 10, showSizeChanger: false, hideOnSinglePage: true } : false}
        onRow={(record) => ({
          onClick: () => navigate(`/projects/${record.id}/files`),
          style: { cursor: "pointer" },
        })}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys),
        }}
        showHeader={filteredProjects.length > 0}
        locale={{
          emptyText: (
            <div style={{ padding: "40px 0 32px" }}>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <Space direction="vertical" size={4}>
                    <Text style={{ fontSize: 14, color: "#5b6675" }}>
                      {searchTerm ? "未找到匹配的项目" : "还没有项目"}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12.5 }}>
                      {searchTerm ? "换个关键词试试" : "创建第一个项目，上传招标文件后开始智能编标"}
                    </Text>
                  </Space>
                }
              >
                <Button
                  type="primary"
                  icon={searchTerm ? <SearchOutlined /> : <PlusOutlined />}
                  onClick={() => (searchTerm ? setSearchTerm("") : handleCreate())}
                >
                  {searchTerm ? "清除搜索" : "新建项目"}
                </Button>
              </Empty>
            </div>
          ),
        }}
        columns={[
          {
            title: "项目名称",
            dataIndex: "name",
            render: (text: string, record) => {
              const accent = accentOf(text || `p${record.id}`);
              const sectionCount = projectStats[record.id];
              return (
                <Space size={13}>
                  <span
                    style={{
                      width: 38,
                      height: 38,
                      borderRadius: 10,
                      display: "grid",
                      placeItems: "center",
                      color: accent,
                      background: `${accent}14`,
                      fontSize: 17,
                      flexShrink: 0,
                    }}
                  >
                    <FolderOutlined />
                  </span>
                  <div>
                    <Text strong style={{ fontSize: 14 }}>{text || `项目 #${record.id}`}</Text>
                    <div style={{ marginTop: 2 }}>
                      <Tag
                        color={sectionCount > 0 ? "green" : "default"}
                        style={{ borderRadius: 5, marginInlineEnd: 0 }}
                      >
                        {sectionCount > 0 ? `${sectionCount} 个章节` : "待上传"}
                      </Tag>
                    </div>
                  </div>
                </Space>
              );
            },
          },
          {
            title: "创建时间",
            dataIndex: "created_at",
            width: 190,
            render: (text: string) => (
              <span className="tnum" style={{ color: "#5b6675" }}>{text ? new Date(text).toLocaleString("zh-CN") : "-"}</span>
            ),
          },
          {
            title: "操作",
            width: 260,
            align: "right",
            render: (_: unknown, record: Project) => (
              <Space size={4} onClick={(e) => e.stopPropagation()}>
                <Tooltip title="重命名">
                  <Button
                    type="link"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => setRenaming({ project: record, name: record.name })}
                  />
                </Tooltip>
                <Button
                  type="link"
                  size="small"
                  icon={<UploadOutlined />}
                  onClick={() => navigate(`/projects/${record.id}/files`)}
                >
                  文件
                </Button>
                <Button
                  type="link"
                  size="small"
                  icon={<FileDoneOutlined />}
                  onClick={() => navigate(`/projects/${record.id}/outline`)}
                >
                  大纲
                </Button>
                <Popconfirm
                  title="确定删除此项目吗？"
                  description="将同时删除项目下全部文件与数据，操作不可恢复。"
                  onConfirm={() => handleDelete(record.id)}
                  okText="删除"
                  okButtonProps={{ danger: true }}
                  cancelText="取消"
                >
                  <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title="重命名项目"
        open={!!renaming}
        onOk={handleRenameConfirm}
        onCancel={() => setRenaming(null)}
        okText="保存"
        cancelText="取消"
        confirmLoading={renamingLoading}
      >
        <Input
          value={renaming?.name}
          onChange={(e) => setRenaming(r => r ? { ...r, name: e.target.value } : null)}
          onPressEnter={handleRenameConfirm}
          placeholder="输入项目名称"
          maxLength={200}
        />
      </Modal>
    </div>
  );
}
