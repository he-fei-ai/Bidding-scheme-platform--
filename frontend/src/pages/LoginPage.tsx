import { useState } from "react";
import { Button, Card, Form, Input, message } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { useNavigate, useSearchParams } from "react-router-dom";
import { login } from "../api";
import { extractApiError } from "../utils";

/**
 * 登录页（BUG-FIX 2026-09-16：此前全应用没有任何登录入口，
 * AUTH_ENABLED=true 时 401 后用户只能停在原页面反复收到报错）。
 *
 * 登录成功后按 ?from= 返回来源路径（由 ProtectedRoute / 401 拦截器携带）。
 * 后端 AUTH_ENABLED=false（开发模式）时不会进入本页：ProtectedRoute 探测
 * /auth/me 返回 200 即匿名放行。
 */
export default function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const result = await login(values.username.trim(), values.password);
      message.success(`欢迎，${result.user?.username || values.username}`);
      const from = searchParams.get("from");
      navigate(from && from.startsWith("/") ? from : "/", { replace: true });
    } catch (e) {
      // login 使用 silent 配置，拦截器不弹提示，这里统一给出友好错误
      message.error(extractApiError(e) || "登录失败，请检查用户名或密码");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f0f2f5",
      }}
    >
      <Card style={{ width: 380, boxShadow: "0 4px 16px rgba(0,0,0,0.08)" }} title="招投标方案平台 · 登录">
        <Form onFinish={onFinish} layout="vertical" autoComplete="off">
          <Form.Item
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" size="large" autoFocus />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block size="large" loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
