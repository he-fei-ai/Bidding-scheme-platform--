"""API文档生成服务

提供RESTful API文档自动生成和导出功能。
支持OpenAPI/Swagger格式、Markdown格式和HTML格式。
"""

from datetime import datetime
import logging


logger = logging.getLogger(__name__)


# API端点定义
API_ENDPOINTS = [
    {
        "path": "/api/projects",
        "methods": ["GET", "POST"],
        "description": "项目管理",
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/projects",
                "summary": "获取项目列表",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "type": "integer",
                        "description": "返回数量限制",
                    },
                    {"name": "offset", "in": "query", "type": "integer", "description": "偏移量"},
                ],
                "responses": {"200": "项目列表", "401": "未授权"},
            },
            {
                "method": "POST",
                "path": "/api/projects",
                "summary": "创建新项目",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "schema": {"name": "string", "description": "string"},
                    },
                ],
                "responses": {"201": "创建成功", "400": "参数错误", "401": "未授权"},
            },
        ],
    },
    {
        "path": "/api/projects/{project_id}/files",
        "methods": ["GET", "POST", "DELETE"],
        "description": "文件管理",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/projects/{project_id}/files",
                "summary": "上传文件",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "integer", "required": True},
                    {"name": "file", "in": "formData", "type": "file", "required": True},
                    {
                        "name": "file_role",
                        "in": "formData",
                        "type": "string",
                        "description": "文件角色",
                    },
                ],
                "supported_formats": [
                    ".docx",
                    ".pdf",
                    ".doc",
                    ".xlsx",
                    ".xls",
                    ".txt",
                    ".pptx",
                    ".ppt",
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".bmp",
                    ".tiff",
                    ".webp",
                    ".html",
                    ".htm",
                    ".wps",
                ],
                "responses": {"200": "上传成功", "400": "文件类型不支持", "401": "未授权"},
            },
            {
                "method": "GET",
                "path": "/api/projects/{project_id}/files",
                "summary": "获取文件列表",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "integer", "required": True},
                ],
                "responses": {"200": "文件列表", "401": "未授权"},
            },
        ],
    },
    {
        "path": "/api/projects/{project_id}/outline",
        "methods": ["GET", "POST"],
        "description": "大纲管理",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/projects/{project_id}/outline",
                "summary": "生成项目大纲",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "integer", "required": True},
                ],
                "responses": {"200": "大纲生成成功", "401": "未授权"},
            },
        ],
    },
    {
        "path": "/api/projects/{project_id}/sections",
        "methods": ["GET", "POST", "PUT", "DELETE"],
        "description": "章节管理",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/projects/{project_id}/sections/generate-content",
                "summary": "生成章节内容",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "integer", "required": True},
                    {
                        "name": "section_id",
                        "in": "query",
                        "type": "integer",
                        "description": "章节ID",
                    },
                ],
                "responses": {"200": "内容生成成功", "401": "未授权"},
            },
        ],
    },
    {
        "path": "/api/projects/{project_id}/export",
        "methods": ["POST"],
        "description": "文档导出",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/projects/{project_id}/export",
                "summary": "导出投标方案",
                "parameters": [
                    {"name": "project_id", "in": "path", "type": "integer", "required": True},
                    {
                        "name": "format",
                        "in": "query",
                        "type": "string",
                        "enum": ["docx", "pdf", "markdown"],
                        "default": "docx",
                    },
                ],
                "responses": {"200": "导出成功", "401": "未授权"},
            },
        ],
    },
    {
        "path": "/api/keys",
        "methods": ["GET", "POST", "DELETE"],
        "description": "API Key管理",
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/keys",
                "summary": "创建API Key",
                "parameters": [
                    {"name": "name", "in": "query", "type": "string", "default": "默认密钥"},
                    {
                        "name": "permissions",
                        "in": "query",
                        "type": "string",
                        "description": "权限列表，逗号分隔",
                    },
                    {"name": "expires_days", "in": "query", "type": "integer", "default": 365},
                ],
                "responses": {"200": "创建成功", "400": "参数错误"},
            },
            {
                "method": "GET",
                "path": "/api/keys",
                "summary": "列出所有API Key",
                "responses": {"200": "Key列表"},
            },
        ],
    },
]


def generate_openapi_doc(base_url: str = "http://localhost:8000") -> dict:
    """生成OpenAPI 3.0规范文档

    Args:
        base_url: API基础URL

    Returns:
        OpenAPI规范字典
    """
    openapi_spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "招投标方案平台 API",
            "description": "提供投标方案自动生成、文件解析、文档导出等功能的RESTful API",
            "version": "1.0.0",
            "contact": {
                "name": "API支持",
            },
            "license": {
                "name": "MIT",
            },
        },
        "servers": [
            {
                "url": base_url,
                "description": "API服务器",
            }
        ],
        "paths": {},
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "在请求头中携带API Key，格式：X-API-Key: sk-xxxxx",
                }
            },
            "schemas": {
                "Project": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "项目ID"},
                        "name": {"type": "string", "description": "项目名称"},
                        "description": {"type": "string", "description": "项目描述"},
                        "status": {
                            "type": "string",
                            "enum": ["draft", "generating", "completed"],
                            "description": "项目状态",
                        },
                        "created_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "创建时间",
                        },
                    },
                },
                "File": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "文件ID"},
                        "filename": {"type": "string", "description": "文件名"},
                        "file_type": {"type": "string", "description": "文件类型"},
                        "file_role": {
                            "type": "string",
                            "enum": ["main", "supporting"],
                            "description": "文件角色",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["success", "failed"],
                            "description": "解析状态",
                        },
                        "created_at": {
                            "type": "string",
                            "format": "date-time",
                            "description": "上传时间",
                        },
                    },
                },
                "Section": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "章节ID"},
                        "title": {"type": "string", "description": "章节标题"},
                        "content": {"type": "string", "description": "章节内容"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "generating", "completed", "paused"],
                            "description": "状态",
                        },
                        "order": {"type": "integer", "description": "排序号"},
                    },
                },
                "ApiResponse": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "响应状态"},
                        "message": {"type": "string", "description": "响应消息"},
                        "data": {"type": "object", "description": "响应数据"},
                    },
                },
            },
        },
        "tags": [
            {"name": "projects", "description": "项目管理"},
            {"name": "files", "description": "文件管理"},
            {"name": "outlines", "description": "大纲管理"},
            {"name": "sections", "description": "章节管理"},
            {"name": "export", "description": "文档导出"},
            {"name": "api_keys", "description": "API Key管理"},
        ],
    }

    # 添加路径
    for endpoint_group in API_ENDPOINTS:
        for endpoint in endpoint_group["endpoints"]:
            path = endpoint["path"]
            method = endpoint["method"].lower()

            if path not in openapi_spec["paths"]:
                openapi_spec["paths"][path] = {}

            parameters = []
            for param in endpoint.get("parameters", []):
                param_dict = {
                    "name": param["name"],
                    "in": param["in"],
                    "description": param.get("description", ""),
                    "required": param.get("required", False),
                    "schema": {"type": param.get("type", "string")},
                }
                if "enum" in param:
                    param_dict["schema"]["enum"] = param["enum"]
                parameters.append(param_dict)

            responses = {}
            for status_code, description in endpoint.get("responses", {}).items():
                responses[str(status_code)] = {
                    "description": description,
                }

            openapi_spec["paths"][path][method] = {
                "summary": endpoint.get("summary", ""),
                "operationId": f"{endpoint['method']}_{path.replace('/', '_').replace('{', '').replace('}', '')}",
                "tags": [endpoint_group["description"]],
                "parameters": parameters,
                "responses": responses,
                "security": [{"ApiKeyAuth": []}],
            }

    return openapi_spec


def generate_markdown_doc(base_url: str = "http://localhost:8000") -> str:
    """生成Markdown格式的API文档

    Args:
        base_url: API基础URL

    Returns:
        Markdown文档字符串
    """
    lines = []
    lines.append("# 招投标方案平台 API 文档\n")
    lines.append("## 基本信息\n")
    lines.append(f"- **Base URL**: `{base_url}`\n")
    lines.append("- **版本**: 1.0.0\n")
    lines.append("- **认证方式**: API Key (请求头: `X-API-Key`)\n")
    lines.append("---\n")

    lines.append("## 认证\n\n")
    lines.append("所有API请求都需要在请求头中携带API Key：\n\n")
    lines.append("```http\n")
    lines.append("X-API-Key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
    lines.append("```\n")
    lines.append("如果没有API Key，请先调用 `/api/keys` 创建。\n")
    lines.append("---\n\n")

    for group in API_ENDPOINTS:
        lines.append(f"## {group['description']}\n")
        lines.append(f"**路径前缀**: `{group['path']}`\n")

        for endpoint in group["endpoints"]:
            lines.append(f"\n### {endpoint['summary']}\n")
            lines.append(f"**Method**: `{endpoint['method']}`\n")
            lines.append(f"**Path**: `{endpoint['path']}`\n")

            if endpoint.get("parameters"):
                lines.append("\n**Parameters**:\n\n")
                lines.append("| Name | In | Type | Required | Description |")
                lines.append("|------|-----|------|----------|-------------|")
                for param in endpoint["parameters"]:
                    required = "是" if param.get("required") else "否"
                    lines.append(
                        f"| {param['name']} | {param['in']} | {param.get('type', 'string')} | {required} | {param.get('description', '')} |"
                    )

            if endpoint.get("supported_formats"):
                lines.append(f"\n**支持的文件格式**: {', '.join(endpoint['supported_formats'])}\n")

            if endpoint.get("responses"):
                lines.append("\n**Responses**:\n\n")
                for status, desc in endpoint["responses"].items():
                    lines.append(f"- `{status}`: {desc}")

            lines.append("\n---\n")

    return "\n".join(lines)


def generate_html_doc(base_url: str = "http://localhost:8000") -> str:
    """生成HTML格式的API文档

    Args:
        base_url: API基础URL

    Returns:
        HTML文档字符串
    """
    html_parts = []
    html_parts.append("<!DOCTYPE html>")
    html_parts.append('<html lang="zh-CN">')
    html_parts.append("<head>")
    html_parts.append('    <meta charset="UTF-8">')
    html_parts.append('    <meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html_parts.append("    <title>招投标方案平台 API 文档</title>")
    html_parts.append("    <style>")
    html_parts.append("        * { MARGIN: 0; PADDING: 0; BOX-SIZING: border-box; }")
    html_parts.append(
        '        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; color: #333; background: #f5f5f5; }'
    )
    html_parts.append("        .container { max-width: 1200px; MARGIN: 0 auto; PADDING: 20px; }")
    html_parts.append(
        "        header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; PADDING: 40px 0; MARGIN-BOTTOM: 30px; }"
    )
    html_parts.append("        header h1 { font-size: 2.5em; MARGIN-BOTTOM: 10px; }")
    html_parts.append("        header p { font-size: 1.1em; opacity: 0.9; }")
    html_parts.append(
        "        .card { background: white; border-radius: 8px; PADDING: 24px; MARGIN-BOTTOM: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }"
    )
    html_parts.append(
        "        .card h2 { color: #667eea; MARGIN-BOTTOM: 16px; PADDING-BOTTOM: 8px; border-bottom: 2px solid #eee; }"
    )
    html_parts.append(
        "        .endpoint { MARGIN-BOTTOM: 24px; PADDING: 16px; background: #f8f9fa; border-left: 4px solid #667eea; border-radius: 4px; }"
    )
    html_parts.append(
        "        .method { display: inline-block; PADDING: 4px 12px; border-radius: 4px; font-weight: bold; font-size: 0.9em; MARGIN-RIGHT: 12px; }"
    )
    html_parts.append("        .GET { background: #d4edda; color: #155724; }")
    html_parts.append("        .POST { background: #cce5ff; color: #004085; }")
    html_parts.append("        .PUT { background: #fff3cd; color: #856404; }")
    html_parts.append("        .DELETE { background: #f8d7da; color: #721c24; }")
    html_parts.append("        .path { font-family: monospace; font-size: 1.1em; color: #495057; }")
    html_parts.append("        table { width: 100%; border-collapse: collapse; MARGIN: 12px 0; }")
    html_parts.append(
        "        th, td { PADDING: 10px 12px; text-align: left; border: 1px solid #dee2e6; }"
    )
    html_parts.append("        th { background: #f8f9fa; font-weight: 600; }")
    html_parts.append(
        "        .badge { display: inline-block; PADDING: 2px 8px; border-radius: 4px; font-size: 0.85em; }"
    )
    html_parts.append("        .badge-required { background: #f8d7da; color: #721c24; }")
    html_parts.append("        .badge-optional { background: #d4edda; color: #155724; }")
    html_parts.append(
        "        .auth-box { background: #e7f3ff; border: 1px solid #b3d9ff; border-radius: 8px; PADDING: 20px; MARGIN-BOTTOM: 30px; }"
    )
    html_parts.append(
        "        .auth-box code { background: #fff; PADDING: 2px 8px; border-radius: 4px; }"
    )
    html_parts.append(
        "        nav { background: white; PADDING: 16px; border-radius: 8px; MARGIN-BOTTOM: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }"
    )
    html_parts.append(
        "        nav ul { list-style: none; display: flex; flex-wrap: wrap; gap: 12px; }"
    )
    html_parts.append(
        "        nav a { color: #667eea; text-decoration: none; PADDING: 6px 12px; border-radius: 4px; transition: background 0.3s; }"
    )
    html_parts.append("        nav a:hover { background: #f0f0f0; }")
    html_parts.append("        footer { text-align: center; PADDING: 20px; color: #6c757d; }")
    html_parts.append("    </style>")
    html_parts.append("</head>")
    html_parts.append("<body>")
    html_parts.append("<header>")
    html_parts.append('    <div class="container">')
    html_parts.append("        <h1>招投标方案平台 API 文档</h1>")
    html_parts.append("        <p>版本 1.0.0 | 基础URL: BASE_URL_PLACEHOLDER</p>")
    html_parts.append("    </div>")
    html_parts.append("</header>")
    html_parts.append('<div class="container">')
    html_parts.append("<nav>")
    html_parts.append("    <ul>")

    for group in API_ENDPOINTS:
        safe_id = group["description"].lower().replace(" ", "-")
        html_parts.append(f'        <li><a href="#{safe_id}">{group["description"]}</a></li>\n')

    html_parts.append("""
    </ul>
</nav>

<div class="auth-box">
    <h2 style="margin-bottom: 12px;">🔐 认证说明</h2>
    <p>所有API请求都需要在请求头中携带API Key：</p>
    <pre style="background: #fff; padding: 12px; border-radius: 4px; margin: 12px 0;"><code>X-API-Key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code></pre>
    <p>如果没有API Key，请先调用 <code>POST /api/keys</code> 创建。</p>
</div>
""")

    for group in API_ENDPOINTS:
        safe_id = group["description"].lower().replace(" ", "-")
        html_parts.append(f'<div class="card" id="{safe_id}">\n')
        html_parts.append(f'    <h2>{group["description"]}</h2>\n')
        html_parts.append(f'    <p><strong>路径前缀</strong>: <code>{group["path"]}</code></p>\n')

        for endpoint in group["endpoints"]:
            method_class = endpoint["method"].upper()
            html_parts.append('\n    <div class="endpoint">\n')
            html_parts.append(
                f'        <span class="method {method_class}">{endpoint["method"]}</span>\n'
            )
            html_parts.append(f'        <span class="path">{endpoint["path"]}</span>\n')
            html_parts.append(
                f'        <p style="margin-top: 8px;">{endpoint.get("summary", "")}</p>\n'
            )

            if endpoint.get("parameters"):
                html_parts.append(
                    "\n        <table>\n            <thead>\n                <tr>\n                    <th>名称</th>\n                    <th>位置</th>\n                    <th>类型</th>\n                    <th>必填</th>\n                    <th>说明</th>\n                </tr>\n            </thead>\n            <tbody>\n"
                )
                for param in endpoint["parameters"]:
                    required_badge = (
                        '<span class="badge badge-required">是</span>'
                        if param.get("required")
                        else '<span class="badge badge-optional">否</span>'
                    )
                    html_parts.append(
                        f'                <tr>\n                    <td><code>{param["name"]}</code></td>\n                    <td>{param["in"]}</td>\n                    <td>{param.get("type", "string")}</td>\n                    <td>{required_badge}</td>\n                    <td>{param.get("description", "")}</td>\n                </tr>\n'
                    )
                html_parts.append("            </tbody>\n        </table>\n")

            if endpoint.get("supported_formats"):
                formats = ", ".join(f"<code>{fmt}</code>" for fmt in endpoint["supported_formats"])
                html_parts.append(f"\n        <p><strong>支持格式</strong>: {formats}</p>\n")

            if endpoint.get("responses"):
                html_parts.append("\n        <p><strong>响应</strong>:</p>\n        <ul>\n")
                for status, desc in endpoint["responses"].items():
                    html_parts.append(f"            <li><code>{status}</code>: {desc}</li>\n")
                html_parts.append("        </ul>\n")

            html_parts.append("    </div>\n")

        html_parts.append("</div>\n")

    html_parts.append("""
<footer>
    <p>© 2025 招投标方案平台 | 文档生成时间: <span id="gen-time"></span></p>
</footer>

<script>
    document.getElementById('gen-time').textContent = new Date().toLocaleString('zh-CN');
</script>
</body>
</html>""")

    html_content = "\n".join(html_parts)
    # 使用replace代替format，避免CSS属性被误解析
    html_content = html_content.replace("BASE_URL_PLACEHOLDER", base_url)
    return html_content


def export_api_document(format: str = "markdown", base_url: str = "http://localhost:8000") -> dict:
    """导出API文档

    Args:
        format: 导出格式 ("openapi", "markdown", "html")
        base_url: API基础URL

    Returns:
        包含文档内容和类型的字典
    """
    exporters = {
        "openapi": generate_openapi_doc,
        "markdown": generate_markdown_doc,
        "html": generate_html_doc,
    }

    exporter = exporters.get(format)
    if not exporter:
        raise ValueError(f"不支持的文档格式: {format}，支持: {', '.join(exporters.keys())}")

    content = exporter(base_url)

    return {
        "format": format,
        "content": content,
        "content_type": {
            "openapi": "application/json",
            "markdown": "text/markdown",
            "html": "text/html",
        }[format],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
