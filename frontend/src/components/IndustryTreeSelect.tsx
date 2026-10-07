import { useState, useEffect, useCallback, useRef } from "react";
import { TreeSelect, Spin, message, Empty } from "antd";
import { getIndustryTree, getIndustryScoringItems } from "../api";
import type { IndustryNode } from "../types";

interface IndustryTreeSelectProps {
  value?: string;
  onChange?: (value: string | undefined) => void;
  onScoringItemsChange?: (items: any[]) => void;
  placeholder?: string;
  allowClear?: boolean;
  disabled?: boolean;
  showSearch?: boolean;
}

/**
 * 行业树选择器组件
 * 从行业树加载数据并渲染为 TreeSelect 下拉选择
 */
export default function IndustryTreeSelect({
  value,
  onChange,
  onScoringItemsChange,
  placeholder = "请选择行业",
  allowClear = true,
  disabled = false,
  showSearch = true,
}: IndustryTreeSelectProps) {
  const [treeData, setTreeData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  const flattenTree = useCallback((nodes: IndustryNode[], level = 1): any[] => {
    return nodes.flatMap((node) => [
      {
        value: node.id,
        label: `${"  ".repeat(level - 1)}${node.name}`,
        isLeaf: node.children?.length === 0,
        level: node.level,
      },
      ...(node.children?.length
        ? flattenTree(node.children, level + 1)
        : []),
    ]);
  }, []);

  useEffect(() => {
    const loadTree = async () => {
      setLoading(true);
      try {
        const tree = await getIndustryTree(true);
        const flat = flattenTree(tree);
        setTreeData(flat);
      } catch {
        message.error("加载行业树失败");
      } finally {
        setLoading(false);
      }
    };
    loadTree();
  }, [flattenTree]);

  // 当选择变化时，加载默认评分项
  const scoringItemsRef = useRef(onScoringItemsChange);
  useEffect(() => {
    scoringItemsRef.current = onScoringItemsChange;
  }, [onScoringItemsChange]);

  useEffect(() => {
    if (value && onScoringItemsChange) {
      getIndustryScoringItems(value)
        .then(res => scoringItemsRef.current?.(res.scoring_items || []))
        .catch(() => scoringItemsRef.current?.([]));
    } else if (!value) {
      onScoringItemsChange?.([]);
    }
  }, [value, onScoringItemsChange]);

  return (
    <div style={{ width: "100%" }}>
      {loading ? (
        <div style={{ textAlign: "center", padding: 20 }}>
          <Spin size="small" />
        </div>
      ) : treeData.length > 0 ? (
        <TreeSelect
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          allowClear={allowClear}
          disabled={disabled}
          showSearch={showSearch}
          treeData={treeData}
          treeDefaultExpandAll
          style={{ maxHeight: 400, overflow: "auto" }}
          treeNodeFilterProp="label"
        />
      ) : (
        <Empty description="暂无行业分类数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      )}
    </div>
  );
}
