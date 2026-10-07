// ============================================================================
// 类型定义
// ============================================================================

export interface ValidationResult {
  valid: boolean;
  errors: string[];
}

interface FlattenNode {
  label: string;
  level: number;
}

// ============================================================================
// 内部工具函数
// ============================================================================

function flattenWithLevel(node: Record<string, any>, level: number = 0): FlattenNode[] {
  const result: FlattenNode[] = [];
  const label = node.label || node.name || '';
  if (label) {
    result.push({ label, level });
  }
  const children = node.children || [];
  if (Array.isArray(children)) {
    for (const child of children) {
      if (typeof child === 'object' && child !== null) {
        result.push(...flattenWithLevel(child, level + 1));
      }
    }
  }
  return result;
}

function calculateTreeDepth(node: Record<string, any>, level: number = 0): number {
  if (typeof node !== 'object' || node === null) return level;
  const children = node.children || [];
  if (!children || children.length === 0) return level;
  let maxD = level;
  for (const child of children) {
    if (typeof child === 'object' && child !== null) {
      maxD = Math.max(maxD, calculateTreeDepth(child, level + 1));
    }
  }
  return maxD;
}

function countAllNodes(node: Record<string, any>): number {
  if (typeof node !== 'object' || node === null) return 0;
  let count = 1;
  const children = node.children || [];
  if (Array.isArray(children)) {
    for (const child of children) {
      if (typeof child === 'object' && child !== null) {
        count += countAllNodes(child);
      }
    }
  }
  return count;
}

// ============================================================================
// 架构图验证
// ============================================================================

export function validateArchitectureTree(data: Record<string, any> | string, subtype: string | null = null): ValidationResult {
  const errors: string[] = [];

  let parsedData: Record<string, any>;
  try {
    if (typeof data === 'string') {
      parsedData = JSON.parse(data);
    } else {
      parsedData = data;
    }
    if (typeof parsedData !== 'object' || parsedData === null) {
      errors.push('架构图数据必须是对象格式');
      return { valid: false, errors };
    }
  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
    return { valid: false, errors };
  }

  // 检查根节点存在且有标签
  if (!(parsedData.label || parsedData.name)) {
    errors.push('缺少根节点标签（label 或 name 字段）');
    return { valid: false, errors };
  }

  // 深度检查（≤5层）
  const depth = calculateTreeDepth(parsedData);
  if (depth > 5) {
    errors.push(`架构深度超过5层限制: ${depth}层`);
    return { valid: false, errors };
  }

  // 同级节点标签唯一性检查
  const labelsSeen = new Set<string>();
  for (const nodeInfo of flattenWithLevel(parsedData)) {
    const label = nodeInfo.label;
    if (labelsSeen.has(label)) {
      errors.push(`检测到同级重复标签: '${label}'`);
    }
    labelsSeen.add(label);
  }

  // 节点数量区间检查（根据subtype）
  const totalNodes = countAllNodes(parsedData);
  const expectedRanges: Record<string, [number, number]> = {
    organization: [6, 18],
    wbs: [5, 15],
    system: [6, 12],
    infrastructure: [5, 12],
  };
  if (subtype && expectedRanges[subtype] !== undefined) {
    const [minN, maxN] = expectedRanges[subtype];
    if (totalNodes < minN) {
      errors.push(`${subtype}类型最少需${minN}个节点，实际${totalNodes}`);
    } else if (totalNodes > maxN) {
      errors.push(`${subtype}类型最多${maxN}个节点，实际${totalNodes}`);
    }
  }

  return { valid: errors.length === 0, errors };
}

// ============================================================================
// 架构子类型检测
// ============================================================================

export function detectArchitectureSubtype(data: Record<string, any> | string, sectionTitle: string | null = null): string | null {
  const keywords: string[] = [];

  function extractLabels(node: Record<string, any>) {
    if (typeof node !== 'object' || node === null) return;
    const label = node.label || node.name || '';
    if (label) {
      keywords.push(label.toLowerCase());
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
      extractLabels(data);
    }
  } catch (e) {
    // 静默失败
  }

  // 添加章节标题关键词
  if (sectionTitle) {
    const sectionKeywords = sectionTitle.split(/[，。、！？\s]+/).filter(s => s.trim());
    keywords.push(...sectionKeywords.map(k => k.toLowerCase()));
  }

  const keywordsLower = keywords.filter(k => k.trim()).map(k => k.trim());

  // 定义各子类型的关键词模式
  const patterns: Record<string, string[]> = {
    organization: [
      '组织(构)?', '部门', '岗(位)?', '机构', '管理处', '项目部',
      '指挥部', '负责人', '总工', '经理', '主任', '主管', '管理层',
      '五方责任', '职责', '职能', '汇报', '矩阵管理', '项目制',
      '职能制', '岗位说明书', '权责矩阵'
    ],
    wbs: [
      'wbs', '工作分解', '工作包', '工作包.', '任务分解', '分解结构',
      '任务清单', '工序', '作业', '单项工程', '分部工程', '分项工程'
    ],
    system: [
      '系统(架构)?', '模块构成', '模块', '软件(架构)?', '平台(架构)?',
      '子系统', '系统拓扑', '网络拓扑', '接口', '数据库', '服务',
      '体系', '框架', '架构', '组件', '中间件', '应用层', '数据层'
    ],
    infrastructure: [
      '网络拓?扑', '设备构成', '设备', '服务器', '主机', '交换机',
      '路由器', '存储', '布线', '机房', '物理架构', '硬件架构',
      '基础设施', '部署架构', '站点', '节点', '终端', '控制器'
    ]
  };

  // 计算匹配分数
  const scores: Record<string, number> = { organization: 0, wbs: 0, system: 0, infrastructure: 0 };
  for (const subType in patterns) {
    for (const pattern of patterns[subType]) {
      try {
        const regex = new RegExp(pattern);
        if (keywordsLower.some(kw => regex.test(kw))) {
          scores[subType]++;
        }
      } catch (e) {
        // 忽略正则错误
      }
    }
  }

  // 获取最高分的子类型
  let bestSubtype = 'organization';
  let bestScore = scores.organization;
  for (const subType of Object.keys(scores)) {
    if (scores[subType] > bestScore) {
      bestScore = scores[subType];
      bestSubtype = subType;
    }
  }

  if (bestScore > 0) {
    return bestSubtype;
  }
  return null;
}

// ============================================================================
// 劳动力数据验证
// ============================================================================

export function validateLaborData(data: Record<string, any> | string): ValidationResult {
  const errors: string[] = [];

  try {
    if (typeof data === 'string') {
      data = JSON.parse(data);
    }
    if (typeof data !== 'object' || data === null) {
      errors.push('劳动力数据必须是对象格式');
      return { valid: false, errors };
    }

    const phases = data.phases || data.time || data.stages || [];
    const categories = data.categories || data.types || [];
    const rawData = data.data || [];

    if (!phases || !categories || !rawData) {
      errors.push('必须包含阶段、类别和劳动率数据');
      return { valid: false, errors };
    }

    // 规范化数据矩阵
    const matrix: number[][] = [];
    for (const row of rawData) {
      let values: number[] = [];
      if (typeof row === 'object' && row !== null) {
        values = row.values || [];
      } else if (Array.isArray(row)) {
        values = row;
      }
      try {
        matrix.push(values.map(v => Number(v)));
      } catch (e) {
        // 跳过无效行
      }
    }

    if (matrix.length === 0) {
      errors.push('无效的数据矩阵');
      return { valid: false, errors };
    }

    const nCats = categories.length;
    // 补齐行长度
    for (const row of matrix) {
      if (row.length < nCats) {
        row.push(...Array(nCats - row.length).fill(0));
      }
    }

    // 检查每个阶段至少有一个工种数值 > 0
    for (let i = 0; i < matrix.length; i++) {
      const row = matrix[i];
      const hasPositive = row.some(v => v > 0);
      if (!hasPositive) {
        errors.push(`第${i+1}个阶段('${phases[i]}')所有工种数值为0，无效数据`);
      }
    }

    // 检查总人数非负
    const total = matrix.reduce((sum, row) => sum + row.reduce((a, b) => a + b, 0), 0);
    if (total < 0) {
      errors.push(`总劳动力人数不能为负数: ${total}`);
    }

  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
  }

  return { valid: errors.length === 0, errors };
}

// ============================================================================
// 触发关键词库
// =================================================================-----------

export const TRIGGER_KEYWORDS: Record<string, string[]> = {
  gantt: [
    '进度计划', '施工进度', '工期安排', '里程碑', '时间节点', '施工部署',
    '阶段划分', '总工期', '开竣工日期', '关键线路', '月度计划', '周计划',
    '赶工措施', '进度保证', '工期目标', '工期倒排', '节点目标',
    '进度款支付节点', '分段验收', '先...再...最后...', '分...个阶段',
    '从...到...需要...', '计划...开始，...结束', '工期60天', '第30天'
  ],
  flowchart: [
    '施工工序', '工艺流程', '部署顺序', '操作步骤', '工艺衔接', '施工流向',
    '作业流程', '施工程序', '逻辑关系', '先...后...', '施工流程',
    '验收流程', '报验流程', '审批流程', '移交流程', '交接检',
    '隐蔽验收流程', '变更流程', '索赔流程', '招标流程', '采购流程',
    '验收步骤', '检查步骤', '安装步骤', '调试步骤', '施工准备→...→交付',
    '如果...则...，反之...', '是否合格', '满足要求'
  ],
  architecture: [
    '组织机构', '项目管理架构', '组织架构', '管理体系', '岗位配置',
    '岗位设置', '职责分工', '责任划分', '职能分配', 'WBS',
    '工作分解', '工作包', '系统架构', '网络拓扑', '系统拓扑',
    '子系统', '模块构成', '设备构成', '系统构成', '平台架构',
    '软件架构', '管理层级', '汇报关系', '上下级关系', '矩阵管理',
    '项目制', '职能制', '指挥部', '项目部', '五方责任主体',
    '部门设置', '总包/分包/业主/监理关系', '管理跨度', '指挥体系',
    '决策层→管理层→执行层', '岗位职责', '岗位说明书', '权责矩阵',
    '设...层', '下设...个部门', '由...负责...，...向...汇报',
    '包含...层'
  ],
  labor: [
    '劳动力配置', '人员配置', '人力计划', '工种分配', '劳动力投入',
    '用工需求', '资源配置', '用工峰值', '劳动力曲线', '劳动力动态',
    '各阶段人数', '技术工种', '普工配置', '劳动力缺口', '人员调配',
    '劳动力平衡', '配置...名...工，...名...工', '高峰期80人',
    '钢筋工30人', '木工25人'
  ],
  layout: [
    '总平面布置', '施工分区', '功能分区', '功能布局', '区域划分',
    '临建布置', '交通组织', '场地布置', '堆场布置', '加工区布置',
    '办公区布置', '生活区布置', '临时道路', '围挡布置', '出入口设置',
    '材料堆放区', '周转场地', '施工便道', '现场排水', '泥浆池布置',
    '塔吊覆盖范围', '施工电梯定位', '钢筋加工棚', '木工加工棚',
    '砂浆搅拌站', '预制构件堆场', '进场道路', '消防通道',
    '垂直运输布置', '进入...区域', '...区布置', '...侧为...'
  ],
  comparison: [
    '得分权重', '成本占比', '资源配置比例', '风险分布', '投资构成',
    '费用占比', '人员比例', '工期占比', '材料占比', '合格率对比',
    '方案对比', '指标对比', 'A/B/C方案比选', '预算占比',
    '实际vs计划', '目标vs完成', '节约率', '损耗率', '利用率',
    '投诉分布', '质量问题分布', '工期延误原因分析', '各项占比',
    '各项权重', '百分比', '一方面...另一方面...', '与...相比，...高出...'
  ]
};

// 句式触发模式
export const SENTENCE_PATTERNS: Record<string, string[]> = {
  gantt: [
    '先(.+)再(.+)最后.+',
    '分(\\d+)个阶段',
    '从(.+)到(.+)需要(.+)天',
    '计划(.+)开始，(.+)结束'
  ],
  architecture: [
    '由(.+)负责(.+),(.+)向(.+)汇报',
    '设(.+)层',
    '下设(.+)个部门',
    '包含(.+)层'
  ],
  comparison: [
    '与(.+)相比(.+)高(.+)',
    '与(.+)相比(.+)低(.+)',
    '一方面(.+)另一方面(.)+'
  ],
  layout: [
    '进入(.+)区域',
    '(+)区布置',
    '东侧为(.+)|西侧为(.+)|北侧为(.+)'
  ],
  labor: [
    '配置(.+)名(.+)工,(+)名(.+)工'
  ],
  flowchart: [
    '如果(.+)则(.+)，反之(.)+'
  ]
};

// ============================================================================
// 辅助函数
// ============================================================================

export function triggerMatch(keyword: string, text: string): boolean {
  if (!keyword || !text) return false;
  const cleanKw = keyword.replace(/[\s·]/g, '').toLowerCase();
  const cleanTxt = text.replace(/[\s·]/g, '').toLowerCase();
  return cleanKw.includes(cleanTxt) || cleanTxt.includes(cleanKw);
}

export function detectChartTypeFromText(text: string): [string, number][] {
  const results: [string, number][] = [];
  const textLower = text.toLowerCase();

  for (const [chartType, keywords] of Object.entries(TRIGGER_KEYWORDS)) {
    const matchCount = keywords.filter(kw => triggerMatch(kw, textLower)).length;
    results.push([chartType, matchCount]);
  }

  for (const [chartType, patterns] of Object.entries(SENTENCE_PATTERNS)) {
    for (const pattern of patterns) {
      try {
        const regex = new RegExp(pattern);
        if (regex.test(text)) {
          const idx = results.findIndex(r => r[0] === chartType);
          if (idx >= 0) {
            results[idx] = [chartType, results[idx][1] + 1];
          }
        }
      } catch (e) {
        // 忽略正则错误
      }
    }
  }

  results.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

  return results;
}

export function extractMatchedKeywords(text: string, chartType: string): string[] {
  const matched: string[] = [];
  const keywords = TRIGGER_KEYWORDS[chartType] || [];
  for (const kw of keywords) {
    if (triggerMatch(kw, text)) {
      matched.push(kw);
    }
  }
  return matched;
}

export function detectChartWithMetadata(text: string): {
  chartTypes: [string, number][];
  matchedKeywords: Record<string, string[]>;
  topChart: string | null;
  topScore: number;
  patternsMatched: string[];
} {
  const results: [string, number][] = [];
  const matchedKeywords: Record<string, string[]> = {};
  const patternsMatched: string[] = [];
  const textLower = text.toLowerCase();

  for (const [chartType, keywords] of Object.entries(TRIGGER_KEYWORDS)) {
    const matches = keywords.filter(kw => triggerMatch(kw, textLower));
    if (matches.length > 0) {
      matchedKeywords[chartType] = matches;
    }
    results.push([chartType, matches.length]);
  }

  for (const [chartType, patterns] of Object.entries(SENTENCE_PATTERNS)) {
    for (const pattern of patterns) {
      try {
        const regex = new RegExp(pattern);
        if (regex.test(text)) {
          patternsMatched.push(`${chartType}:${pattern.substring(0, 50)}...`);
          const idx = results.findIndex(r => r[0] === chartType);
          if (idx >= 0) {
            results[idx] = [chartType, results[idx][1] + 1];
          }
        }
      } catch (e) {
        // 忽略正则错误
      }
    }
  }

  for (const chartType of Object.keys(TRIGGER_KEYWORDS)) {
    if (!(chartType in matchedKeywords)) {
      matchedKeywords[chartType] = [];
    }
  }

  results.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

  const topChart = results.length > 0 ? results[0][0] : null;
  const topScore = results.length > 0 ? results[0][1] : 0;

  return {
    chartTypes: results,
    matchedKeywords,
    topChart,
    topScore,
    patternsMatched
  };
}

// ============================================================================
// 其他验证函数（简化版）
// ============================================================================

export function validateGanttPlan(plan: any): ValidationResult {
  const errors: string[] = [];
  try {
    if (typeof plan === 'string') plan = JSON.parse(plan);
    if (typeof plan !== 'object' || plan === null) {
      errors.push('甘特图计划必须是对象格式');
      return { valid: false, errors };
    }
    const tasks = plan.tasks || plan.items || [];
    if (!Array.isArray(tasks) || tasks.length === 0) {
      errors.push('必须包含至少一个任务');
      return { valid: false, errors };
    }

    const seenIds = new Set<string>();
    for (const task of tasks) {
      if (typeof task !== 'object' || task === null) continue;
      const taskId = String(task.id || task.name || '');
      if (seenIds.has(taskId)) {
        errors.push(`检测到重复的任务ID: ${taskId}`);
      }
      seenIds.add(taskId);
    }

    const taskIds = new Set<string>(tasks.map(t => String((t.id || t.name || ''))));
    for (const task of tasks) {
      if (typeof task !== 'object' || task === null) continue;
      const deps = task.dependencies || [];
      for (const dep of deps) {
        if (!taskIds.has(String(dep))) {
          errors.push(`任务 ${task.id || task.name} 的依赖项 '${dep}' 不存在`);
        }
      }
    }
  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
  }
  return { valid: errors.length === 0, errors };
}

export function validateFlowchartStructure(data: any): ValidationResult {
  const errors: string[] = [];
  try {
    if (typeof data === 'string') data = JSON.parse(data);
    if (typeof data !== 'object' || data === null) {
      errors.push('流程图数据必须是对象格式');
      return { valid: false, errors };
    }
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    if (!Array.isArray(nodes) || nodes.length < 2) {
      errors.push('流程图必须至少包含2个节点');
      return { valid: false, errors };
    }

    const nodeIds = new Set<string>();
    for (const node of nodes) {
      if (typeof node !== 'object' || node === null) continue;
      const nid = String(node.id || node);
      if (!/^\w+$/.test(nid)) {
          errors.push(`节点ID '${nid}' 包含非ASCII字符`);
        }
      nodeIds.add(nid);
    }

    const connectedIds = new Set<string>();
    for (const edge of edges) {
      if (typeof edge !== 'object' || edge === null) continue;
      const from = String(edge.from || '');
      const to = String(edge.to || '');
      if (from) connectedIds.add(from);
      if (to) connectedIds.add(to);
    }

    const isolated = nodeIds.size - connectedIds.size;
    if (isolated > 0) {
      errors.push('检测到孤立节点（未在任何连线上出现）');
    }
  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
  }
  return { valid: errors.length === 0, errors };
}

export function validateComparisonPie(data: any): ValidationResult {
  const errors: string[] = [];
  try {
    if (typeof data === 'string') data = JSON.parse(data);
    if (typeof data !== 'object' || data === null) {
      errors.push('对比图数据必须是对象格式');
      return { valid: false, errors };
    }
    const rows = data.rows || [];
    const items = data.items || [];
    const values: number[] = [];

    if (rows.length > 0) {
      for (const row of rows) {
        if (typeof row === 'object' && row !== null) {
          if ('value' in row) values.push(Number(row['value']));
          else if ('数值' in row) values.push(Number(row['数值']));
        }
      }
    } else if (items.length > 0) {
      for (const item of items) {
        if (typeof item === 'object' && item !== null && 'value' in item) {
          values.push(Number(item.value));
        }
      }
    }

    if (values.length < 2) {
      errors.push('饼图至少需要2个数据项');
      return { valid: false, errors };
    }

    const total = values.reduce((sum, v) => sum + v, 0);
    if (total <= 0) {
      errors.push('饼图数据总和必须为正数');
      return { valid: false, errors };
    }

    const pctSum = values.reduce((sum, v) => sum + (v / total) * 100, 0);
    if (pctSum < 99 || pctSum > 101) {
      errors.push(`饼图百分比之和超出允许范围 [99, 101], 当前: ${pctSum.toFixed(2)}%`);
    }
  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
  }
  return { valid: errors.length === 0, errors };
}

export function validateLayoutData(data: any): ValidationResult {
  const errors: string[] = [];
  try {
    if (typeof data === 'string') data = JSON.parse(data);
    if (typeof data !== 'object' || data === null) {
      errors.push('布局图数据必须是对象格式');
      return { valid: false, errors };
    }
    const zones = data.zones || data.areas || [];
    if (!Array.isArray(zones) || zones.length === 0) {
      errors.push('必须包含至少一个区域（zones/areas）');
      return { valid: false, errors };
    }

    for (let i = 0; i < zones.length; i++) {
      const zone = zones[i];
      if (typeof zone !== 'object' || zone === null) {
        errors.push(`区域 ${i+1} 不是对象`);
        continue;
      }
      const name = zone.name || zone.label || zone.title || '';
      if (!name || String(name).trim() === '') {
        errors.push(`区域 ${i+1} 缺少有效名称`);
      }
    }

    const connections = data.connections || [];
    if (Array.isArray(connections)) {
      for (const conn of connections) {
        if (typeof conn !== 'object' || conn === null) continue;
        const fromIdx = conn.from;
        const toIdx = conn.to;
        if (fromIdx !== undefined && toIdx !== undefined) {
          if (typeof fromIdx !== 'number' && typeof fromIdx !== 'string') {
            errors.push(`连接项 from 必须是整数或字符串索引`);
          }
          if (typeof toIdx !== 'number' && typeof toIdx !== 'string') {
            errors.push(`连接项 to 必须是整数或字符串索引`);
          }
        }
      }
    }
  } catch (e) {
    errors.push(`数据解析失败: ${String(e)}`);
  }
  return { valid: errors.length === 0, errors };
}