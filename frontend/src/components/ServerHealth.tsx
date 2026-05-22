import { useEffect, useMemo, useState } from "react";
import { getServerMetrics, type ServerMetricPoint } from "../lib/api";

const RANGES = [1, 6, 24, 168] as const;
const POLL_MS = 60_000;

type MetricKey = "stress" | "cpu_percent" | "memory_percent" | "load_1m" | "disk_percent";

const METRICS: { key: MetricKey; label: string; color: string; max?: number; suffix?: string }[] = [
  { key: "stress", label: "Stress", color: "#d68b8b", max: 100, suffix: "%" },
  { key: "cpu_percent", label: "CPU", color: "#d6b46c", max: 100, suffix: "%" },
  { key: "memory_percent", label: "RAM", color: "#7fb678", max: 100, suffix: "%" },
  { key: "load_1m", label: "Load 1m", color: "#8ba6d6" },
  { key: "disk_percent", label: "Disk", color: "#a78bfa", max: 100, suffix: "%" },
];

function fmt(value: number | null | undefined, suffix = "") {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value.toFixed(value >= 10 ? 0 : 1)}${suffix}`;
}

function timeLabel(raw: string) {
  return new Date(raw).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function metricValue(point: ServerMetricPoint, key: MetricKey) {
  if (key !== "stress") return point[key];
  const cpu = point.cpu_percent ?? 0;
  const ram = point.memory_percent ?? 0;
  const disk = point.disk_percent ?? 0;
  const loadPressure = Math.min(100, ((point.load_1m ?? 0) / 4) * 100);
  return Math.max(cpu, ram * 0.85, disk * 0.65, loadPressure);
}

function MetricChart({
  points,
  metric,
}: {
  points: ServerMetricPoint[];
  metric: (typeof METRICS)[number];
}) {
  const values = points
    .map((p) => metricValue(p, metric.key))
    .filter((v): v is number => v != null && Number.isFinite(v));
  const latest = points.length > 0 ? metricValue(points[points.length - 1], metric.key) : null;
  const maxValue = Math.max(metric.max ?? 0, ...values, metric.key === "load_1m" ? 4 : 0);
  const width = 720;
  const height = 160;
  const pad = 18;
  const usableW = width - pad * 2;
  const usableH = height - pad * 2;
  const path = points
    .map((p, idx) => {
      const raw = metricValue(p, metric.key);
      if (raw == null || !Number.isFinite(raw)) return null;
      const x = pad + (points.length <= 1 ? 0 : (idx / (points.length - 1)) * usableW);
      const y = pad + usableH - (Math.max(0, raw) / Math.max(1, maxValue)) * usableH;
      return `${idx === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .filter(Boolean)
    .join(" ");

  return (
    <section className="rounded-lg border border-border bg-panel p-4 min-w-0">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted">{metric.label}</div>
          <div className="mt-1 font-mono text-2xl text-zinc-100">{fmt(latest, metric.suffix)}</div>
        </div>
        <div className="text-right text-[10px] uppercase tracking-widest text-muted">
          Peak{" "}
          <span className="font-mono text-zinc-300">{fmt(values.length ? Math.max(...values) : null, metric.suffix)}</span>
        </div>
      </div>

      <div className="mt-4 h-40 w-full overflow-hidden">
        {points.length < 2 ? (
          <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted">
            waiting for history
          </div>
        ) : (
          <svg viewBox={`0 0 ${width} ${height}`} className="h-full w-full" preserveAspectRatio="none">
            <path d={`M ${pad} ${height - pad} H ${width - pad}`} stroke="#27272a" strokeWidth="1" />
            <path d={`M ${pad} ${pad} H ${width - pad}`} stroke="#27272a" strokeWidth="1" opacity="0.7" />
            <path d={path} fill="none" stroke={metric.color} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </div>

      {points.length > 0 && (
        <div className="mt-2 flex justify-between text-[10px] font-mono text-muted">
          <span>{timeLabel(points[0].created_at)}</span>
          <span>{timeLabel(points[points.length - 1].created_at)}</span>
        </div>
      )}
    </section>
  );
}

export function ServerHealth() {
  const [rangeHours, setRangeHours] = useState<(typeof RANGES)[number]>(24);
  const [points, setPoints] = useState<ServerMetricPoint[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const data = await getServerMetrics(rangeHours);
      setPoints(data.points);
      setError(null);
    } catch (err: any) {
      setError(err?.message ?? "failed to load server metrics");
    }
  }

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangeHours]);

  const latest = points.length > 0 ? points[points.length - 1] : null;
  const summary = useMemo(
    () => [
      ["CPU", fmt(latest?.cpu_percent, "%")],
      ["RAM", fmt(latest?.memory_percent, "%")],
      ["Swap", fmt(latest?.swap_percent, "%")],
      ["Conn", fmt(latest?.net_connections)],
    ],
    [latest]
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-[0.18em] uppercase text-zinc-100">
            Server Stress
          </h1>
          <div className="mt-1 text-[11px] uppercase tracking-widest text-muted">
            {points.length} samples
          </div>
        </div>
        <div className="inline-flex rounded-lg border border-border overflow-hidden">
          {RANGES.map((h) => (
            <button
              key={h}
              onClick={() => setRangeHours(h)}
              className={`px-3 py-2 text-[10px] uppercase tracking-widest ${
                rangeHours === h ? "bg-accent text-black" : "text-muted hover:text-zinc-200"
              }`}
            >
              {h === 168 ? "7d" : `${h}h`}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-900/50 bg-red-950/20 px-4 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {summary.map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-panel px-4 py-3">
            <div className="text-[10px] uppercase tracking-widest text-muted">{label}</div>
            <div className="mt-1 font-mono text-xl text-zinc-100">{value}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {METRICS.map((metric) => (
          <MetricChart key={metric.key} points={points} metric={metric} />
        ))}
      </div>
    </div>
  );
}
