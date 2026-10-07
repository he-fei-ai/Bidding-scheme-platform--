import { useEffect, useState } from "react";
import { Button, Card, Empty, Input, Modal, Popconfirm, Space, Spin, Tag, Typography, message, Tree } from "antd";
import { ApartmentOutlined, DeleteOutlined, EditOutlined, PlusOutlined, SearchOutlined } from "@ant-design/icons";
import {
  getIndustryTree,
  searchIndustryNodes,
  createIndustryNode,
  updateIndustryNode,
  deleteIndustryNode,
  getIndustryTreeStats,
} from "../api";
import type { IndustryNode, IndustryNodeCreate, IndustryNodeUpdate } from "../types";

interface TreeNodeData {
  key: string;
  title: React.ReactNode;
  icon?: React.ReactNode;
  children?: TreeNodeData[];
  isLeaf?: boolean;
  node: IndustryNode;
}

function convertToTreeData(nodes: IndustryNode[]): TreeNodeData[] {
  return nodes.map((node) => ({
    key: node.id,
    title: (
      <Space>
        <span>{node.name}</span>
        {node.is_active ? null : <Tag color="default">停用</Tag>}
        {node.is_ai_generated && <Tag color="purple">AI</Tag>}
        {node.reference_count > 0 && <Tag color="blue">{node.reference_count} 引用</Tag>}
      </Space>
    ),
    icon: node.icon ? <span>{node.icon}</span> : undefined,
    isLeaf: !node.children || node.children.length === 0,
    node,
    children: node.children?.length ? convertToTreeData(node.children) : undefined,
  }));
}

export default function IndustryTreePage() {
  const [treeData, setTreeData] = useState<TreeNodeData[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [stats, setStats] = useState<{ total: number; active: number; ai_generated: number; root_count: number; leaf_count: number } | null>(null);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [newNode, setNewNode] = useState<IndustryNodeCreate>({ name: "" });
  const [editingNode, setEditingNode] = useState<IndustryNode | null>(null);
  const [updateData, setUpdateData] = useState<IndustryNodeUpdate>({});
  const [searchResults, setSearchResults] = useState<TreeNodeData[]>([]);
  const [showSearchResults, setShowSearchResults] = useState(false);
  const [creating, setCreating] = useState(false);
  const [updating, setUpdating] = useState(false);

  const loadTree = async () => {
    setLoading(true);
    try {
      const data = await getIndustryTree(true);
      const converted = convertToTreeData(data);
      setTreeData(converted);
      const allKeys = extractAllKeys(converted);
      setExpandedKeys(allKeys.slice(0, 5));
    } catch {
      message.error("加载行业树失败");
    } finally {
      setLoading(false);
    }
  };

  const loadStats = async () => {
    try {
      const s = await getIndustryTreeStats();
      setStats(s);
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    loadTree();
    loadStats();
  }, []);

  const extractAllKeys = (nodes: TreeNodeData[]): string[] => {
    const keys: string[] = [];
    const walk = (ns: TreeNodeData[]) => {
      for (const n of ns) {
        keys.push(n.key);
        if (n.children) walk(n.children);
      }
    };
    walk(nodes);
    return keys;
  };

  const handleSearch = async () => {
    if (!searchKeyword.trim()) {
      setShowSearchResults(false);
      setSearchResults([]);
      return;
    }
    try {
      const results = await searchIndustryNodes(searchKeyword, undefined, 50);
      const converted = convertToTreeData(results);
      setSearchResults(converted);
      setShowSearchResults(true);
    } catch {
      message.error("搜索失败");
    }
  };

  const handleClearSearch = () => {
    setSearchKeyword("");
    setShowSearchResults(false);
    setSearchResults([]);
  };

  const handleToggleExpand = (keys: React.Key[]) => {
    setExpandedKeys(keys as string[]);
  };

  const handleSelect = (_keys: React.Key[], info: { node: any }) => {
    if (info.node?.node) {
      setSelectedKey(info.node.key);
    }
  };

  const handleCreate = async () => {
    if (!newNode.name.trim()) {
      message.warning("请输入行业名称");
      return;
    }
    setCreating(true);
    try {
      await createIndustryNode(newNode);
      message.success("行业节点创建成功");
      setCreateModalOpen(false);
      setNewNode({ name: "" });
      loadTree();
    } catch {
      message.error("创建失败");
    } finally {
      setCreating(false);
    }
  };

  const handleEdit = (node: IndustryNode) => {
    setEditingNode(node);
    setUpdateData({ name: node.name, description: node.description || "", icon: node.icon || "" });
    setEditModalOpen(true);
  };

  const handleUpdate = async () => {
    if (!editingNode) return;
    setUpdating(true);
    try {
      await updateIndustryNode(editingNode.id, updateData);
      message.success("行业节点更新成功");
      setEditModalOpen(false);
      setEditingNode(null);
      setUpdateData({});
      loadTree();
    } catch {
      message.error("更新失败");
    } finally {
      setUpdating(false);
    }
  };

  const handleDelete = async (nodeId: string, nodeName: string) => {
    try {
      await deleteIndustryNode(nodeId, true);
      message.success(`"${nodeName}" 已删除`);
      loadTree();
      if (selectedKey === nodeId) setSelectedKey(null);
    } catch {
      message.error("删除失败");
    }
  };

  const renderTreeNode = (data: TreeNodeData): React.ReactNode => (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", padding: "2px 4px" }}>
      <Space>
        {data.icon && <span>{data.icon}</span>}
        <span>{data.title}</span>
      </Space>
      <Space size={4}>
        <Button type="text" size="small" icon={<EditOutlined />} onClick={(e) => { e.stopPropagation(); handleEdit(data.node); }} />
        <Popconfirm
          title={`确认删除 "${data.node.name}"？`}
          onConfirm={() => handleDelete(data.node.id, data.node.name)}
          okText="删除"
          cancelText="取消"
        >
          <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
        </Popconfirm>
      </Space>
    </div>
  );

  const displayData = showSearchResults ? searchResults : treeData;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <Space>
          <ApartmentOutlined style={{ fontSize: 28, color: "#1677ff" }} />
          <Typography.Title level={3} style={{ margin: 0 }}>行业树</Typography.Title>
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalOpen(true)}>
          新增行业
        </Button>
      </div>

      {stats && (
        <Card style={{ marginBottom: 24 }}>
          <Space wrap>
            <div>
              <Typography.Text type="secondary">总节点数</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total}</div>
            </div>
            <div>
              <Typography.Text type="secondary">活跃节点</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600, color: "#52c41a" }}>{stats.active}</div>
            </div>
            <div>
              <Typography.Text type="secondary">AI生成</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600, color: "#722ed1" }}>{stats.ai_generated}</div>
            </div>
            <div>
              <Typography.Text type="secondary">根节点</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.root_count}</div>
            </div>
            <div>
              <Typography.Text type="secondary">叶子节点</Typography.Text>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.leaf_count}</div>
            </div>
          </Space>
        </Card>
      )}

      <Card title="搜索行业" style={{ marginBottom: 24 }}>
        <Space style={{ width: "100%" }}>
          <Input
            placeholder="搜索行业名称..."
            prefix={<SearchOutlined />}
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
            onPressEnter={handleSearch}
            allowClear
            style={{ maxWidth: 400 }}
          />
          <Button onClick={handleSearch} disabled={!searchKeyword.trim()}>搜索</Button>
          {showSearchResults && (
            <Button onClick={handleClearSearch}>显示全部</Button>
          )}
        </Space>
      </Card>

      <Card title={showSearchResults ? `搜索结果（${searchResults.length} 项）` : "行业分类树"}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 40 }}>
            <Spin size="large" />
          </div>
        ) : displayData.length === 0 ? (
          <Empty description="暂无行业数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Tree
            treeData={displayData}
            expandedKeys={expandedKeys}
            selectedKeys={selectedKey ? [selectedKey] : []}
            onSelect={handleSelect}
            onExpand={handleToggleExpand}
            showLine
            blockNode
            titleRender={renderTreeNode}
            style={{ maxHeight: 600, overflow: "auto" }}
          />
        )}
      </Card>

      <Modal
        title="新增行业"
        open={createModalOpen}
        onOk={handleCreate}
        onCancel={() => {
          setCreateModalOpen(false);
          setNewNode({ name: "" });
        }}
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <div>
            <div style={{ marginBottom: 4 }}>行业名称 *</div>
            <Input
              placeholder="请输入行业名称"
              value={newNode.name}
              onChange={(e) => setNewNode({ ...newNode, name: e.target.value })}
              onPressEnter={handleCreate}
              autoFocus
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>英文名称</div>
            <Input
              placeholder="可选"
              value={newNode.name_en || ""}
              onChange={(e) => setNewNode({ ...newNode, name_en: e.target.value })}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>描述</div>
            <Input.TextArea
              rows={3}
              placeholder="可选"
              value={newNode.description || ""}
              onChange={(e) => setNewNode({ ...newNode, description: e.target.value })}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>图标（emoji）</div>
            <Input
              placeholder="例如：🏗️"
              value={newNode.icon || ""}
              onChange={(e) => setNewNode({ ...newNode, icon: e.target.value })}
            />
          </div>
        </Space>
      </Modal>

      <Modal
        title="编辑行业"
        open={editModalOpen}
        onOk={handleUpdate}
        onCancel={() => {
          setEditModalOpen(false);
          setEditingNode(null);
          setUpdateData({});
        }}
        okText="保存"
        cancelText="取消"
        confirmLoading={updating}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <div>
            <div style={{ marginBottom: 4 }}>行业名称</div>
            <Input
              value={updateData.name || ""}
              onChange={(e) => setUpdateData({ ...updateData, name: e.target.value })}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>描述</div>
            <Input.TextArea
              rows={3}
              value={updateData.description || ""}
              onChange={(e) => setUpdateData({ ...updateData, description: e.target.value })}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>图标（emoji）</div>
            <Input
              value={updateData.icon || ""}
              onChange={(e) => setUpdateData({ ...updateData, icon: e.target.value })}
            />
          </div>
        </Space>
      </Modal>
    </div>
  );
}
