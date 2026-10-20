# E2E 证据 · mode=free · project_id=1871136

## 1. 一级目录落库（sections 表）

| 序 | 标题 | scoring_point_id | 子节点数 |
|---|---|---|---|
| 1 | 技术方案澄清与项目整体响应 | - | 4 |
| 2 | 深化设计与优化专项方案 | - | 4 |
| 3 | 施工部署与区段划分策略 | - | 5 |
| 4 | 施工总平面布置规划 | - | 5 |
| 5 | 施工组织管理体系构建 | - | 3 |
| 6 | 既有管线排查与保护技术方案 | - | 3 |
| 7 | 新技术新工艺应用与创新措施 | - | 4 |
| 8 | 安全文明施工与环保保障方案 | - | 5 |
| 9 | 消防系统改造与验收专项方案 | - | 5 |
| 10 | 施工全过程质量控制措施 | - | 3 |
| 11 | 施工进度控制与节点管理 | - | 5 |
| 12 | 关键工序识别与资源配置计划 | - | 4 |
| 13 | 拟投入主要施工机械配置方案 | - | 3 |
| 14 | 特殊气候条件下施工保障措施 | - | 4 |
| 15 | 多专业交叉施工协调机制 | - | 4 |

- L1 总数 15；scoring_point_id 命中 R{n}/S{n} 命名空间的 **0/15**
- 总节点 541；叶子 465
- 已生成正文节点 26（status 分布：{"completed": 26, "pending": 515}）

## 2. 服务端日志锚点
- Aligned L1 程序构造: 未出现
- locked 抑制 zone 重排: 未出现
- zone 机械重排实际发生: 命中 1 处
- 直出路径: 未出现
- Aligned 回落 AI: 未出现

```
Message: 'V5.1 门禁：一级章节工程逻辑顺序倒置，已机械重排 %d 个章节：%s'
```

## 3. 九大章节程序构造演示（build_nine_chapter_l1，确定性无 AI）
- S1 工程概况  locked=True  content_mode=ai-generate
- S2 编制依据  locked=True  content_mode=ai-generate
- S3 施工计划  locked=True  content_mode=ai-generate
- S4 施工工艺技术  locked=True  content_mode=ai-generate
- S5 安全保证措施  locked=True  content_mode=ai-generate
- S6 人员配备  locked=True  content_mode=ai-generate
- S7 验收要求  locked=True  content_mode=ai-generate
- S8 应急处置措施  locked=True  content_mode=ai-generate
- S9 计算书与图纸  locked=True  content_mode=ai-generate