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

/** 清理 HTML 中的危险元素和属性，防止 XSS 攻击 */
function sanitizeHtml(html: string): string {
  if (!html) return "";
  let clean = html;
  // 1) 移除可承载脚本/行为的容器标签及其内容（含 svg/math 等可注入点）
  clean = clean.replace(
    /<(script|iframe|object|embed|form|style|link|meta|svg|math|base|body|html|head)\b[^<]*(?:(?!<\/\1>)<[^<]*)*<\/\1>/gi,
    "",
  );
  // 2) 移除危险的自闭合/无结束标签
  clean = clean.replace(
    /<(script|iframe|object|embed|form|style|link|meta|svg|math|base)\b[^>]*\/?>/gi,
    "",
  );
  // 3) 移除所有 on* 事件处理属性 (onerror, onclick, onload 等)
  clean = clean.replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  // 4) 中和任意属性值中的 javascript: 危险协议（带/不带引号、任意属性名）
  //    注意：保留 data: 协议以支持 base64 内联图片（如 <img src="data:image/png;base64,...">）
  clean = clean.replace(
    /(=\s*)(?:"javascript:[^"]*"|'javascript:[^']*'|javascript:[^\s>]+)/gi,
    '$1"#"',
  );
  return clean;
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
