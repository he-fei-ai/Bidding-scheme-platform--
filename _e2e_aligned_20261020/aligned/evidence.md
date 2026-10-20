# E2E 证据 · mode=aligned · project_id=1871136

## 1. 一级目录落库（sections 表）

| 序 | 标题 | scoring_point_id | 子节点数 |
|---|---|---|---|
| 1 | 技术标澄清重点事项 | R1 | 3 |
| 2 | 设计方案 | R2 | 3 |
| 3 | 勘察方案 | R3 | 3 |
| 4 | 施工部署 | R4 | 4 |
| 5 | 施工进度控制 | R5 | 3 |
| 6 | 施工重难点技术方案 | R6 | 5 |
| 7 | 施工资源配置 | R7 | 3 |
| 8 | 关键功能性材料采购 | R8 | 3 |
| 9 | 项目管理方案 | R9 | 5 |
| 10 | 技术标澄清事项 | R10 | 5 |
| 11 | 技术标初步分析响应 | R11 | 5 |
| 12 | 施工组织设计 | R12 | 3 |
| 13 | 质量保证措施 | R13 | 4 |
| 14 | 安全文明施工措施 | R14 | 5 |
| 15 | 施工进度计划及工期保障 | R15 | 3 |

- L1 总数 15；scoring_point_id 命中 R{n}/S{n} 命名空间的 **15/15**
- 总节点 509；叶子 437
- 已生成正文节点 22（status 分布：{"completed": 22, "pending": 487}）

## 2. 服务端日志锚点
- Aligned L1 程序构造: 命中 1 处
- locked 抑制 zone 重排: 命中 2 处
- zone 机械重排实际发生: 未出现
- 直出路径: 未出现
- Aligned 回落 AI: 未出现

```
Message: 'V5.1 L1 Aligned 模式程序构造: %d 章 (评分项=%d)'
Message: 'V5.1 门禁：一级目录全部由程序锁定（Aligned），zone 重排与 zone-order 违例提示已抑制'
Message: 'V5.1 门禁：一级目录全部由程序锁定（Aligned），zone 重排与 zone-order 违例提示已抑制'
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