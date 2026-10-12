import React from "react";
import { useNavigate } from "react-router-dom";
import { Card, Tag, Typography, Space, Anchor, Button, Tooltip } from "antd";
import { ArrowLeftOutlined, QuestionCircleOutlined } from "@ant-design/icons";

const { Title, Paragraph, Text } = Typography;

interface RuleItem {
  id: string;
  title: string;
  standard: string;
}

const GROUPS: { key: string; label: string; rules: RuleItem[] }[] = [
  {
    key: "base",
    label: "一、口径与输入（R1–R4）",
    rules: [
      {
        id: "R1",
        title: "可读字数口径",
        standard:
          "字数 = 中文（CJK）逐字计 + 西文按词计；剔除 Markdown 标记、代码块、图片、链接、HTML。生成侧、校验侧、展示侧口径完全一致。",
      },
      {
        id: "R2",
        title: "万字输入单位",
        standard:
          "最少/最多/每小节字数均以「万字」为单位（×10000），最多 4 位小数；填 0 表示不控制，AI 默认生成多少就是多少。",
      },
      {
        id: "R3",
        title: "默认单节口径",
        standard: "未设置每小节字数时，回落到全局默认单节口径 3000 字。",
      },
      {
        id: "R4",
        title: "页数预估",
        standard: "页数 ≈ 平均目标字数 ÷ 650 字/页（向上取整）。页数与排版有关，无法精确预估，不作为验收标准。",
      },
    ],
  },
  {
    key: "range",
    label: "二、区间、快照、初稿倒推（R5–R8）",
    rules: [
      {
        id: "R5",
        title: "强控小节区间 ±20%",
        standard:
          "开启强控后，每小节字数须落在 [ceil(0.8S), floor(1.2S)] 内（S 为每小节目标字数）。越界将自动压缩/扩写并提示。",
      },
      {
        id: "R6",
        title: "目标小节数反推",
        standard:
          "由总字数与每节字数反推目标叶子数：双边 ceil(均值/S)；仅上限 floor(max/S)−2；仅下限 ceil(min/S)+2；并做可行性校验（独立成册下限）。",
      },
      {
        id: "R7",
        title: "目录二审容差 ±2",
        standard: "目录审核时，实际叶子数与目标叶子数的容差为 ±2；区间内不算 leaf-count 问题。",
      },
      {
        id: "R8",
        title: "初稿折扣目标",
        standard:
          "强控且有全文上限时，单节初稿目标 = max(0.8S, floor(0.8×最大字数/叶子数))，为扩写预留缓冲、避免初稿超量。",
      },
    ],
  },
  {
    key: "adjust",
    label: "三、调整、校验、兜底（R9–R13）",
    rules: [
      {
        id: "R9",
        title: "小节强控双向调整",
        standard:
          "每节最多 3 轮；偏差 >20% 走段落粒度，否则句子粒度；方向按越界侧自动选择（低于下限扩写 / 高于上限缩写）。",
      },
      {
        id: "R10",
        title: "全文扩/缩写批次",
        standard:
          "每轮最多 10 个小节；缩写最多 3 轮、单节最多减 25%；扩写不限轮数，但连续 3 轮无净增字即停，防无限烧 token。",
      },
      {
        id: "R11",
        title: "六道硬校验",
        standard:
          "每次调整须经：非空 → 方向单调 → 不超额度 → 差距严格缩小 → 小节边界 → 全文边界；任一失败即放弃本轮、保留原正文。",
      },
      {
        id: "R12",
        title: "失败不硬裁（人工核对）",
        standard:
          "多轮修复仍不达标时，不静默截断、不硬裁正文，仅输出「人工核对」结构化警告，并逐节列出 id/标题/实际字数，把裁量权交给你。",
      },
      {
        id: "R13",
        title: "内容保真优先",
        standard:
          "保留原方案实质内容（技术路线、参数、周期、人员、验收、售后、承诺等）高于字数要求；字数服从内容保真。",
      },
    ],
  },
  {
    key: "anchor",
    label: "四、锚定与独立成册（R14–R15）",
    rules: [
      {
        id: "R14",
        title: "配置快照锚定",
        standard:
          "字数配置在目录生成成功时固化为快照，正文生成只认快照；无快照禁止生成正文。修改配置后须重新固化（重新生成目录）。",
      },
      {
        id: "R15",
        title: "独立成册下限",
        standard:
          "可研/独立成册目标总字数下限 1000 字、默认 30000 字（软引导，非闭环强控）。",
      },
    ],
  },
];

const SOURCE =
  "J:\\编程\\报告_易标文字生成数量控制规则与标准_2026-10-12.md（§9 规则速查表 R1–R15）";

export default function WordControlHelpPage() {
  const navigate = useNavigate();
  return (
    <div style={{ maxWidth: 920, margin: "0 auto", padding: 24 }}>
      <Space style={{ marginBottom: 16 }} align="center">
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>
          返回
        </Button>
        <Title level={3} style={{ margin: 0 }}>
          字数控制标准（引用易标 R1–R15）
        </Title>
        <Tooltip title="本平台字数控制体系标准来源">
          <QuestionCircleOutlined style={{ color: "#999" }} />
        </Tooltip>
      </Space>

      <Paragraph type="secondary" style={{ fontSize: 13 }}>
        本平台字数控制以易标（OpenBidKit）审计报告定义的 R1–R15 为权威标准，已移植为后端纯函数实现并完成生产线接线。
        标准来源：<Text code>{SOURCE}</Text>
      </Paragraph>

      <Anchor
        offsetTop={80}
        items={GROUPS.map((g) => ({ key: g.key, href: `#${g.key}`, title: g.label }))}
        style={{ marginBottom: 16 }}
      />

      {GROUPS.map((g) => (
        <section id={g.key} key={g.key} style={{ marginBottom: 24 }}>
          <Title level={4}>{g.label}</Title>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {g.rules.map((r) => (
              <Card key={r.id} size="small" style={{ width: "100%" }}>
                <Space align="start">
                  <Tag color="blue" style={{ fontSize: 13 }}>
                    {r.id}
                  </Tag>
                  <div>
                    <Text strong>{r.title}</Text>
                    <Paragraph style={{ margin: "4px 0 0", fontSize: 13 }}>
                      {r.standard}
                    </Paragraph>
                  </div>
                </Space>
              </Card>
            ))}
          </Space>
        </section>
      ))}

      <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 24 }}>
        本平台实现落点详见 <Text code>doc/易标字数控制标准引用.md</Text> 与
        <Text code> doc/字数控制.md</Text>。后端权威实现位于
        <Text code> backend/app/services/word_control.py</Text>、
        <Text code> word_target_resolver.py</Text>、
        <Text code> ai/workflows_word_adjust.py</Text>。
      </Paragraph>
    </div>
  );
}
