import { useState, useRef, useEffect, useMemo, useCallback } from "react";
import { Button, Tooltip, Popover, Space, Select, Divider } from "antd";
import {
  BoldOutlined,
  ItalicOutlined,
  UnderlineOutlined,
  StrikethroughOutlined,
  OrderedListOutlined,
  UnorderedListOutlined,
  AlignLeftOutlined,
  AlignCenterOutlined,
  AlignRightOutlined,
  FileTextOutlined,
  PictureOutlined,
} from "@ant-design/icons";

interface RichTextEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  style?: React.CSSProperties;
  readOnly?: boolean;
  /** 插入图片回调：接收 base64 数据 */
  onInsertImage?: (dataUrl: string) => void;
}

/** 富文本允许的标签（白名单；黑名单永远补不齐漏洞） */
const ALLOWED_TAGS = new Set([
  "P", "BR", "DIV", "SPAN", "B", "STRONG", "I", "EM", "U", "S", "STRIKE", "DEL", "INS",
  "UL", "OL", "LI", "H1", "H2", "H3", "H4", "H5", "H6",
  "BLOCKQUOTE", "PRE", "CODE",
  "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TD", "TH", "CAPTION",
  "IMG", "A", "FONT", "HR",
]);

/** 各标签允许的属性；"*" 表示通用属性 */
const ALLOWED_ATTRS: Record<string, Set<string>> = {
  "*": new Set(["style", "class"]),
  A: new Set(["href", "title", "target", "rel"]),
  IMG: new Set(["src", "alt", "title", "width", "height"]),
  TD: new Set(["colspan", "rowspan"]),
  TH: new Set(["colspan", "rowspan"]),
  OL: new Set(["start"]),
};

/** style 里允许的属性（其余一律丢弃，防止 position:fixed 全屏覆盖等 UI 伪造） */
const ALLOWED_STYLE_PROPS = new Set([
  "text-align", "text-indent", "font-weight", "font-style", "text-decoration",
  "color", "background-color", "font-size", "line-height",
  "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
  "padding", "padding-left", "padding-right",
]);

/** a[href] 允许的协议（相对地址与锚点放行） */
const SAFE_HREF = /^(?:https?:\/\/|mailto:|tel:|#|\/(?!\/)|\.{1,2}\/)/i;
/** img[src]：只放行 base64 内联图片与 http(s) */
const SAFE_IMG = /^(?:https?:\/\/|data:image\/(?:png|jpe?g|gif|webp|bmp);base64,[A-Za-z0-9+/=\s]+$)/i;

/**
 * 清理富文本 HTML，防止 XSS / UI 伪造。
 *
 * BUG-FIX 2026-10-08（P0 安全）：旧实现是纯正则黑名单，实测可被以下载荷绕过：
 *   <a href=" javascript:alert(1)">            前导空格 → 浏览器会先剥空格再解析协议
 *   <a href="jav&#x09;ascript:alert(1)">      实体编码的制表符
 *   <a href="javascript&colon;alert(1)">      实体编码的冒号
 *   <div style="position:fixed;inset:0;z-index:99999"><button>登录</button></div>
 *                                               style 完全不受限 → 全屏 UI 伪造
 *   <input name=p>                             标签白名单缺失，表单元素可直接注入
 * 根本原因：正则无法处理「HTML 实体 / 空白 / 协议解析」这些需要 HTML 解析器语义的东西。
 * 修复：改用浏览器原生解析器（DOMParser）先做实体解码与标签规范化，再按
 * **标签白名单 + 属性白名单 + 协议白名单 + style 属性白名单** 重建 DOM。
 * 非浏览器环境（SSR/单测无 DOM）退回正则兜底，仅保证不抛异常。
 */
function sanitizeHtml(html: string): string {
  if (!html) return "";
  const hasDom = typeof DOMParser !== "undefined" && typeof document !== "undefined";
  if (!hasDom) {
    // 兜底：至少去掉事件属性与 script 类容器（不追求完备，仅保证不在无 DOM 环境崩溃）
    return html
      .replace(/<(script|iframe|object|embed|form|style|link|meta|svg|math|base)\b[^>]*>[\s\S]*?<\/\1>/gi, "")
      .replace(/<(script|iframe|object|embed|form|style|link|meta|svg|math|base)\b[^>]*\/?>/gi, "")
      .replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  }

  const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
  const root = doc.body;

  const cleanStyle = (value: string): string => {
    const out: string[] = [];
    for (const decl of value.split(";")) {
      const idx = decl.indexOf(":");
      if (idx <= 0) continue;
      const prop = decl.slice(0, idx).trim().toLowerCase();
      const val = decl.slice(idx + 1).trim();
      if (!ALLOWED_STYLE_PROPS.has(prop) || !val) continue;
      // 值里不允许 url()/expression()/注释（expression 已被白名单挡掉，这里防 url 泄漏外链）
      if (/url\s*\(|expression\s*\(|javascript:/i.test(val)) continue;
      out.push(`${prop}:${val}`);
    }
    return out.join(";");
  };

  const sanitizeNode = (node: Element) => {
    // 先摘除子树中的危险节点（SVG/MathML 命名空间也一并丢弃）
    for (const child of Array.from(node.children)) sanitizeNode(child as Element);

    for (const el of Array.from(node.querySelectorAll("*"))) {
      const tag = el.tagName.toUpperCase();
      const isForeign = el.namespaceURI && el.namespaceURI.indexOf("http://www.w3.org/2000/svg") === 0;
      if (!ALLOWED_TAGS.has(tag) || isForeign) {
        el.remove();
        continue;
      }
      const allowed = new Set([...(ALLOWED_ATTRS["*"] || []), ...(ALLOWED_ATTRS[tag] || [])]);
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        if (name.startsWith("on")) {
          el.removeAttribute(attr.name);
          continue;
        }
        if (!allowed.has(name)) {
          el.removeAttribute(attr.name);
          continue;
        }
        if (name === "style") {
          const s = cleanStyle(attr.value);
          if (s) el.setAttribute("style", s);
          else el.removeAttribute("style");
          continue;
        }
        if (name === "href") {
          if (!SAFE_HREF.test(attr.value.trim())) {
            el.removeAttribute("href");
            el.setAttribute("href", "#");
          } else if (tag === "A") {
            el.setAttribute("rel", "noopener noreferrer");
          }
          continue;
        }
        if (name === "src" && !SAFE_IMG.test(attr.value.trim())) {
          el.removeAttribute("src");
        }
      }
    }
  };

  sanitizeNode(root);
  return root.innerHTML;
}

/**
 * 富文本编辑器组件
 * 
 * 基于 contentEditable 实现，支持：
 * - 基本格式：加粗、斜体、下划线、删除线
 * - 标题：H1/H2/H3
 * - 列表：有序/无序
 * - 对齐：左/中/右
 * - 自定义 CSS 样式
 * 
 * 数据以 HTML 字符串存储，可与后端 API 无缝对接。
 */
export default function RichTextEditor({
  value,
  onChange,
  placeholder = "请输入内容...",
  style,
  readOnly = false,
  onInsertImage,
}: RichTextEditorProps) {
  const editorRef = useRef<HTMLDivElement>(null);
  const [lastContent, setLastContent] = useState(value);
  const sanitizedContent = useMemo(() => sanitizeHtml(lastContent), [lastContent]);

  // 同步外部值变化
  useEffect(() => {
    if (editorRef.current && value !== lastContent) {
      editorRef.current.innerHTML = sanitizeHtml(value || "");
      setLastContent(value);
    }
  }, [value]);

  const handleInput = () => {
    if (editorRef.current) {
      const raw = editorRef.current.innerHTML;
      // BUG-FIX 2026-09-15（中，XSS）：入库前先清洗，避免把用户粘贴/AI 注入的危险
      // HTML（如 <script>、onerror=）落库后在别处渲染时造成存储型 XSS。
      const html = sanitizeHtml(raw);
      if (html !== lastContent) {
        setLastContent(html);
        onChange(html);
      }
    }
  };

  const execCommand = (command: string, value: string = "") => {
    document.execCommand(command, false, value);
    editorRef.current?.focus();
    handleInput();
  };

  // 插入图片处理
  const handleInsertImage = useCallback(() => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.onchange = (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file || !onInsertImage) return;
      const reader = new FileReader();
      reader.onload = (ev) => {
        const dataUrl = ev.target?.result as string;
        if (dataUrl) {
          onInsertImage(dataUrl);
        }
      };
      reader.readAsDataURL(file);
    };
    input.click();
  }, [onInsertImage]);

  const selectTag = (tag: string) => () => {
    execCommand("formatBlock", tag);
  };

  const formatButtons = [
    { icon: <span style={{ fontWeight: 700, fontSize: 16 }}>H1</span>, label: "标题1", action: selectTag("h1") },
    { icon: <span style={{ fontWeight: 700, fontSize: 14 }}>H2</span>, label: "标题2", action: selectTag("h2") },
    { icon: <span style={{ fontWeight: 700, fontSize: 13 }}>H3</span>, label: "标题3", action: selectTag("h3") },
  ];

  const alignButtons = [
    { icon: <AlignLeftOutlined />, label: "左对齐", action: () => execCommand("justifyLeft") },
    { icon: <AlignCenterOutlined />, label: "居中", action: () => execCommand("justifyCenter") },
    { icon: <AlignRightOutlined />, label: "右对齐", action: () => execCommand("justifyRight") },
  ];

  return (
    <div style={{ border: "1px solid #d9d9d9", borderRadius: 6, overflow: "hidden", ...style }}>
      {/* 工具栏 */}
      {!readOnly && (
        <div
          style={{
            background: "#fafafa",
            borderBottom: "1px solid #d9d9d9",
            padding: "8px 12px",
            display: "flex",
            gap: 4,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {/* 标题 */}
          <Popover
            content={
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {formatButtons.map((btn) => (
                  <Button
                    key={btn.label}
                    type="text"
                    icon={btn.icon}
                    onClick={btn.action}
                    style={{ width: "100%", justifyContent: "flex-start" }}
                  >
                    {btn.label}
                  </Button>
                ))}
              </div>
            }
            title="标题样式"
            trigger="hover"
            placement="bottomLeft"
          >
            <Button type="text" icon={<FileTextOutlined />} />
          </Popover>

          <Divider type="vertical" style={{ margin: "0 4px" }} />

          {/* 基本格式 */}
          <Tooltip title="加粗"><Button type="text" icon={<BoldOutlined />} onClick={() => execCommand("bold")} /></Tooltip>
          <Tooltip title="斜体"><Button type="text" icon={<ItalicOutlined />} onClick={() => execCommand("italic")} /></Tooltip>
          <Tooltip title="下划线"><Button type="text" icon={<UnderlineOutlined />} onClick={() => execCommand("underline")} /></Tooltip>
          <Tooltip title="删除线"><Button type="text" icon={<StrikethroughOutlined />} onClick={() => execCommand("strikeThrough")} /></Tooltip>

          <Divider type="vertical" style={{ margin: "0 4px" }} />

          {/* 列表 */}
          <Tooltip title="有序列表"><Button type="text" icon={<OrderedListOutlined />} onClick={() => execCommand("insertOrderedList")} /></Tooltip>
          <Tooltip title="无序列表"><Button type="text" icon={<UnorderedListOutlined />} onClick={() => execCommand("insertUnorderedList")} /></Tooltip>

          <Divider type="vertical" style={{ margin: "0 4px" }} />

          {/* 图片 */}
          {!readOnly && onInsertImage && (
            <Tooltip title="插入图片">
              <Button type="text" icon={<PictureOutlined />} onClick={handleInsertImage} />
            </Tooltip>
          )}

          <Divider type="vertical" style={{ margin: "0 4px" }} />

          {/* 对齐 */}
          {alignButtons.map((btn) => (
            <Tooltip key={btn.label} title={btn.label}>
              <Button type="text" icon={btn.icon} onClick={btn.action} />
            </Tooltip>
          ))}
        </div>
      )}

      {/* 编辑区域 */}
      <div
        ref={editorRef}
        contentEditable={!readOnly}
        dangerouslySetInnerHTML={{ __html: sanitizedContent }}
        onInput={handleInput}
        className="rich-text-editor"
        style={{
          padding: 16,
          minHeight: 300,
          maxHeight: 600,
          overflow: "auto",
          fontSize: 14,
          lineHeight: 1.8,
          fontFamily: "'Microsoft YaHei', 'SimSun', serif",
          outline: "none",
          cursor: readOnly ? "text" : "default",
        }}
        data-placeholder={placeholder}
      />
    </div>
  );
}
