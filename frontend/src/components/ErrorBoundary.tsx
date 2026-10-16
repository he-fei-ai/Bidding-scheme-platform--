import { Component, type ReactNode } from "react";
import { Button } from "antd";
import { HomeOutlined, ReloadOutlined, WarningOutlined } from "@ant-design/icons";

interface Props {
  children: ReactNode;
  /**
   * 页面标识（可选，默认「应用」）。仅用于兜底页文案，让被接管的是「哪一层」
   * 一目了然 —— SPA 根层与页面级 ErrorBoundary 用的是同一个组件。
   */
  label?: string;
}
interface State {
  hasError: boolean;
  error?: Error;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: unknown) {
    console.error("ErrorBoundary caught:", error, info);
  }

  handleReload = () => {
    window.location.reload();
  };

  handleBackHome = () => {
    window.location.href = "/";
  };

  /**
   * 就地重试（不用整页刷新）。
   *
   * ENHANCE 2026-10-15：此前只有「刷新页面 / 返回首页」两个动作，而**唯一的**
   * ErrorBoundary 挂在 SPA 根层（`App.tsx`）—— 任何一个页面抛错都会把整棵
   * React 树（含登录壳、侧栏、路由状态）卸载掉，用户只能强刷整站。
   * 页面级 ErrorBoundary 只接管自己那一块时，「就地重试」才是符合预期的恢复
   * 动作：把 `hasError` 归零、让子树重新渲染，路由与全局状态都还在。
   * 根层沿用默认按钮组，行为与改造前完全一致（新增能力为纯增量）。
   */
  handleRetry = () => {
    this.setState({ hasError: false, error: undefined });
  };

  render() {
    if (this.state.hasError) {
      const label = this.props.label || "应用";
      return (
        <div className="error-screen">
          <div className="error-screen__icon"><WarningOutlined /></div>
          <h2>{label}出现了一些问题</h2>
          <p>
            抱歉，应用在渲染该页面时遇到错误。您可以尝试就地重试、刷新页面，或返回项目列表继续操作。
            {this.state.error?.message ? (
              <>
                <br />
                <code style={{ color: "#9aa5b8", fontSize: 12 }}>
                  {this.state.error.message.slice(0, 160)}
                </code>
              </>
            ) : null}
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <Button type="primary" size="large" icon={<ReloadOutlined />} onClick={this.handleRetry}>
              重试
            </Button>
            <Button size="large" icon={<ReloadOutlined />} onClick={this.handleReload}>
              刷新页面
            </Button>
            <Button size="large" icon={<HomeOutlined />} onClick={this.handleBackHome}>
              返回首页
            </Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
