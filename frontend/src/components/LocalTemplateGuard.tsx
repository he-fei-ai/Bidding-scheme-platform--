import React, { useState } from "react";
import { Alert, Button, Modal, Tag, Tooltip, Typography } from "antd";

const { Paragraph, Text } = Typography;

/** 与后端 chart_auto_orchestration._LOCAL_TEMPLATE_DISCLAIMER 同口径 */
export const LOCAL_TEMPLATE_DISCLAIMER = "（模板示例数据，请按项目实际核定）";

/** 识别 Mermaid 引导语披露括注（供 mermaid 围栏分支打标签用） */
export function leadInDisclosesLocalTemplate(text: string | undefined): boolean {
  return /（模板示例数据[^）]*）/.test(text || "");
}

interface LocalTemplateGuardProps {
  /** 提供时显示「编辑数据」按钮（仅 chart-json 围栏类型支持编辑） */
  onEdit?: () => void;
  children: React.ReactNode;
}

/** source="local" 载荷的统一包裹层：标签 + 核定提示 + 可选编辑入口 */
export function LocalTemplateGuard({ onEdit, children }: LocalTemplateGuardProps) {
  const [tipOpen, setTipOpen] = useState(false);
  return (
    <div style={{ position: "relative" }}>
      <div style={{ marginBottom: 6, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Tooltip title="本图由本地模板兜底生成，数字与名称为示例值，请按项目实际核定后使用">
          <Tag color="warning">模板数据</Tag>
        </Tooltip>
        <Button size="small" type="link" onClick={() => setTipOpen(true)}>
          为什么提示？
        </Button>
        {onEdit && (
          <Button size="small" type="link" onClick={onEdit}>
            编辑数据
          </Button>
        )}
      </div>
      {children}
      <Modal
        title="关于「模板数据」提示"
        open={tipOpen}
        onCancel={() => setTipOpen(false)}
        footer={null}
      >
        <Paragraph>
          当前 AI 服务不可用或未生成时，系统使用本地模板兜底绘制了本图。
          图中的工期、人数、工序名称为<Text strong>通用示例值</Text>，可能与标书正文矛盾。
          请核对项目实际情况，通过「编辑数据」修改，或重新生成章节以触发 AI 绘图。
        </Paragraph>
        <Alert type="warning" showIcon message="投标文件中与正文矛盾的图表比缺图危害更大，导出前请务必核定。" />
      </Modal>
    </div>
  );
}
