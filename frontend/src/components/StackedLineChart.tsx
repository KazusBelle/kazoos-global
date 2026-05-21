import { useMemo } from "react";

type Point = { ts: number; value: number | null };

type Props = {
  price: Point[];
  metric: Point[];
  metricLabel: string;
  height?: number;
};

const PAD_X = 56;
const PAD_TOP = 22;
const PAD_BOTTOM = 22;
const PRICE_COLOR = "#E3D02D";       // yellow
const METRIC_COLOR = "#4ea3ff";      // blue
const METRIC_FILL = "rgba(78, 163, 255, 0.12)";
const GRID = "#1a1c22";
const AXIS_TEXT = "#5a5f6b";

function niceNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  if (abs >= 100) return n.toFixed(2);
  if (abs >= 1) return n.toFixed(3);
  if (abs >= 0.01) return n.toFixed(5);
  return n.toPrecision(3);
}

function buildPath(
  pts: Point[],
  width: number,
  height: number,
  xMin: number,
  xMax: number,
  yMin: number,
  yMax: number,
  fill: boolean = false,
): string {
  if (pts.length === 0 || xMax === xMin || yMax === yMin) return "";
  const xRange = xMax - xMin;
  const yRange = yMax - yMin;
  const inner = pts.filter((p) => p.value != null);
  if (inner.length === 0) return "";

  const xy = inner.map((p) => {
    const x = PAD_X + ((p.ts - xMin) / xRange) * (width - 2 * PAD_X);
    const y =
      PAD_TOP + (1 - ((p.value as number) - yMin) / yRange) * (height - PAD_TOP - PAD_BOTTOM);
    return [x, y] as const;
  });

  let d = `M${xy[0][0].toFixed(2)},${xy[0][1].toFixed(2)}`;
  for (let i = 1; i < xy.length; i++) {
    d += ` L${xy[i][0].toFixed(2)},${xy[i][1].toFixed(2)}`;
  }
  if (fill) {
    const baseY = height - PAD_BOTTOM;
    d += ` L${xy[xy.length - 1][0].toFixed(2)},${baseY} L${xy[0][0].toFixed(2)},${baseY} Z`;
  }
  return d;
}

export function StackedLineChart({
  price,
  metric,
  metricLabel,
  height = 280,
}: Props) {
  const width = 880;

  const { xMin, xMax, priceMin, priceMax, metricMin, metricMax, empty } =
    useMemo(() => {
      const all = [...price, ...metric];
      if (all.length === 0) {
        return {
          xMin: 0,
          xMax: 1,
          priceMin: 0,
          priceMax: 1,
          metricMin: 0,
          metricMax: 1,
          empty: true,
        };
      }
      const xs = all.map((p) => p.ts);
      const priceVals = price.map((p) => p.value).filter((v): v is number => v != null);
      const metricVals = metric.map((p) => p.value).filter((v): v is number => v != null);
      const padBand = (lo: number, hi: number) => {
        if (lo === hi) {
          const eps = Math.max(1e-9, Math.abs(lo) * 0.01);
          return [lo - eps, hi + eps];
        }
        const span = hi - lo;
        return [lo - span * 0.08, hi + span * 0.08];
      };
      const [pLo, pHi] =
        priceVals.length > 0 ? padBand(Math.min(...priceVals), Math.max(...priceVals)) : [0, 1];
      const [mLo, mHi] =
        metricVals.length > 0
          ? padBand(Math.min(...metricVals), Math.max(...metricVals))
          : [0, 1];
      return {
        xMin: Math.min(...xs),
        xMax: Math.max(...xs),
        priceMin: pLo,
        priceMax: pHi,
        metricMin: mLo,
        metricMax: mHi,
        empty: priceVals.length === 0 && metricVals.length === 0,
      };
    }, [price, metric]);

  if (empty) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-border bg-bg/60 text-xs text-muted"
        style={{ height }}
      >
        No data in this window yet.
      </div>
    );
  }

  const priceFill = buildPath(price, width, height, xMin, xMax, priceMin, priceMax, false);
  const metricFill = buildPath(metric, width, height, xMin, xMax, metricMin, metricMax, true);
  const metricLine = buildPath(metric, width, height, xMin, xMax, metricMin, metricMax, false);

  // Y-axis labels: left = price (yellow), right = metric (blue)
  const yTicks = [0, 0.5, 1].map((t) => ({
    y: PAD_TOP + t * (height - PAD_TOP - PAD_BOTTOM),
    price: priceMax - t * (priceMax - priceMin),
    metric: metricMax - t * (metricMax - metricMin),
  }));

  return (
    <div className="rounded-xl border border-border bg-bg/40 overflow-hidden">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto block">
        {/* Horizontal gridlines */}
        {yTicks.map((t, i) => (
          <line
            key={i}
            x1={PAD_X}
            x2={width - PAD_X}
            y1={t.y}
            y2={t.y}
            stroke={GRID}
            strokeWidth={1}
          />
        ))}
        {/* Metric fill (background highlight) */}
        {metricFill && <path d={metricFill} fill={METRIC_FILL} stroke="none" />}
        {/* Metric line */}
        {metricLine && (
          <path
            d={metricLine}
            fill="none"
            stroke={METRIC_COLOR}
            strokeWidth={1.6}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}
        {/* Price line */}
        {priceFill && (
          <path
            d={priceFill}
            fill="none"
            stroke={PRICE_COLOR}
            strokeWidth={1.4}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}
        {/* Y-axis ticks */}
        {yTicks.map((t, i) => (
          <g key={`tx${i}`}>
            <text
              x={PAD_X - 6}
              y={t.y + 3}
              textAnchor="end"
              fontSize="9"
              fill={PRICE_COLOR}
              opacity={0.8}
            >
              {niceNum(t.price)}
            </text>
            <text
              x={width - PAD_X + 6}
              y={t.y + 3}
              textAnchor="start"
              fontSize="9"
              fill={METRIC_COLOR}
              opacity={0.8}
            >
              {niceNum(t.metric)}
            </text>
          </g>
        ))}
        {/* Legend */}
        <g>
          <rect x={PAD_X} y={4} width={10} height={2} fill={PRICE_COLOR} />
          <text x={PAD_X + 14} y={9} fontSize="9" fill={AXIS_TEXT}>
            Price
          </text>
          <rect x={PAD_X + 70} y={4} width={10} height={2} fill={METRIC_COLOR} />
          <text x={PAD_X + 84} y={9} fontSize="9" fill={AXIS_TEXT}>
            {metricLabel}
          </text>
        </g>
      </svg>
    </div>
  );
}
