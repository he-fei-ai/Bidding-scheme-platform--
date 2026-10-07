/**
 * 技术计划服务 - 集成架构图子类型检测
 * 
 * @version v3.1
 */

import { detectArchitectureSubtype } from "./chartValidators";

/**
 * 技术计划编排结果
 */
export interface TechnicalPlanArrangement {
  needed: boolean;
  type: "architecture" | "gantt" | "flowchart" | "labor" | "layout" | "comparison";
  subtype?: string; // only for architecture: organization | wbs | system | infrastructure
  priority: number;
  reason: string;
  nodeCountHint?: number;
  depthHint?: number;
  phaseCountHint?: number;
  flowHint?: string;
  estimatedPeak?: number;
  matchedKeywords?: string[];
}

/**
 * 技术计划服务类
 */
export class TechnicalPlanService {
  /**
   * 判定是否需要生成架构图表并确定子类型
   * @param data - 架构图树数据（JSON对象或字符串）
   * @param sectionTitle - 章节标题
   * @param description - 章节描述（可选）
   * @returns 技术计划编排结果
   */
  public arrangeArchitecture(
    data: Record<string, any> | string,
    sectionTitle: string,
    description?: string
  ): TechnicalPlanArrangement {
    const keywords: string[] = [];
    const matchedKeywords: string[] = [];

    // 提取章节标题关键词
    const titleKeywords = sectionTitle
      .split(/[，。、！？\s]+/)
      .filter(k => k.trim())
      .map(k => k.toLowerCase());
    keywords.push(...titleKeywords);

    // 提取章节描述关键词（如果提供）
    if (description) {
      const descKeywords = description
        .split(/[，。、！？\s]+/)
        .filter(k => k.trim())
        .map(k => k.toLowerCase());
      keywords.push(...descKeywords);
    }

    // 从树数据中提取标签关键词
    function extractLabels(node: Record<string, any>): void {
      if (typeof node !== 'object' || node === null) return;
      const label = (node.label || node.name || '').toLowerCase();
      if (label) {
        keywords.push(label);
      }
      const children = node.children || [];
      if (Array.isArray(children)) {
        for (const child of children) {
          extractLabels(child);
        }
      }
    }

    try {
      if (typeof data === 'string') {
        data = JSON.parse(data);
      }
      if (typeof data === 'object' && data !== null) {
        extractLabels(data as Record<string, any>);
      }
    } catch (error) {
      console.warn('[TechnicalPlanService] 解析数据失败:', error);
    }

    // 检测子类型
    const subtype = detectArchitectureSubtype(data, sectionTitle);

    // 判断是否需要架构图
    const needsArchitecture = !!subtype;

    // 匹配关键词追踪
    this.matchMatchedKeywords(keywords, matchedKeywords, sectionTitle);

    if (!needsArchitecture) {
      return {
        needed: false,
        type: 'architecture',
        priority: 4,
        reason: '章节未检测到需要架构图的内容',
        matchedKeywords
      };
    }

    // 根据子类型设置不同的提示参数
    const hints = this.getSubtypeHints(subtype);

    return {
      needed: true,
      type: 'architecture',
      subtype: subtype as any,
      priority: 4,
      reason: this.generateReasonMessage(subtype, sectionTitle),
      ...hints,
      matchedKeywords
    };
  }

  /**
   * 为不同子类型获取提示参数
   */
  private getSubtypeHints(subtype: string): { nodeCountHint?: number; depthHint?: number; phaseCountHint?: number; flowHint?: string; estimatedPeak?: number } {
    const hints: Record<string, any> = {
      organization: { nodeCountHint: 8, depthHint: 3, phaseCountHint: 0, flowHint: '' },
      wbs: { nodeCountHint: 10, depthHint: 4, phaseCountHint: 0, flowHint: '' },
      system: { nodeCountHint: 9, depthHint: 3, phaseCountHint: 0, flowHint: '端-边-云' },
      infrastructure: { nodeCountHint: 8, depthHint: 3, phaseCountHint: 0, flowHint: '系统→子系统→设备→终端' }
    };

    return hints[subtype] || { nodeCountHint: 8, depthHint: 3 };
  }

  /**
   * 生成原因消息
   */
  private generateReasonMessage(subtype: string, sectionTitle: string): string {
    const reasons: Record<string, string> = {
      organization: '章节涉及项目管理岗位设置与职责分工，适合用组织架构图表达',
      wbs: '章节涉及工程分解与任务拆解，适合用WBS分解图表达',
      system: '章节涉及系统模块与数据流设计，适合用系统架构图表达',
      infrastructure: '章节涉及设备构成与网络拓扑，适合用基础设施架构图表达'
    };

    return reasons[subtype] || `章节"${sectionTitle}"检测到需要架构图`;
  }

  /**
   * 追踪匹配的关键词
   */
  private matchMatchedKeywords(
    keywords: string[],
    matchedKeywords: string[],
    sectionTitle: string
  ): void {
    const titleLower = sectionTitle.toLowerCase();
    for (const kw of keywords) {
      if (titleLower.includes(kw) && !matchedKeywords.includes(kw)) {
        matchedKeywords.push(kw);
      }
    }
  }

  /**
   * 批量编排多个章节的技术计划
   */
  public arrangeMultipleSections(sections: Array<{ data: any; title: string; description?: string }>): TechnicalPlanArrangement[] {
    return sections.map(section => this.arrangeArchitecture(section.data, section.title, section.description));
  }
}

// 导出默认实例便于使用
export default new TechnicalPlanService();