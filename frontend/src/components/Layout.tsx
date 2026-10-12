import { useState, useEffect } from "react";
import { Layout, Menu, Button, Tooltip } from "antd";
import {
  HomeOutlined,
  RobotOutlined,
  ApartmentOutlined,
  FileTextOutlined,
  AuditOutlined,
  ApiOutlined,
  AppstoreOutlined,
  FileDoneOutlined,
  SafetyCertificateOutlined,
  DatabaseOutlined,
  CopyOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  BarChartOutlined,
  FileSearchOutlined,
  NodeIndexOutlined,
  DashboardOutlined,
  CloudUploadOutlined,
  EditOutlined,
  ExportOutlined,
} from "@ant-design/icons";
import { Outlet, useNavigate, useParams, useLocation } from "react-router-dom";
import HealthStatusIndicator from "./HealthStatusIndicator";
import TaskStatusBar from "./TaskStatusBar";
import { clearAuthToken, isAuthenticated } from "../api";

const { Sider, Content, Header } = Layout;

/** 项目工作流步骤（与侧栏项目工作流菜单顺序保持一致） */
const WORKFLOW_STEPS = [
  { path: "files", label: "文件上传" },
  { path: "review", label: "要点提取" },
  { path: "outline", label: "生成大纲" },
  { path: "sections", label: "章节编写" },
  { path: "consistency", label: "一致性校验" },
  { path: "word-count", label: "字数校验" },
  { path: "export", label: "导出配置" },
];

function LiveClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return (
    <Tooltip title={dateStr}>
      <span className="tnum" style={{ color: "#5b6675", display: "inline-flex", alignItems: "center", gap: 6 }}>
        <ClockCircleOutlined style={{ fontSize: 13, color: "#8a94a6" }} />
        {hh}:{mm}
      </span>
    </Tooltip>
  );
}

export default function AppLayout() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const projectId = id ? Number(id) : null;

  const projectMenuItems = projectId
    ? [
        {
          key: "project",
          icon: <FileSearchOutlined />,
          label: "项目工作流",
          children: [
            { key: `/projects/${projectId}/files`, icon: <CloudUploadOutlined />, label: "1. 文件上传" },
            { key: `/projects/${projectId}/review`, icon: <AuditOutlined />, label: "2. 要点提取" },
            { key: `/projects/${projectId}/outline`, icon: <ApartmentOutlined />, label: "3. 生成大纲" },
            { key: `/projects/${projectId}/sections`, icon: <FileTextOutlined />, label: "4. 章节编写" },
            { key: `/projects/${projectId}/consistency`, icon: <SafetyCertificateOutlined />, label: "5. 一致性校验" },
            { key: `/projects/${projectId}/word-count`, icon: <FileSearchOutlined />, label: "6. 字数校验" },
            { key: `/projects/${projectId}/export`, icon: <ExportOutlined />, label: "7. 导出配置" },
          ],
        },
        {
          key: "enhance",
          icon: <AppstoreOutlined />,
          label: "项目增强",
          children: [
            { key: `/projects/${projectId}/templates`, icon: <FileDoneOutlined />, label: "投标模板" },
            { key: `/projects/${projectId}/qa`, icon: <CheckCircleOutlined />, label: "AI 智能问答" },
          ],
        },
      ]
    : [];

  const globalMenuItems = [
    {
      key: "quality",
      icon: <SafetyCertificateOutlined />,
      label: "质量检查",
      children: [
        { key: "/rejection-check", icon: <AuditOutlined />, label: "废标项检查" },
        { key: "/duplicate-check", icon: <CopyOutlined />, label: "重复内容检查" },
        { key: "/feasibility-report", icon: <NodeIndexOutlined />, label: "可研报告" },
      ],
    },
    {
      key: "ai",
      icon: <RobotOutlined />,
      label: "智能体配置",
      children: [
        { key: "/ai-config", label: "AI 模型配置" },
        { key: "/text-config", label: "文本模型" },
        { key: "/image-config", label: "图片模型" },
        { key: "/prompts", icon: <EditOutlined />, label: "提示词管理" },
      ],
    },
    {
      key: "data",
      icon: <DatabaseOutlined />,
      label: "基础数据",
      children: [
        { key: "/knowledge-base", label: "知识库管理" },
        { key: "/industry-tree", icon: <ApartmentOutlined />, label: "行业分类" },
      ],
    },
    { key: "/api-keys", icon: <ApiOutlined />, label: "API Key 管理" },
    { key: "/cache-monitor", icon: <BarChartOutlined />, label: "缓存监控" },
  ];

  const allMenuItems = [...projectMenuItems, ...globalMenuItems];

  const getSelectedKey = () => {
    const path = location.pathname;
    const exact = allMenuItems
      .flatMap((m: any) => (m.children ? m.children : [m]))
      .find((item: any) => item.key === path || path.startsWith(item.key + "/"));
    return exact ? exact.key : path;
  };

  const currentWorkflowIndex = (() => {
    const path = location.pathname;
    return WORKFLOW_STEPS.findIndex((s) => path.endsWith("/" + s.path));
  })();

  const isFluidRoute = ["/sections", "/qa"].some((seg) =>
    location.pathname.includes(seg),
  );

  return (
    <Layout style={{ height: "100vh" }}>
      <Sider
        width={232}
        className="app-sider"
        breakpoint="lg"
        collapsedWidth={0}
      >
        <div className="app-brand" onClick={() => navigate("/")}>
          <div className="app-brand__mark">招</div>
          <div>
            <div className="app-brand__name">招投标方案平台</div>
            <div className="app-brand__sub">BID ASSISTANT</div>
          </div>
        </div>

        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[getSelectedKey()]}
          defaultOpenKeys={["project", "quality", "ai", "data", "enhance"]}
          items={allMenuItems}
          onClick={({ key }) => navigate(key)}
        />

        {projectId && currentWorkflowIndex >= 0 && (
          <div className="workflow-rail">
            <div className="workflow-rail__title">
              <DashboardOutlined />
              当前进度
            </div>
            {WORKFLOW_STEPS.map((step, idx) => {
              const state =
                idx < currentWorkflowIndex ? "is-done" : idx === currentWorkflowIndex ? "is-current" : "";
              return (
                <button
                  key={step.path}
                  type="button"
                  className={`workflow-step ${state}`}
                  onClick={() => navigate(`/projects/${projectId}/${step.path}`)}
                >
                  <span className="workflow-step__index">{idx + 1}</span>
                  <span className="workflow-step__label">{step.label}</span>
                </button>
              );
            })}
          </div>
        )}
      </Sider>

      <Layout style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        <Header className="app-header">
          <div className="app-header__meta">
            <Tooltip title="返回项目列表">
              <Button
                type="text"
                size="small"
                icon={<HomeOutlined style={{ color: "#5b6675" }} />}
                onClick={() => navigate("/")}
              />
            </Tooltip>
            <span className="app-header__divider" />
            <HealthStatusIndicator />
          </div>

          <div className="app-header__tasks">
            <TaskStatusBar />
          </div>

          <div className="app-header__right">
            {/* BUG-FIX 2026-09-16：登录态下提供退出入口（配合 LoginPage / ProtectedRoute）。
                AUTH_ENABLED=false 开发模式匿名访问时不显示。 */}
            {isAuthenticated() && (
              <Tooltip title="退出登录">
                <Button
                  type="text"
                  size="small"
                  icon={<ExportOutlined style={{ color: "#5b6675" }} />}
                  onClick={() => {
                    clearAuthToken();
                    window.location.assign("/login");
                  }}
                >
                  退出
                </Button>
              </Tooltip>
            )}
            <LiveClock />
          </div>
        </Header>

        <Content className="app-content">
          <div key={location.pathname} className={`app-content__inner${isFluidRoute ? " is-fluid" : ""}`}>
            <Outlet />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}
