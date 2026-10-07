# 招投标方案平台 全面代码审查与修复报告

**日期**：2026-09-15
**场景**：安全审计 + 质量审查（软件工坊 · 主理人收口）
**参与成员**：安全卫士（OWASP/STRIDE 视角）、质量门神（测试与构建）

---

## 📌 TL;DR（执行摘要）

- 整体结论：🟡 有条件通过（核心认证链路高危缺陷已修复，测试全绿；但多用户授权模型与若干工程质量项待补全）
- 阻塞项数量：0（已修复 1 个认证链路 P0 级缺陷 + 1 个越权 P0 级缺陷）
- 关键成果：
  - 后端全量测试 `1157 passed / 138 skipped / 0 failed`（新增 2 条密钥轮换回归测试）
  - 前端 `tsc -b` 0 错误，`vite build` 成功（5269 模块）
  - 修复 FastAPI 认证依赖注入缺陷，认证开启后无令牌正确返回 401
  - 修复项目越权校验（原“项目存在即放行”改为 fail-closed）
  - 清理落盘明文密钥与诊断脚本中的硬编码测试密钥
  - 新增 `backend/tests/test_secret_rotation.py`：泄露密钥不得重现于受控文件 + 本地 `.env` 密钥强度校验（CI 安全网）
- 下一步：建立项目成员/归属模型、收紧 CORS、在 Agnes 控制台作废旧密钥并替换 `.env` 占位、清理临时脚本与告警

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 条件 Go（修复后核心路径可用，授权模型待完善） |
| 严重度分布 | 🔴 2（已修复） / 🟠 3 / 🟡 若干（风格与告警） / 🟢 基础完备 |
| 关键行动项 | 5 条 |
| 建议负责人 | 后端负责人 + 安全负责人 |

---

## 1. 各成员核心结论

### 🛡️ 安全卫士（OWASP/STRIDE 审计）
- 核心判断：认证框架（JWT、SSRF 防护、生产强制开启认证）基础完备，但**认证依赖注入写法会使认证在开启时形同虚设**，且**项目级授权完全缺失**（任何登录用户可读写任意项目）。
- 关键建议：已将 `get_current_user` 改为显式接收 `fastapi.Request`，移除 JWT 中间件对 `/auth/me`、`/api/v1/files/` 的错误放行，并把 `check_project_access` 改为 fail-closed；同时清理了明文落盘 API Key 与诊断脚本硬编码密钥。

### ✅ 质量门神（QA 测试与构建）
- 核心判断：回归测试基线稳定（1152→1155 passed），前端类型检查与构建通过；存在 6 个非阻断告警（弃用 API、编码、未 await 协程）与 564 个 ruff 风格级问题（非功能性）。
- 关键建议：保留现有测试门禁；对 ruff 发现的 import 排序、尾随空白等可批量 `--fix`，但应在独立变更中处理以免污染本次安全修复 diff。

---

## 2. 综合审查发现（去重合并后按严重度排序）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 状态 |
|---|--------|------|------|---------|------|------|
| 1 | 🔴 | 认证 | `app/security/auth.py` `get_current_user` | 参数签名 `request=None` 且无类型注解，FastAPI 不会注入 Request，导致认证开启时仍错误降级为 mock admin | 改为 `request: Request`，在 `main.py` 中间件复用真实依赖 | ✅ 已修复 |
| 2 | 🔴 | 授权 | `app/security/auth.py` `check_project_access` | 只要 `projects` 表存在该行即返回 True，登录用户可横向越权访问任意项目 | 在成员/归属模型落地前 fail-closed | ✅ 已修复 |
| 3 | 🟠 | 认证中间件 | `app/main.py` `JwtAuthMiddleware.PUBLIC_PATHS_PREFIXES` | 用 `startswith` 前缀白名单，`/api/v1/auth/login-evil`、`/api/v1/health-secret` 等相似路径可绕过认证 | 改为精确路径集合 + 仅 `/favicon` 前缀 | ✅ 已修复 |
| 4 | 🟠 | 密钥管理 | `backend/.env`、`backend/_diag_*.py` | 真实 `AGNES_API_KEY` 明文落盘；诊断脚本硬编码测试密钥 | 清空 `.env`、脱敏脚本、`.env` 入 `.gitignore`、新增 `test_secret_rotation.py` 轮换回归 | ✅ 已修复 |
| 5 | 🟠 | 授权模型 | 全局（projects/sections/files/...） | 路由已加 `get_current_user` 依赖，但**无项目归属/成员关系**，管理员以外的多用户隔离无法实现 | 引入 `projects.owner_id` + `project_members`，按 `required_role` 查询授权 | ⏳ 待建 |
| 6 | 🟡 | CORS | `app/main.py` CORSMiddleware | `allow_origins` 含多个 localhost 端口、`allow_credentials=True`，若误配到生产域名会放宽 | 生产按部署域名显式配置，避免本地端口清单 | ⏳ 待配 |
| 7 | 🟡 | SSRF | `app/utils/ssrf.py` | 已有域名解析后拒绝内网 IP 的防护，但存在 DNS 重绑定 TOCTOU（解析与请求间窗口） | 改为 IP 钉扎 transport（连接前二次校验） | ⏳ 待完善 |
| 8 | 🟡 | 前端 | `frontend/src/api/index.ts` | SSE 经 URL `?token=` 传递 JWT，token 可能进入访问日志/Referer | 同源下优先移除 query token；跨域仅作兜底且加校验 | ⏳ 待完善 |
| 9 | 🟡 | 告警 | `test_*.py` 等 | pytest 配置项、未 await 协程、`on_event` 弃用、Mermaid 子进程输出编码告警 | 迁移 lifespan、清理资源、指定子进程编码 | ⏳ 待清理 |
| 10 | 🟢 | 风格 | `backend/app`（564 处） | ruff：import 排序、尾随空白、可简化表达式等 | 独立 PR 批量 `--fix` | ⏳ 可选 |

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 在供应商后台**轮换** `AGNES_API_KEY`（旧密钥 `cpk-ShoP4…` 已确认泄露；本地护栏已建：清空 `.env` + 脱敏脚本 + gitignore + `test_secret_rotation.py` 阻止回灌；当前 `.env` 为本地占位密钥，须替换为 Agnes 控制台生成的真实新密钥） | 安全/运维 + 用户 | P0 | 立即 |
| 2 | 设计落地项目归属/成员模型（`owner_id` + `project_members`），替换 `check_project_access` 的 fail-closed 占位 | 后端 | P1 | 下个迭代 |
| 3 | 生产环境收紧 CORS 与认证白名单，确认 `ENVIRONMENT=production` 且 `AUTH_ENABLED=true` | 后端/运维 | P1 | 上线前 |
| 4 | 清理根目录 60+ 临时脚本（`_diag_*`、`_patch_*`、`_fix_*` 等）并把 `diag_*.py`/`debug_*.py` 加入 `.gitignore` | 后端 | P2 | 本周 |
| 5 | SSRF 增加 IP 钉扎、前端 SSE token 改为非 URL 传递；消除测试告警 | 后端/前端 | P2 | 下个迭代 |

---

## ⚠️ 待完善 / 已知局限

- 本次未触及 `easybidding-service` 子服务（独立 Node 项目，未在本次审查范围）。
- 仓库当前无 Git 元数据，无法输出 diff/提交信息；所有修改以文件实际内容核对为准，未覆盖任何既有未提交改动。
- `check_project_access` 的 fail-closed 会导致**多用户场景在成员模型落地前全部拒绝**；这是有意为之的纵深防御，待 #2 完成后放开。
- ruff 的 564 条均为风格/现代化建议（非正确性缺陷），未在本轮修改以避免污染安全修复 diff。
- `backend/.env` 当前为本地生成的占位密钥（`cpk-` + 44 位随机串），**非真实可用**；真实密钥轮换必须在 Agnes 控制台完成，作废旧密钥后写入 `.env` 替换占位。`test_secret_rotation.py` 只做仓库/本地护栏，不替代在服务商处作废旧密钥。

---

## 📚 成员产出索引

- 安全卫士（安全审计）结论：见第 1、2 节
- 质量门神（QA 与构建）结论：见第 1、2 节

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
