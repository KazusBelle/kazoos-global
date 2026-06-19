import { type RuntimeState, type FlowStatus } from "../lib/api";

// Observation Period status bar (Stage 2). Read-only operator visibility.
// derived_status RED authority is the value path (Stage 1) — event-like flows
// and failure_boundary are soft (YELLOW at most). On fetch error the bar is RED
// ("status unavailable"), never green.
//
// Presentational only: runtime-state is polled once in the parent (Liquidity)
// and passed in as { data, loading, error } so the banner and the header
// WsStatusPill always read the same snapshot (no divergent caches).

const COLORS: Record<FlowStatus, { fg: string; border: string; bg: string }> = {
  GREEN: { fg: "rgba(82, 185, 122, 0.95)", border: "rgba(82, 185, 122, 0.5)", bg: "rgba(82, 185, 122, 0.08)" },
  YELLOW: { fg: "rgba(227, 180, 87, 0.95)", border: "rgba(227, 180, 87, 0.5)", bg: "rgba(227, 180, 87, 0.08)" },
  RED: { fg: "rgba(214, 105, 105, 0.95)", border: "rgba(214, 105, 105, 0.5)", bg: "rgba(214, 105, 105, 0.10)" },
};

function Chip({ status, children, title }: { status: FlowStatus; children: React.ReactNode; title?: string }) {
  const c = COLORS[status];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] whitespace-nowrap"
      style={{ color: c.fg, borderColor: c.border, background: c.bg }}
      title={title}
    >
      {children}
    </span>
  );
}

function fmtAge(s: number | null): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

const FLOW_LABELS: Record<string, string> = {
  bursts: "bursts",
  resiliency: "resil",
  exec_validation: "exec",
  liq_stress: "stress",
};

export function ObservationBanner({
  data,
  loading,
  error,
}: {
  data: RuntimeState | null;
  loading: boolean;
  error: boolean;
}) {
  // Error → RED bar, never green.
  if (error || (!loading && !data)) {
    const c = COLORS.RED;
    return (
      <div
        className="rounded-lg border px-3 py-1.5 text-[10px] uppercase tracking-[0.18em]"
        style={{ color: c.fg, borderColor: c.border, background: c.bg }}
      >
        continuity status (derived): status unavailable
      </div>
    );
  }
  // Loading → neutral skeleton, never green.
  if (loading || !data) {
    return (
      <div className="rounded-lg border border-border bg-panel px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] text-muted">
        loading status…
      </div>
    );
  }

  const c = COLORS[data.derived_status];
  const elapsedH = data.elapsed_s / 3600;
  const subsOk = data.subscribed_count != null && data.subscribed_count >= data.baseline.length;
  const fbHealthy = data.failure_boundary === "HEALTHY";

  // Compact per-symbol value-path summary (age/dv) for the tooltip.
  const vpTip = data.value_path.per_symbol
    .map((p) => `${p.symbol}: ${p.age_s == null ? "—" : Math.round(p.age_s) + "s"} / dv${p.dv}`)
    .join("\n");

  return (
    <div
      className="rounded-lg border px-3 py-2 flex flex-wrap items-center gap-2"
      style={{ borderColor: c.border, background: c.bg }}
    >
      <span className="text-[9px] uppercase tracking-[0.2em]" style={{ color: c.fg }}>
        continuity status (derived)
      </span>
      <Chip status={data.derived_status}>{data.derived_status}</Chip>

      <span className="text-[9px] uppercase tracking-[0.16em] text-muted" title={`T0_new ${data.t0_new}`}>
        T0 +{elapsedH.toFixed(1)}h · →3d {data.hrs_to_3d.toFixed(1)}h · →7d {data.hrs_to_7d.toFixed(1)}h
      </span>

      <Chip status={subsOk ? "GREEN" : "RED"}>
        subs {data.subscribed_count ?? "?"}/{data.baseline.length}
      </Chip>

      <Chip status={data.value_path.ok ? "GREEN" : "RED"} title={vpTip}>
        value-path {data.value_path.ok ? "✓" : "×"}
      </Chip>

      {(["bursts", "resiliency", "exec_validation", "liq_stress"] as const).map((name) => {
        const f = data.flows[name];
        if (!f) return null;
        return (
          <Chip key={name} status={f.status} title={`${name}: ${fmtAge(f.latest_age_s)} old`}>
            {FLOW_LABELS[name]} {fmtAge(f.latest_age_s)}
          </Chip>
        );
      })}

      <Chip
        status={fbHealthy ? "GREEN" : "YELLOW"}
        title={`failure_boundary (informational) · health age ${fmtAge(data.health_age_s)}`}
      >
        fb: {data.failure_boundary.toLowerCase()}
      </Chip>
    </div>
  );
}
