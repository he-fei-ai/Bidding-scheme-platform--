import { useEffect, useRef, useState } from "react";
import { Button, Card, List, Space, Spin, Statistic, Tabs, Tag, Typography, message, Progress, Alert, Empty, Input } from "antd";
import { CopyOutlined, FileTextOutlined, DownloadOutlined, FileExcelOutlined } from "@ant-design/icons";
import { startDuplicateCheck, getDuplicateStatus, getDuplicateResult, listDuplicateTasks, getDuplicateReportUrl } from "../api";
import { extractApiError } from "../utils";
import type { DuplicateCheckTask } from "../types";

const { Text } = Typography;

export default function DuplicateCheckPage() {
  const [fileIds, setFileIds] = useState("");
  const [tenderFileId, setTenderFileId] = useState("");
  const [task, setTask] = useState<DuplicateCheckTask | null>(null);
  const [status, setStatus] = useState<{
    id?: number; status: string; progress: number; progress_message: string; error: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const pollingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const loadRecent = async () => {
      try {
        const tasks = await listDuplicateTasks(1);
        if (tasks.length > 0) setTask(tasks[0]);
      } catch {
        // ignore
      }
    };
    loadRecent();
  }, []);

  useEffect(() => {
    if (task && task.status === "running") {
      const timer = setInterval(async () => {
        try {
          if (!isMountedRef.current) return;
          const s = await getDuplicateStatus(task.id);
          setStatus(s);
          if (s.status !== "running") {
            clearInterval(timer);
            pollingTimerRef.current = null;
            if (s.status === "completed") {
              const result = await getDuplicateResult(task.id);
              setTask(result);
              message.success("查重完成");
            } else if (s.status === "failed") {
              message.error(s.error || "查重失败");
            }
          }
        } catch {
          clearInterval(timer);
          pollingTimerRef.current = null;
        }
      }, 2000);
      pollingTimerRef.current = timer;
    }
    return () => {
      if (pollingTimerRef.current) clearInterval(pollingTimerRef.current);
    };
  }, [task?.id, task?.status]);

  const handleStartCheck = async () => {
    const ids = fileIds.split(",").map((s) => Number(s.trim())).filter(Boolean);
    const tenderId = tenderFileId ? Number(tenderFileId.trim()) : undefined;
    if (ids.length === 0) {
      message.warning("请输入至少一个文件ID");
      return;
    }
    setLoading(true);
    try {
      const res = await startDuplicateCheck({
        file_ids: ids,
        tender_file_id: tenderId || null,
      });
      setTask({
        id: res.task_id,
        file_ids: ids,
        tender_file_id: tenderId || null,
        signature: "",
        status: "running",
        progress: 0,
        progress_message: "检查已启动",
        results: { metadata: { metadata: [], anomalies: [] }, toc: { tocs: [], matrix: [], filenames: [], duplicate_groups: [] }, content: { duplicate_groups: [], similar_groups: [], stats: { total_sentences: 0, duplicate_count: 0, similar_count: 0 } }, images: { total_images: 0, duplicate_groups: [] } },
        error: "",
        created_at: new Date().toISOString(),
      });
      setStatus({ status: "running", progress: 0, progress_message: "检查已启动", error: "" });
      message.success("查重任务已启动");
    } catch (e: any) {
      // BUG-FIX 2026-09-15：409 冲突已由响应拦截器用后端中文提示单独弹窗，
      // 这里不再重复提示，避免双重 toast。
      if (e?.response?.status !== 409) {
        message.error(`启动查重失败：${extractApiError(e)}`);
      }
    } finally {
      setLoading(false);
    }
  };

  const renderMetadata = () => {
    if (!task?.results.metadata) return null;
    const { metadata, anomalies } = task.results.metadata;
    const sameSourcePairs = task.results.metadata.same_source_pairs || [];
    return (
      <>
        {metadata.length > 0 ? (
          <List
            dataSource={metadata}
            renderItem={(item: any) => (
              <List.Item>
                <List.Item.Meta
                  title={item.filename || "未命名文件"}
                  description={
                    <>
                      {item.author && <Tag>作者：{item.author}</Tag>}
                      {item.company && <Tag>公司：{item.company}</Tag>}
                      {item.title && <Tag>标题：{item.title}</Tag>}
                    </>
                  }
                />
              </List.Item>
            )}
          />
        ) : (
          <Empty description="暂无元数据" />
        )}
        {anomalies.length > 0 && (
          <Alert
            type="warning"
            message="元数据异常"
            description={anomalies.map((a: any, idx: number) => (
              <div key={idx}>{typeof a === "string" ? a : (a.description || a.message || JSON.stringify(a))}</div>
            ))}
          />
        )}
        {/* ENHANCE 2026-09-15：文档同源疑点（Word RSID / PDF 结构 / WPS 账号痕迹）。
            这是围标串标认定里比"文字相似"更硬的线索，此前只在 Word 报告里体现。 */}
        {sameSourcePairs.length > 0 && (
          <Card title={`文档同源疑点（${sameSourcePairs.length} 对）`} style={{ marginTop: 16 }}>
            <Alert
              type="warning"
              showIcon
              message="同源痕迹仅表示文件由同一办公环境或同一底稿派生，不构成串标结论，请结合商务关系与投标授权情况人工核实"
              style={{ marginBottom: 12 }}
            />
            <List
              dataSource={sameSourcePairs}
              renderItem={(pair: any) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space wrap>
                        <Tag color={pair.severity === "high" ? "red" : "orange"}>
                          严重度 {pair.severity === "high" ? "高" : pair.severity === "medium" ? "中" : "低"}
                        </Tag>
                        <Text>{pair.file_a}</Text>
                        <Text type="secondary">↔</Text>
                        <Text>{pair.file_b}</Text>
                      </Space>
                    }
                    description={<Text type="secondary">{(pair.evidence || []).join("；")}</Text>}
                  />
                </List.Item>
              )}
            />
          </Card>
        )}
      </>
    );
  };

  const renderTOC = () => {
    if (!task?.results.toc) return null;
    const { tocs, duplicate_groups } = task.results.toc;
    const similarGroups = task.results.toc.similar_groups || [];
    const pairwise = task.results.toc.pairwise || [];
    const riskColor: Record<string, string> = { high: "red", medium: "orange", low: "blue", none: "default" };
    const riskText: Record<string, string> = { high: "高", medium: "中", low: "低", none: "无" };
    return (
      <>
        {tocs.length > 0 ? (
          <List
            dataSource={tocs}
            renderItem={(item: any) => (
              <List.Item>
                <List.Item.Meta
                  title={item.filename}
                  description={
                    item.titles.length > 0 && (
                      <Space wrap>
                        {item.titles.slice(0, 10).map((title: string, idx: number) => (
                          <Tag key={idx}>{title}</Tag>
                        ))}
                        {item.titles.length > 10 && (
                          <Tag>... 共 {item.titles.length} 个章节</Tag>
                        )}
                      </Space>
                    )
                  }
                />
              </List.Item>
            )}
          />
        ) : (
          <Empty description="暂无目录" />
        )}
        {duplicate_groups.length > 0 && (
          <Card title={`目录重复组（${duplicate_groups.length} 组）`} style={{ marginTop: 16 }}>
            {duplicate_groups.map((group: any, idx: number) => (
              <Card key={idx} type="inner" style={{ marginBottom: 8 }}>
                <Tag color="red">{(group.similarity * 100).toFixed(1)}%</Tag>
                <Tag>{group.file_a}</Tag>
                vs
                <Tag>{group.file_b}</Tag>
                {group.common_titles.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">相同章节：</Text>
                    {group.common_titles.map((t: string, i: number) => (
                      <Tag key={i}>{t}</Tag>
                    ))}
                  </div>
                )}
              </Card>
            ))}
          </Card>
        )}
        {/* ENHANCE 2026-09-15：近义改写标题。只看"完全相同"会漏掉
            「施工组织设计」→「施工组织设计方案」这类加字改写。 */}
        {similarGroups.length > 0 && (
          <Card title={`近义改写标题（${similarGroups.length} 处）`} style={{ marginTop: 16 }}>
            <List
              dataSource={similarGroups}
              renderItem={(group: any) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space wrap>
                        <Tag color="orange">{(group.similarity * 100).toFixed(1)}%</Tag>
                        <Text>{group.title_a}</Text>
                        <Text type="secondary">↔</Text>
                        <Text>{group.title_b}</Text>
                      </Space>
                    }
                    description={
                      <Text type="secondary">
                        {group.file_a} ↔ {group.file_b}
                        {group.criterion ? `（判定依据：${group.criterion}）` : ""}
                      </Text>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>
        )}
        {/* ENHANCE 2026-09-15：文件级综合相似度（覆盖率 + 顺序），带风险分级 */}
        {pairwise.length > 0 && (
          <Card title="文件综合相似度" style={{ marginTop: 16 }}>
            <List
              dataSource={pairwise}
              renderItem={(pair: any) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space wrap>
                        <Tag color={riskColor[pair.risk] || "default"}>风险 {riskText[pair.risk] || "无"}</Tag>
                        <Text>{(pair.score * 100).toFixed(1)}%</Text>
                        <Text type="secondary">{pair.file_a} ↔ {pair.file_b}</Text>
                      </Space>
                    }
                    description={
                      <Text type="secondary">
                        标题重合 {(pair.title_overlap * 100).toFixed(1)}% ／ 路径重合{" "}
                        {(pair.path_overlap * 100).toFixed(1)}% ／ 顺序相似{" "}
                        {(pair.order_similarity * 100).toFixed(1)}%（共同目录 {pair.shared_count} 项）
                      </Text>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>
        )}
      </>
    );
  };

  const renderContent = () => {
    if (!task?.results.content) return null;
    const { duplicate_groups, similar_groups, stats } = task.results.content;
    const total = stats.total_sentences || 0;
    const dupCount = stats.duplicate_count || 0;
    const simCount = stats.similar_count || 0;
    const similarityRate = total > 0 ? ((dupCount + simCount) / total) * 100 : 0;
    const whitelist = stats.tender_whitelist || {};
    const whitelistTotal = stats.tender_whitelist_count || 0;

    return (
      <>
        <Card>
          <Space style={{ width: "100%", justifyContent: "space-around", marginBottom: 16 }}>
            <Statistic title="总句子数" value={total} />
            <Statistic title="完全重复" value={dupCount} valueStyle={{ color: dupCount > 0 ? "#ff4d4f" : undefined }} />
            <Statistic title="高度相似" value={simCount} valueStyle={{ color: simCount > 0 ? "#faad14" : undefined }} />
            <Statistic title="相似度" value={`${similarityRate.toFixed(1)}%`} valueStyle={{ color: similarityRate > 30 ? "#ff4d4f" : "#52c41a" }} />
          </Space>
          <Progress percent={Math.round(similarityRate)} status={similarityRate > 30 ? "exception" : similarityRate > 10 ? "active" : "success"} style={{ marginBottom: 16 }} />
          {/* ENHANCE 2026-09-15：招标原文白名单四级命中统计。
              正常引用招标原文不属于抄重，这里把豁免口径摆出来，
              避免用户只看"重复数下降"而怀疑漏检。 */}
          {whitelistTotal > 0 && (
            <div style={{ marginBottom: 16 }}>
              <Text type="secondary">已排除的招标原文引用（不计入重复）：</Text>
              <Space wrap style={{ marginLeft: 8 }}>
                <Tag color="blue">完全相同 {whitelist.exact || 0}</Tag>
                <Tag color="blue">标点差异 {whitelist.strict || 0}</Tag>
                <Tag color="blue">数字替换 {whitelist.skeleton || 0}</Tag>
                <Tag color="blue">近似改写 {whitelist.near || 0}</Tag>
                <Tag>招标句库 {whitelistTotal} 句</Tag>
              </Space>
            </div>
          )}

        {duplicate_groups.length > 0 && (
          <Card title={`完全重复段落（${duplicate_groups.length} 处）`} style={{ marginBottom: 16 }}>
            <List
              dataSource={duplicate_groups}
              renderItem={(group: any) => (
                <List.Item>
                  <List.Item.Meta
                    title={<Text code>{group.sentence}</Text>}
                    description={group.occurrences.map((occ: any, idx: number) => (
                      <Tag key={idx} color="blue">{occ.filename} #{occ.sentence_idx}</Tag>
                    ))}
                  />
                </List.Item>
              )}
            />
          </Card>
        )}

        {similar_groups.length > 0 && (
          <Card title={`相似段落（${similar_groups.length} 处）`}>
            <List
              dataSource={similar_groups}
              renderItem={(group: any) => (
                <List.Item>
                  <List.Item.Meta
                    title={
                      <Space>
                        <Tag color="orange">{(group.similarity * 100).toFixed(1)}%</Tag>
                        <Text>{group.sentence_a}</Text>
                      </Space>
                    }
                    description={
                      <Space>
                        <Text type="secondary">{group.file_a}</Text>
                        <Text type="secondary">↔</Text>
                        <Text type="secondary">{group.file_b}</Text>
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          </Card>
        )}
        </Card>
      </>
    );
  };

  const renderImages = () => {
    if (!task?.results.images) return null;
    const { total_images, duplicate_groups } = task.results.images;
    if (total_images === 0) return null;
    return (
      <Card title={`图片查重（共 ${total_images} 张图片）`}>
        {duplicate_groups.length > 0 ? (
          <List
            dataSource={duplicate_groups}
            renderItem={(group: any, idx: number) => (
              <List.Item>
                <List.Item.Meta
                  title={`重复组 #${idx + 1}`}
                  description={
                    <Space direction="vertical" size={2}>
                      {/* ENHANCE 2026-09-15：带上章节路径与序号，人工才能判断
                          "同一张图是否出现在同一语境"（同源配图 vs 通用示意图） */}
                      {group.occurrences.map((img: any, i: number) => (
                        <Space key={i} wrap>
                          <Tag>{img.filename}</Tag>
                          {img.directory && <Tag color="geekblue">{img.directory}</Tag>}
                          {img.image_index ? <Tag>第 {img.image_index} 张</Tag> : null}
                          {img.previous_sentence && (
                            <Text type="secondary">前文：{img.previous_sentence}</Text>
                          )}
                        </Space>
                      ))}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        ) : (
          <Text type="secondary">无重复图片</Text>
        )}
      </Card>
    );
  };

  return (
    <div style={{ padding: 24 }}>
      <Typography.Title level={4}>
        <FileTextOutlined /> 文件查重
      </Typography.Title>
      <Card style={{ marginBottom: 24 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input
            placeholder="请输入文件ID，多个用逗号分隔"
            value={fileIds}
            onChange={(e) => setFileIds(e.target.value)}
          />
          <Input
            placeholder="招标文件ID（可选）"
            value={tenderFileId}
            onChange={(e) => setTenderFileId(e.target.value)}
          />
          <Button type="primary" onClick={handleStartCheck} loading={loading} icon={<CopyOutlined />}>
            开始查重
          </Button>
        </Space>
      </Card>

      {status?.status === "running" && (
        <Card style={{ marginBottom: 24 }}>
          <Space>
            <Spin />
            <Text>{status.progress_message || "查重中..."}</Text>
          </Space>
          {status.progress > 0 && <Progress percent={status.progress} />}
        </Card>
      )}

      {/* ENHANCE 2026-09-15：查重报告下载入口。
          此前前端只有 getDuplicateReportUrl 定义、没有任何调用方，
          后端端点还因下载守卫写死 data/projects 而恒定 403 —— 报告实际拿不到。 */}
      {task?.status === "completed" && (
        <Card style={{ marginBottom: 16 }}>
          <Space wrap>
            <Text type="secondary">导出查重报告：</Text>
            <a href={getDuplicateReportUrl(task.id, "docx")} target="_blank" rel="noopener noreferrer">
              <Button icon={<DownloadOutlined />}>Word 报告</Button>
            </a>
            <a href={getDuplicateReportUrl(task.id, "xlsx")} target="_blank" rel="noopener noreferrer">
              <Button icon={<FileExcelOutlined />}>Excel 报告（六表）</Button>
            </a>
            <Text type="secondary">
              Excel 含「查重概览 / 元数据 / 目录重复 / 文件相似度 / 重复句子 / 重复图片」六个工作表，可直接筛选排序
            </Text>
          </Space>
        </Card>
      )}

      {task?.status === "completed" && (
        <Tabs defaultActiveKey="metadata" items={[
          { key: "metadata", label: "元数据", children: renderMetadata() },
          { key: "toc", label: "目录", children: renderTOC() },
          { key: "content", label: "内容", children: renderContent() },
          { key: "images", label: "图片", children: renderImages() },
        ]} />
      )}
    </div>
  );
}