import React, { useMemo } from 'react';

/**
 * Unified single-chain flowchart component.
 *
 * Visual spec (dual vertical / horizontal mode):
 *   - node bg      #f0edff
 *   - node border  #a89aff
 *   - node text    #333333
 *   - arrow        #333333
 *
 * Direction rules of thumb:
 *   - <= 8 nodes  -> horizontal
 *   -  > 8 nodes  -> vertical
 * Pass `direction="auto"` to let the component pick automatically.
 */
export interface SingleChainFlowChartProps {
  /** Ordered list of node labels. */
  steps: string[];
  /** Optional title (only shown when `showTitle` is true). */
  title?: string;
  /** Direction: 'vertical' (top-down), 'horizontal' (left-right), or 'auto'. */
  direction?: 'vertical' | 'horizontal' | 'auto';
  /** Node width in px. Defaults: vertical 280 / horizontal 200. */
  nodeWidth?: number;
  /** Node minimum height in px. Default 80. */
  nodeHeight?: number;
  /** Gap between nodes (also arrow length). Default 40. */
  gap?: number;
  /** Font size in px. Default 15. */
  fontSize?: number;
  /** Show a title bar above the flow. */
  showTitle?: boolean;
  /** Extra wrapper style. */
  style?: React.CSSProperties;
  /** Extra wrapper class. */
  className?: string;
}

const NODE_BG = '#f0edff';
const NODE_BORDER = '#a89aff';
const NODE_TEXT = '#333333';
const ARROW_COLOR = '#333333';

const DEFAULT_WIDTH_VERTICAL = 280;
const DEFAULT_WIDTH_HORIZONTAL = 200;
const DEFAULT_HEIGHT = 80;
const DEFAULT_GAP = 40;
const DEFAULT_FONT = 15;

export function pickDirection(
  stepCount: number,
  direction: 'vertical' | 'horizontal' | 'auto' = 'auto',
): 'vertical' | 'horizontal' {
  if (direction === 'auto') {
    return stepCount <= 8 ? 'horizontal' : 'vertical';
  }
  return direction;
}

export const SingleChainFlowChart: React.FC<SingleChainFlowChartProps> = ({
  steps,
  title,
  direction = 'auto',
  nodeWidth,
  nodeHeight = DEFAULT_HEIGHT,
  gap = DEFAULT_GAP,
  fontSize = DEFAULT_FONT,
  showTitle = false,
  style,
  className,
}) => {
  const resolvedDirection = pickDirection(steps.length, direction);
  const isVertical = resolvedDirection === 'vertical';

  const effectiveNodeWidth =
    nodeWidth ?? (isVertical ? DEFAULT_WIDTH_VERTICAL : DEFAULT_WIDTH_HORIZONTAL);

  const wrapperStyle: React.CSSProperties = useMemo(
    () => ({
      display: 'inline-flex',
      flexDirection: isVertical ? 'column' : 'row',
      alignItems: 'center',
      padding: 20,
      fontFamily: '"Microsoft YaHei", "PingFang SC", Arial, sans-serif',
      ...style,
    }),
    [isVertical, style],
  );

  const nodeStyle: React.CSSProperties = useMemo(
    () => ({
      boxSizing: 'border-box',
      width: effectiveNodeWidth,
      minHeight: nodeHeight,
      background: NODE_BG,
      border: `1px solid ${NODE_BORDER}`,
      borderRadius: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '16px 24px',
      fontSize,
      color: NODE_TEXT,
      letterSpacing: 0.5,
      textAlign: 'center',
      lineHeight: 1.4,
      whiteSpace: 'pre-wrap',
      wordBreak: 'break-word',
      flexShrink: 0,
      transition: 'box-shadow 0.2s',
    }),
    [effectiveNodeWidth, nodeHeight, fontSize],
  );

  const arrowContainerStyle: React.CSSProperties = useMemo(
    () => ({
      width: isVertical ? '100%' : gap,
      height: isVertical ? gap : '100%',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      flexShrink: 0,
    }),
    [isVertical, gap],
  );

  const arrowSvg = useMemo(() => {
    if (isVertical) {
      return (
        <svg width={2} height={gap} style={{ overflow: 'visible' }} xmlns="http://www.w3.org/2000/svg">
          <line x1={0} y1={0} x2={0} y2={gap - 10} stroke={ARROW_COLOR} strokeWidth={1.5} strokeLinecap="round" />
          <polygon points={`0,${gap} -5,${gap - 10} 5,${gap - 10}`} fill={ARROW_COLOR} />
        </svg>
      );
    }
    return (
      <svg width={gap} height={2} style={{ overflow: 'visible' }} xmlns="http://www.w3.org/2000/svg">
        <line x1={0} y1={0} x2={gap - 10} y2={0} stroke={ARROW_COLOR} strokeWidth={1.5} strokeLinecap="round" />
        <polygon points={`${gap},0 ${gap - 10},-5 ${gap - 10},5`} fill={ARROW_COLOR} />
      </svg>
    );
  }, [isVertical, gap]);

  return (
    <div className={className} data-direction={resolvedDirection}>
      {showTitle && title ? (
        <div
          style={{
            fontFamily: '"Microsoft YaHei", "PingFang SC", Arial, sans-serif',
            fontSize: 16,
            fontWeight: 600,
            color: NODE_TEXT,
            marginBottom: 8,
            textAlign: 'center',
          }}
        >
          {title}
        </div>
      ) : null}
      <div style={wrapperStyle} role="img" aria-label={title ?? 'flowchart'}>
        {steps.map((text, i) => (
          <React.Fragment key={i}>
            <div
              style={nodeStyle}
              onMouseEnter={e => {
                (e.currentTarget as HTMLDivElement).style.boxShadow =
                  '0 2px 8px rgba(168, 154, 255, 0.3)';
              }}
              onMouseLeave={e => {
                (e.currentTarget as HTMLDivElement).style.boxShadow = '';
              }}
            >
              {text}
            </div>
            {i < steps.length - 1 ? (
              <div style={arrowContainerStyle}>{arrowSvg}</div>
            ) : null}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
};

export default SingleChainFlowChart;
