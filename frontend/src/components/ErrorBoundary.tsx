import { Component, type ReactNode } from "react";
import { Button } from "antd";
import { ReloadOutlined, WarningOutlined } from "@ant-design/icons";

interface Props {
  children: ReactNode;
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

  render() {
    if (this.state.hasError) {
      return (
        <div className="error-screen">
          <div className="error-screen__icon"><WarningOutlined /></div>
          <h2>页面出现了一些问题</h2>
          <p>
            抱歉，应用在渲染该页面时遇到错误。您可以尝试刷新页面，或返回项目列表继续操作。
            {this.state.error?.message ? (
              <>
                <br />
                <code style={{ color: "#9aa5b8", fontSize: 12 }}>
                  {this.state.error.message.slice(0, 160)}
                </code>
              </>
            ) : null}
          </p>
          <div style={{ display: "flex", gap: 12 }}>
            <Button type="primary" size="large" icon={<ReloadOutlined />} onClick={this.handleReload}>
              刷新页面
            </Button>
            <Button size="large" onClick={this.handleBackHome}>
              返回首页
            </Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
