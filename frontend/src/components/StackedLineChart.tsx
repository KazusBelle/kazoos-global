import { useMemo } from "react";

type Point = { ts: number; value: number | null };

export type ChartMarker = {
  ts: number;
  color: string;       // CSS color
  label: string;       // tooltip text
  kind?: "up" | "down" | "dot";
};

type Props = {
  price?: Point[];           // optional — when omitted the chart shows the metric only
  metric: Point[];
  metricLabel: string;
  height?: number;
  markers?: ChartMarker[];
};

const PAD_X = 56;
const PAD_TOP = 22;
const PAD_BOTTOM = 22;
const PRICE_COLOR = "#E3D02D";       // yellow
const METRIC_COLOR = "#4ea3ff";      // blue
const METRIC_FILL = "rgba(78, 163, 255, 0.10)";
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
    return [x, y, p.ts] as const;
  });

  // Gap-break: split the path whenever the time gap between consecutive
  // samples is much larger than the typical sampling cadence. Without
  // this a WS-metric that's only collected while the symbol is active
  // would draw long horizontal "fake bridges" across the dead windows
  // between sessions.
  let gapThreshold = Number.POSITIVE_INFINITY;
  if (inner.length >= 4) {
    const gaps: number[] = [];
    for (let i = 1; i < inner.length; i++) {
      gaps.push(inner[i].ts - inner[i - 1].ts);
    }
    gaps.sort((a, b) => a - b);
    const median = gaps[Math.floor(gaps.length / 2)] || 0;
    // 4× median is a reasonable line-break threshold — small jitter passes,
    // genuine multi-cycle absence breaks.
    gapThreshold = Math.max(median * 4, 30_000);
  }

  const baseY = height - PAD_BOTTOM;
  let d = "";

  if (!fill) {
    // Stroke mode — break the line on big gaps with a new M command.
    d = `M${xy[0][0].toFixed(2)},${xy[0][1].toFixed(2)}`;
    for (let i = 1; i < xy.length; i++) {
      const cmd = xy[i][2] - xy[i - 1][2] > gapThreshold ? "M" : "L";
      d += ` ${cmd}${xy[i][0].toFixed(2)},${xy[i][1].toFixed(2)}`;
    }
    return d;
  }

  // Fill mode — emit one closed polygon per contiguous segment so each
  // gap shows as a true blank, not as a connected polygon.
  let segStart = 0;
  for (let i = 1; i <= xy.length; i++) {
    const isBreak = i === xy.length || xy[i][2] - xy[i - 1][2] > gapThreshold;
    if (!isBreak) continue;
    const segEnd = i - 1;
    if (segEnd > segStart) {
      d += `M${xy[segStart][0].toFixed(2)},${baseY.toFixed(2)} `;
      d += `L${xy[segStart][0].toFixed(2)},${xy[segStart][1].toFixed(2)}`;
      for (let j = segStart + 1; j <= segEnd; j++) {
        d += ` L${xy[j][0].toFixed(2)},${xy[j][1].toFixed(2)}`;
      }
      d += ` L${xy[segEnd][0].toFixed(2)},${baseY.toFixed(2)} Z `;
    }
    segStart = i;
  }
  return d;
}

export function StackedLineChart({
  price,
  metric,
  metricLabel,
  height = 240,
  markers,
}: Props) {
  const width = 880;
  const priceSeries = price ?? [];
  const hasPrice = priceSeries.length > 0;
  // Unique filter id per instance so multiple charts don't share glow defs.
  const glowId = useMemo(
    () => `kz-glow-${Math.random().toString(36).slice(2, 9)}`,
    [],
  );

  const { xMin, xMax, priceMin, priceMax, metricMin, metricMax, empty } =
    useMemo(() => {
      const all = [...priceSeries, ...metric];
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
      const priceVals = priceSeries.map((p) => p.value).filter((v): v is number => v != null);
      const metricVals = metric.map((p) => p.value).filter((v): v is number => v != null);
      const padBand = (lo: number, hi: number) => {
        if (lo === hi) {
          const eps = Math.max(1e-9, Math.abs(lo) * 0.01);
          return [lo - eps, hi + eps];
        }
        const span = hi - lo;
        // 4% breathing room above/below — small enough that signed
        // metrics like OBI (natural [-1, +1]) don't get axis labels
        // visibly outside their actual bounds.
        return [lo - span * 0.04, hi + span * 0.04];
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
    }, [priceSeries, metric]);

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

  const priceFill = hasPrice
    ? buildPath(priceSeries, width, height, xMin, xMax, priceMin, priceMax, false)
    : "";
  const metricFill = buildPath(metric, width, height, xMin, xMax, metricMin, metricMax, true);
  const metricLine = buildPath(metric, width, height, xMin, xMax, metricMin, metricMax, false);

  const yTicks = [0, 0.5, 1].map((t) => ({
    y: PAD_TOP + t * (height - PAD_TOP - PAD_BOTTOM),
    price: priceMax - t * (priceMax - priceMin),
    metric: metricMax - t * (metricMax - metricMin),
  }));

  return (
    <div className="rounded-xl border border-border bg-bg/40 overflow-hidden">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto block">
        <defs>
          {/* Soft glow — Gaussian blur fed back over the original stroke
              so the line looks like it's emitting light. */}
          <filter id={glowId} x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="2.4" result="b1" />
            <feGaussianBlur in="SourceGraphic" stdDeviation="0.8" result="b2" />
            <feMerge>
              <feMergeNode in="b1" />
              <feMergeNode in="b2" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

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

        {metricFill && <path d={metricFill} fill={METRIC_FILL} stroke="none" />}
        {metricLine && (
          <path
            d={metricLine}
            fill="none"
            stroke={METRIC_COLOR}
            strokeWidth={1.6}
            strokeLinejoin="round"
            strokeLinecap="round"
            filter={`url(#${glowId})`}
          />
        )}
        {hasPrice && priceFill && (
          <path
            d={priceFill}
            fill="none"
            stroke={PRICE_COLOR}
            strokeWidth={1.6}
            strokeLinejoin="round"
            strokeLinecap="round"
            filter={`url(#${glowId})`}
          />
        )}

        {/* Event markers — small triangles/dots pinned to the metric line.
            Anchored at the nearest metric sample so the marker visibly
            sits on the curve, not floating above an empty x-axis tick. */}
        {markers && markers.length > 0 && (() => {
          const valuesByTs = new Map<number, number>();
          for (const p of metric) {
            if (p.value != null && Number.isFinite(p.value)) valuesByTs.set(p.ts, p.value);
          }
          const orderedTs = Array.from(valuesByTs.keys()).sort((a, b) => a - b);
          if (orderedTs.length === 0 || xMax === xMin || metricMax === metricMin) return null;
          const innerW = width - 2 * PAD_X;
          const innerH = height - PAD_TOP - PAD_BOTTOM;
          return (
            <g>
              {markers.map((m, i) => {
                // Snap to closest available sample so the glyph rides the line.
                let bestTs = orderedTs[0];
                let bestDt = Math.abs(orderedTs[0] - m.ts);
                for (const t of orderedTs) {
                  const dt = Math.abs(t - m.ts);
                  if (dt < bestDt) { bestDt = dt; bestTs = t; }
                }
                const v = valuesByTs.get(bestTs)!;
                const x = PAD_X + ((bestTs - xMin) / (xMax - xMin)) * innerW;
                const y = PAD_TOP + (1 - (v - metricMin) / (metricMax - metricMin)) * innerH;
                const kind = m.kind ?? "dot";
                if (kind === "up") {
                  return (
                    <g key={i}>
                      <title>{m.label}</title>
                      <polygon
                        points={`${x},${y - 7} ${x - 4.5},${y - 1} ${x + 4.5},${y - 1}`}
                        fill={m.color}
                        stroke="#0d0e11"
                        strokeWidth={0.5}
                      />
                    </g>
                  );
                }
                if (kind === "down") {
                  return (
                    <g key={i}>
                      <title>{m.label}</title>
                      <polygon
                        points={`${x},${y + 7} ${x - 4.5},${y + 1} ${x + 4.5},${y + 1}`}
                        fill={m.color}
                        stroke="#0d0e11"
                        strokeWidth={0.5}
                      />
                    </g>
                  );
                }
                return (
                  <g key={i}>
                    <title>{m.label}</title>
                    <circle cx={x} cy={y} r={3.5} fill={m.color} stroke="#0d0e11" strokeWidth={0.5} />
                  </g>
                );
              })}
            </g>
          );
        })()}

        {yTicks.map((t, i) => (
          <g key={`tx${i}`}>
            {hasPrice && (
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
            )}
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

        <g>
          {hasPrice && (
            <>
              <rect x={PAD_X} y={4} width={10} height={2} fill={PRICE_COLOR} />
              <text x={PAD_X + 14} y={9} fontSize="9" fill={AXIS_TEXT}>
                Price
              </text>
            </>
          )}
          <rect
            x={hasPrice ? PAD_X + 70 : PAD_X}
            y={4}
            width={10}
            height={2}
            fill={METRIC_COLOR}
          />
          <text
            x={(hasPrice ? PAD_X + 70 : PAD_X) + 14}
            y={9}
            fontSize="9"
            fill={AXIS_TEXT}
          >
            {metricLabel}
          </text>
        </g>
      </svg>
    </div>
  );
}
