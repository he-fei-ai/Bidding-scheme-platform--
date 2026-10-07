import { validateArchitectureTree, detectArchitectureSubtype, triggerMatch, detectChartTypeFromText } from '../services/chartValidators';

describe('chartValidators', () => {
  describe('validateArchitectureTree', () => {
    it('should validate a valid architecture tree', () => {
      const data = {
        label: 'Root',
        children: [
          { label: 'Child1', children: [] },
          { label: 'Child2', children: [] },
        ],
      };
      const result = validateArchitectureTree(data, 'organization');
      expect(result.valid).toBe(true);
      expect(result.errors).toEqual([]);
    });

    it('should reject missing root label', () => {
      const data = { children: [{ label: 'Child' }] };
      const result = validateArchitectureTree(data);
      expect(result.valid).toBe(false);
      expect(result.errors).toContain('缺少根节点标签（label 或 name 字段）');
    });

    it('should check depth limit', () => {
      // Create a deep tree (>5 levels)
      const deep: any = { label: 'L0' };
      let current = deep;
      for (let i = 0; i < 6; i++) {
        current.children = [{ label: `L${i}` }];
        current = current.children[0];
      }
      const result = validateArchitectureTree(deep);
      expect(result.valid).toBe(false);
      expect(result.errors).toContain('架构深度超过5层限制');
    });

    it('should reject duplicate sibling labels', () => {
      const data = {
        label: 'Root',
        children: [
          { label: 'Duplicate', children: [] },
          { label: 'Duplicate', children: [] },
        ],
      };
      const result = validateArchitectureTree(data);
      expect(result.valid).toBe(false);
      expect(result.errors).toContain('检测到同级重复标签');
    });
  });

  describe('detectArchitectureSubtype', () => {
    it('should detect organization subtype from keywords', () => {
      const data = {
        label: '组织结构',
        children: [
          { label: '管理部门', children: [] },
          { label: '项目组', children: [] },
        ],
      };
      const subtype = detectArchitectureSubtype(data);
      expect(subtype).toBe('organization');
    });

    it('should return null when no match', () => {
      const data = { label: 'Unknown', children: [] };
      const subtype = detectArchitectureSubtype(data);
      expect(subtype).toBeNull();
    });
  });

  describe('triggerMatch', () => {
    it('should match keywords with whitespace', () => {
      expect(triggerMatch('进度计划', '施工进度计划')).toBe(true);
      expect(triggerMatch('进度 计划', '施工进度计划')).toBe(true);
    });

    it('should be case insensitive', () => {
      expect(triggerMatch('GANTT', 'gantt chart')).toBe(true);
    });
  });

  describe('detectChartTypeFromText', () => {
    it('should detect gantt type from text with schedule keywords', () => {
      const results = detectChartTypeFromText('项目施工进度计划安排，分三个阶段完成');
      const ganttResult = results.find(r => r[0] === 'gantt');
      expect(ganttResult).toBeDefined();
      expect(ganttResult![1]).toBeGreaterThan(0);
    });
  });
});