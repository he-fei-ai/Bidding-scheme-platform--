import { useEffect, useState } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { getAuthToken, http } from "../api";

type AuthState = "checking" | "ok" | "unauthorized";

function AuthChecking() {
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#5b6675",
      }}
      role="status"
      aria-label="身份验证中"
    >
      正在验证登录状态…
    </div>
  );
}

/**
 * 路由守卫（BUG-FIX 2026-09-16：原应用无任何登录/路由守卫，
 * AUTH_ENABLED=true 时 token 过期后用户停在当前页，每个请求都继续 401）。
 *
 * 判定策略（与后端 AUTH_ENABLED 开关兼容）：
 * - 本地有 token → 直接放行（失效由 api 层 401 拦截器统一清理并跳转登录页）
 * - 本地无 token → 探测 GET /auth/me：
 *   - 200：AUTH_ENABLED=false（开发模式匿名放行）或已有会话 → 放行
 *   - 401：未登录 → 重定向 /login 并携带来源路径（silent 请求，不触发全局 401 跳转）
 */
export default function ProtectedRoute() {
  const location = useLocation();
  const [state, setState] = useState<AuthState>(() => (getAuthToken() ? "ok" : "checking"));

  useEffect(() => {
    if (state !== "checking") return;
    let cancelled = false;
    http
      .get("/auth/me", { silent: true } as any)
      .then(() => {
        if (!cancelled) setState("ok");
      })
      .catch(() => {
        if (!cancelled) setState("unauthorized");
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  if (state === "checking") return <AuthChecking />;
  if (state === "unauthorized") {
    return (
      <Navigate
        to="/login"
        state={{ from: location.pathname + location.search }}
        replace
      />
    );
  }
  return <Outlet />;
}
