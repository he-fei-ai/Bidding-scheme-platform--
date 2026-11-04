import React, { useEffect, useState } from "react";
import { Alert, Button, Modal, Typography } from "antd";

interface ChartJsonEditorProps {
  open: boolean;
  /** 当前载荷 JSON 文本（已格式化） */
  initial: string;
  /** 保存：传入校验通过的 JSON 文本（已 trim） */
  onOk: (next: string) => void;
  onCancel: () => void;
}

/** 校验编辑后的 JSON 载荷；返回错误原因或 null（与后端 source/type 口径一致） */
export function validateChartJsonPayload(v: string): string | null {
  let obj: any;
  try {
    obj = JSON.parse(v);
  } catch (e) {
    return `JSON 语法错误：${(e as Error).message}`;
  }
  if (typeof obj !== "object" || obj === null || Array.isArray(obj)) {
    return "载荷必须是 JSON 对象";
  }
  if (!obj.type) return "缺少 type 字段（图表类型）";
  if (obj.source !== "local") return 'source 字段必须保留为 "local"（来源口径不可破坏）';
  return null;
}

/** chart-json 围栏载荷编辑器：强制保留 type/source，实时校验 */
export function ChartJsonEditor({ open, initial, onOk, onCancel }: ChartJsonEditorProps) {
  const [text, setText] = useState(initial);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setText(initial);
      setErr(null);
    }
  }, [open, initial]);

  const handleOk = () => {
    const e = validateChartJsonPayload(text);
    if (e) {
      setErr(e);
      return;
    }
    onOk(text.trim());
  };

  return (
    <Modal
      title="编辑图表数据（模板数据）"
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      okText="保存"
      cancelText="取消"
      okButtonProps={{ disabled: !!err }}
      width={640}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="请按项目实际核定数字与名称。保存后将重写正文中的图表围栏，导出图题已自动剥离模板括注。"
      />
      <Typography.Paragraph>
        <textarea
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setErr(null);
          }}
          spellCheck={false}
          style={{
            width: "100%",
            minHeight: 280,
            fontFamily: "monospace",
            fontSize: 13,
            padding: 8,
            border: "1px solid #d9d9d9",
            borderRadius: 4,
            resize: "vertical",
          }}
        />
      </Typography.Paragraph>
      {err && <Alert type="error" showIcon message={err} />}
    </Modal>
  );
}
