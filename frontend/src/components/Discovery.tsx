/**
 * Phase-14 Discovery dashboard.
 *
 * Autonomous pattern discovery + crisis archetypes + hidden regimes +
 * symbol-level propagation + evolutionary trends + memory abstraction +
 * intelligence forecasting + adaptation recommendations.
 *
 * Read-only over the data the previous phases already accumulate.
 * Recommendations on this page are descriptive — nothing auto-applies.
 */

import { useEffect, useState } from "react";
import {
  getAdaptationRecommendations,
  getAdaptationState,
  getAdaptedRecommendations,
  getCausalPropagation,
  getCrisisArchetypes,
  getCrisisGenesis,
  getMarketStateTransitions,
  getNarrativeCausality,
  getOperatorDigest,
  getOperatorEscalationHistory,
  getOperatorPriorities,
  getStructuralDependencies,
  postOperatorAck,
  getEvolutionaryBehavior,
  getHiddenRegimes,
  getIntelligenceForecast,
  getMemoryAbstraction,
  getPatternDiscovery,
  getPropagation,
  getSanityAudit,
  type AdaptationOut,
  type AdaptationState,
  type CausalEdge,
  type CausalPropagation,
  type OperatorAckAction,
  type OperatorDigest,
  type OperatorEscalationHistory,
  type CausalRole,
  type CausalVerdict,
  type CrisisArchetypes,
  type CrisisGenesis,
  type CrisisGenesisStatus,
  type CrisisGenesisVerdict,
  type MarketStateTransitions,
  type NarrativeCausality,
  type OperatorEscalation,
  type OperatorLifecycle,
  type OperatorPriorities,
  type OperatorPriorityItem,
  type TransitionVerdict,
  type StructuralDependencies,
  type DataQuality,
  type EvolutionaryBehavior,
  type HiddenRegimes,
  type IntelligenceForecast,
  type MemoryAbstraction,
  type PatternDiscovery,
  type Propagation,
  type SanityAudit,
} from "../lib/api";

const REFRESH_MS = 60_000;

export function Discovery() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">DISC</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          autonomous discovery · archetypes · hidden regimes · propagation
        </div>
      </div>

      <OperatorPrioritiesPanel />
      <SanityBanner />
      <AdaptationStatePanel />
      <CrisisGenesisPanel />
      <NarrativeCausalityPanel />
      <PatternDiscoveryPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CrisisArchetypesPanel />
        <HiddenRegimesPanel />
      </div>
      <PropagationPanel />
      <CausalPropagationPanel />
      <StructuralDependenciesPanel />
      <MarketStateTransitionsPanel />
      <EvolutionaryPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MemoryAbstractionPanel />
        <IntelligenceForecastPanel />
      </div>
      <AdaptationPanel />
    </div>
  );
}

function Panel({
  title, subtitle, toolbar, children,
}: { title: string; subtitle?: string; toolbar?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-panel">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <span className="text-[11px] uppercase tracking-[0.22em] text-zinc-100">{title}</span>
          {subtitle && <span className="text-[10px] uppercase tracking-[0.2em] text-muted">{subtitle}</span>}
        </div>
        {toolbar}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function TertileChip({ v }: { v: "low" | "mid" | "high" | "na" }) {
  const colors: Record<typeof v, string> = {
    low: "rgba(82, 185, 122, 0.95)",
    mid: "rgba(140, 170, 235, 0.95)",
    high: "rgba(214, 105, 105, 0.95)",
    na: "rgba(90, 95, 107, 0.85)",
  };
  const color = colors[v];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$/, "0.5)"),
        background: color.replace(/0\.95\)$/, "0.10)"),
      }}
    >
      {v}
    </span>
  );
}

function fmtTs(ts: number): string {
  return new Date(ts).toISOString().replace("T", " ").slice(0, 16);
}

const DATA_QUALITY_COLOR: Record<DataQuality, string> = {
  HIGH: "rgba(82, 185, 122, 0.95)",
  MEDIUM: "rgba(140, 170, 235, 0.95)",
  LOW: "rgba(227, 180, 87, 0.95)",
  INSUFFICIENT: "rgba(140, 140, 140, 0.85)",
};

const DATA_QUALITY_EXPLAIN: Record<DataQuality, string> = {
  HIGH: "Enough data to trust outputs.",
  MEDIUM: "Borderline — treat conclusions as provisional.",
  LOW: "Sparse data — outputs are candidates, not findings.",
  INSUFFICIENT: "Not enough data — accumulating.",
};

function DataQualityChip({ q }: { q: DataQuality | undefined }) {
  if (!q) return null;
  const color = DATA_QUALITY_COLOR[q];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$|0\.85\)$/, "0.5)"),
        background: color.replace(/0\.95\)$|0\.85\)$/, "0.10)"),
      }}
      title={`Sample-adequacy gate. ${DATA_QUALITY_EXPLAIN[q]}`}
    >
      {q}
    </span>
  );
}

// Diminished outer ring when data quality is poor — panel still works,
// but visually signals "don't trust this surface yet".
function dataQualityClass(q: DataQuality | undefined): string {
  if (q === "INSUFFICIENT") return "opacity-60";
  if (q === "LOW") return "opacity-80";
  return "";
}

// Short human-readable copy keyed by finding.kind — lets the banner say
// what the diagnostic actually means in one sentence, with all the
// thresholds/numbers staying in the tooltip.
const FINDING_HUMAN: Record<string, string> = {
  validation_collapse: "Alert outcomes are all marked as noise. Validation thresholds likely mis-set.",
  anomaly_inflation: "Anomaly recorder is writing more rows than usual — cooldowns may be too loose.",
  propagation_loop: "Symbol pairs lead each other equally — these are coincidence, not lead-lag relationships.",
  propagation_instability: "Propagation graph is dominated by weak edges; few connections are statistically meaningful.",
  forecast_overshoot: "Forecast pinned at boundary (0 or 100). Slope was extrapolated past what data supports.",
  pattern_explosion: "Too many patterns at current support threshold — likely artifacts of bucket density.",
  confidence_collapse: "Aggregate confidence across discovery surfaces is low. Treat outputs as exploratory.",
  regime_fragmentation_spike: "Engine state is jumping between more coordinated_states than usual.",
  unstable_clustering: "Hidden-regime clusters are mostly micro-clusters — not enough snapshots for stable structure.",
  adaptation_oscillation: "Adaptation recommender is producing conflicting actions on the same target.",
};

const FINDING_TREND_LABEL: Record<string, string> = {
  WORSENING: "getting worse",
  STABILIZING: "settling",
  CHRONIC: "long-standing",
  RECURRING: "persistent",
  TRANSIENT: "one-shot",
  NEW: "new",
};

const PROPAGATION_CONF_COLOR: Record<"HIGH" | "MEDIUM" | "LOW", string> = {
  HIGH: "rgba(82, 185, 122, 0.95)",
  MEDIUM: "rgba(140, 170, 235, 0.95)",
  LOW: "rgba(227, 180, 87, 0.95)",
};

function EdgeConfidenceChip({ c }: { c: "HIGH" | "MEDIUM" | "LOW" }) {
  const color = PROPAGATION_CONF_COLOR[c];
  return (
    <span
      className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$/, "0.5)"),
        background: color.replace(/0\.95\)$/, "0.10)"),
      }}
    >
      {c}
    </span>
  );
}

// ── Sanity audit banner ─────────────────────────────────────────────────

const SANITY_SEVERITY_COLOR: Record<"critical" | "warn" | "info", string> = {
  critical: "rgba(214, 75, 75, 0.95)",
  warn: "rgba(227, 180, 87, 0.95)",
  info: "rgba(140, 170, 235, 0.95)",
};

const SANITY_OVERALL_LABEL: Record<SanityAudit["overall_state"], string> = {
  CRITICAL: "engine integrity check failing",
  WARN: "engine integrity warnings",
  INFO: "engine notes",
  CLEAN: "engine sanity checks clean",
};

function SanityBanner() {
  const [data, setData] = useState<SanityAudit | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getSanityAudit()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (error) return null;
  if (!data) return null;

  if (data.overall_state === "CLEAN") {
    return (
      <div className="rounded-lg border border-border/40 bg-panel/40 px-3 py-1.5 text-[10px] uppercase tracking-[0.16em] text-muted flex items-center gap-2">
        <span style={{ color: "rgba(82, 185, 122, 0.85)" }}>●</span>
        <span>all {data.check_count} integrity checks clean</span>
      </div>
    );
  }

  const overallColor =
    data.overall_state === "CRITICAL" ? "rgba(214, 75, 75, 0.95)" :
    data.overall_state === "WARN" ? "rgba(227, 180, 87, 0.95)" :
    "rgba(140, 170, 235, 0.95)";

  return (
    <div
      className="rounded-lg border bg-panel/60 px-3 py-2.5"
      style={{ borderColor: overallColor.replace(/0\.95\)$/, "0.55)") }}
    >
      <div className="flex items-baseline gap-3 mb-2.5">
        <span className="text-[11px] uppercase tracking-[0.22em]" style={{ color: overallColor }}>
          ● {data.overall_state}
        </span>
        <span className="text-[10px] uppercase tracking-[0.2em] text-muted">
          {data.findings.length} of {data.check_count} checks tripped · worst severity {data.overall_score.toFixed(0)}/100
        </span>
      </div>
      <ul className="space-y-2">
        {data.findings.map((f, i) => {
          const sc = SANITY_SEVERITY_COLOR[f.severity];
          const trendColor =
            f.trend === "WORSENING" ? "text-[#d68b8b]"
              : f.trend === "STABILIZING" ? "text-[#52b97a]"
                : f.trend === "CHRONIC" ? "text-[#e3b457]"
                  : f.trend === "RECURRING" ? "text-[#8caaeb]"
                    : "text-muted";
          const human = FINDING_HUMAN[f.kind] ?? f.kind.replace(/_/g, " ");
          const trendLabel = FINDING_TREND_LABEL[f.trend] ?? f.trend.toLowerCase();
          const tip = [
            `category    ${f.category}`,
            `kind        ${f.kind}`,
            `value       ${f.metric_value.toFixed(1)} ${f.threshold_unit}`,
            `thresholds  info≥${f.info_threshold} warn≥${f.warn_threshold} crit≥${f.critical_threshold}`,
            `severity    ${f.severity_score.toFixed(0)}/100`,
            `trend       ${f.trend}`,
            ``,
            f.detail,
          ].join("\n");
          return (
            <li key={i} title={tip}>
              <div className="flex items-baseline gap-2 text-[11px]">
                <span
                  className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em] shrink-0 tabular-nums"
                  style={{
                    color: sc,
                    borderColor: sc.replace(/0\.95\)$/, "0.5)"),
                    background: sc.replace(/0\.95\)$/, "0.10)"),
                    minWidth: "3.4rem",
                    textAlign: "center",
                  }}
                >
                  {f.severity_score.toFixed(0)}
                </span>
                <span className="text-zinc-200 flex-1">{human}</span>
                <span className={`text-[9px] uppercase tracking-[0.14em] ${trendColor} shrink-0`}>
                  {trendLabel}
                </span>
              </div>
              <div className="text-[10px] text-muted font-mono pl-[3.9rem] mt-0.5 truncate">
                {f.detail}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ── Operator Priorities (Phase 17) ──────────────────────────────────────

const ESCALATION_COLOR: Record<OperatorEscalation, string> = {
  CRITICAL:  "rgba(214, 75, 75, 0.95)",
  IMPORTANT: "rgba(214, 139, 105, 0.95)",
  WATCH:     "rgba(227, 180, 87, 0.95)",
  NORMAL:    "rgba(140, 170, 235, 0.85)",
};

const LIFECYCLE_LABEL: Record<OperatorLifecycle, string> = {
  NEW:          "new",
  WORSENING:    "worsening",
  STABILIZING:  "stabilizing",
  PERSISTENT:   "persistent",
  RESOLVED:     "resolved",
};

const LIFECYCLE_COLOR: Record<OperatorLifecycle, string> = {
  NEW:          "rgba(140, 170, 235, 0.95)",
  WORSENING:    "rgba(214, 105, 105, 0.95)",
  STABILIZING:  "rgba(82, 185, 122, 0.95)",
  PERSISTENT:   "rgba(170, 170, 180, 0.85)",
  RESOLVED:     "rgba(120, 120, 120, 0.75)",
};

const SOURCE_LAYER_LABEL: Record<string, string> = {
  sanity:       "integrity",
  genesis:      "crisis genesis",
  transitions:  "state transitions",
  causal:       "causal",
  structural:   "structural",
  adaptation:   "adaptation",
};

const ACK_COLOR: Record<OperatorAckAction, string> = {
  ack:     "rgba(140, 170, 235, 0.95)",
  ignore:  "rgba(140, 140, 140, 0.85)",
  resolve: "rgba(82, 185, 122, 0.95)",
  mute:    "rgba(227, 180, 87, 0.95)",
};

function OperatorPriorityItemRow({
  item,
  onAction,
  onHistory,
  busy,
}: {
  item: OperatorPriorityItem;
  onAction: (key: string, action: OperatorAckAction) => void;
  onHistory: (key: string) => void;
  busy: boolean;
}) {
  const escColor = ESCALATION_COLOR[item.escalation_state];
  const lifeColor = LIFECYCLE_COLOR[item.lifecycle];
  const ack = item.ack ?? null;
  const tip = [
    `priority      ${item.priority_score.toFixed(1)}/100  →  ${item.escalation_state}`,
    `decomposition severity ${item.severity_raw.toFixed(0)} × confidence ${(item.confidence * 100).toFixed(0)}% × recency ${(item.recency * 100).toFixed(0)}% × source_weight ${item.source_weight.toFixed(2)}`,
    `lifecycle     ${item.lifecycle}${item.priority_delta != null ? ` (Δ ${item.priority_delta > 0 ? "+" : ""}${item.priority_delta.toFixed(1)})` : ""}`,
    `source        ${item.source_layer} (${item.kind})`,
    item.occurrence_count != null ? `occurrences   ${item.occurrence_count}` : "",
    ack ? `ack           ${ack.action.toUpperCase()} at ${new Date(ack.created_at_ms).toISOString().slice(11, 16)}${ack.expires_at_ms ? ` (until ${new Date(ack.expires_at_ms).toISOString().slice(11, 16)})` : ""}` : "",
    ``,
    item.detail,
    item.members.length > 0 ? `\nmembers: ${item.members.join(", ")}` : "",
  ].filter(Boolean).join("\n");
  const muted = ack && (ack.action === "ignore" || ack.action === "mute");
  return (
    <li className={`rounded border border-border/40 bg-bg/40 px-3 py-2 ${muted ? "opacity-50" : ""}`} title={tip}>
      <div className="flex items-baseline gap-2 flex-wrap mb-1">
        <span
          className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em] shrink-0 tabular-nums"
          style={{
            color: escColor,
            borderColor: escColor.replace(/0\.95\)$|0\.85\)$/, "0.5)"),
            background: escColor.replace(/0\.95\)$|0\.85\)$/, "0.10)"),
            minWidth: "3.4rem",
            textAlign: "center",
          }}
        >
          {item.priority_score.toFixed(0)}
        </span>
        <span className="text-[9px] uppercase tracking-[0.14em]" style={{ color: escColor }}>
          {item.escalation_state}
        </span>
        <span className={`text-[9px] uppercase tracking-[0.14em]`} style={{ color: lifeColor }}>
          {LIFECYCLE_LABEL[item.lifecycle]}
          {item.priority_delta != null && Math.abs(item.priority_delta) >= 1 && (
            <span className="ml-1 tabular-nums">
              ({item.priority_delta > 0 ? "+" : ""}{item.priority_delta.toFixed(0)})
            </span>
          )}
        </span>
        <span className="text-[9px] uppercase tracking-[0.14em] text-muted">
          {SOURCE_LAYER_LABEL[item.source_layer] ?? item.source_layer}
        </span>
        {ack && (
          <span
            className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em]"
            style={{
              color: ACK_COLOR[ack.action],
              borderColor: ACK_COLOR[ack.action].replace(/0\.95\)$|0\.85\)$/, "0.5)"),
              background: ACK_COLOR[ack.action].replace(/0\.95\)$|0\.85\)$/, "0.10)"),
            }}
          >
            {ack.action}
          </span>
        )}
        {item.occurrence_count != null && item.occurrence_count > 1 && (
          <span className="text-[9px] text-muted/70">×{item.occurrence_count}</span>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          <button
            disabled={busy}
            onClick={() => onAction(item.key, "ack")}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-[#8caaeb] hover:border-[#8caaeb]/50 disabled:opacity-30"
            title="acknowledge"
          >ack</button>
          <button
            disabled={busy}
            onClick={() => onAction(item.key, "mute")}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-[#e3b457] hover:border-[#e3b457]/50 disabled:opacity-30"
            title="mute for 60 min"
          >mute</button>
          <button
            disabled={busy}
            onClick={() => onAction(item.key, "ignore")}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-400 hover:border-zinc-500 disabled:opacity-30"
            title="ignore"
          >ign</button>
          <button
            disabled={busy}
            onClick={() => onAction(item.key, "resolve")}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-[#52b97a] hover:border-[#52b97a]/50 disabled:opacity-30"
            title="resolve"
          >res</button>
          <button
            onClick={() => onHistory(item.key)}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200 hover:border-zinc-400"
            title="show escalation history"
          >hist</button>
        </div>
      </div>
      <div className="text-[12px] text-zinc-200 leading-snug">{item.headline}</div>
      {item.detail && item.detail !== item.headline && (
        <div className="text-[10px] text-muted mt-0.5 truncate">{item.detail}</div>
      )}
    </li>
  );
}

function OperatorHistoryDrawer({
  data,
  onClose,
}: {
  data: OperatorEscalationHistory;
  onClose: () => void;
}) {
  if (!data.found || data.history == null) {
    return (
      <div className="rounded border border-border/40 bg-bg/60 px-3 py-2 text-[11px] text-muted">
        No persistent history found for {data.priority_key}. <button onClick={onClose} className="text-zinc-300 underline">close</button>
      </div>
    );
  }
  const h = data.history;
  return (
    <div className="rounded border border-border/60 bg-bg/60 px-3 py-2.5 text-[11px]">
      <div className="flex items-baseline justify-between mb-2">
        <span className="text-[10px] uppercase tracking-[0.18em] text-zinc-200">
          history · {h.kind}
        </span>
        <button onClick={onClose} className="text-[10px] text-muted hover:text-zinc-300">close</button>
      </div>
      <div className="text-zinc-300 mb-2">{h.headline}</div>
      <div className="text-[10px] text-muted mb-3 font-mono">
        status {h.current_status} · escalation {h.current_escalation} (peak {h.peak_escalation} at {h.peak_priority_score.toFixed(0)}) · {h.occurrence_count}×
        <br />
        first {new Date(h.first_seen_at_ms).toISOString().slice(0, 16)} · last {new Date(h.last_seen_at_ms).toISOString().slice(0, 16)}
        {h.resolved_at_ms && <> · resolved {new Date(h.resolved_at_ms).toISOString().slice(0, 16)}</>}
      </div>
      {data.events.length === 0 ? (
        <div className="text-[10px] text-muted">No events recorded.</div>
      ) : (
        <ul className="space-y-1 font-mono text-[10px]">
          {data.events.map((e, i) => (
            <li key={i} className="text-muted">
              <span className="text-zinc-400 mr-2">{new Date(e.ts_ms).toISOString().slice(11, 19)}</span>
              <span className="text-zinc-300 mr-2">{e.event_type}</span>
              {e.priority_before != null && e.priority_after != null && (
                <span className="mr-2">{e.priority_before.toFixed(0)} → {e.priority_after.toFixed(0)}</span>
              )}
              {e.escalation_before && e.escalation_after && e.escalation_before !== e.escalation_after && (
                <span className="mr-2">{e.escalation_before} → {e.escalation_after}</span>
              )}
              {e.note && <span className="text-zinc-300">{e.note}</span>}
            </li>
          ))}
        </ul>
      )}
      {data.acknowledgements.length > 0 && (
        <div className="mt-3 pt-2 border-t border-border/40">
          <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">ack history</div>
          <ul className="space-y-0.5 font-mono text-[10px] text-muted">
            {data.acknowledgements.map((a, i) => (
              <li key={i}>
                <span className={a.active ? "text-zinc-300" : "text-zinc-500"}>
                  {a.action.toUpperCase()}
                </span>
                <span className="ml-2">{new Date(a.created_at_ms).toISOString().slice(11, 19)}</span>
                {a.expires_at_ms && <span className="ml-2">until {new Date(a.expires_at_ms).toISOString().slice(11, 19)}</span>}
                {!a.active && <span className="ml-2 text-zinc-600">[superseded]</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

type DigestWindow = 1 | 6 | 24;

function OperatorDigestBlock() {
  const [window, setWindow] = useState<DigestWindow>(24);
  const [data, setData] = useState<OperatorDigest | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getOperatorDigest(window)
      .then((d) => { if (!cancelled) setData(d); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [window]);

  return (
    <div className="rounded border border-border/40 bg-bg/40 px-3 py-2.5 mt-3">
      <div className="flex items-baseline gap-2 mb-2">
        <span className="text-[10px] uppercase tracking-[0.18em] text-muted">digest</span>
        <div className="flex gap-1">
          {[1, 6, 24].map((w) => (
            <button
              key={w}
              onClick={() => setWindow(w as DigestWindow)}
              className={`h-5 px-1.5 rounded border text-[9px] uppercase tracking-[0.14em] ${
                window === w ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
              }`}
            >{w}h</button>
          ))}
        </div>
        {data && (
          <span className="text-[10px] text-muted ml-2 truncate flex-1">{data.summary}</span>
        )}
      </div>
      {data && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-[11px] font-mono">
          {[
            { title: "new", items: data.new, color: LIFECYCLE_COLOR.NEW },
            { title: "worsened", items: data.worsened, color: LIFECYCLE_COLOR.WORSENING },
            { title: "stabilized", items: data.stabilized, color: LIFECYCLE_COLOR.STABILIZING },
            { title: "resolved", items: data.resolved, color: LIFECYCLE_COLOR.RESOLVED },
            { title: "reappeared", items: data.reappeared, color: ESCALATION_COLOR.WATCH },
          ].filter((s) => s.items.length > 0).map((s) => (
            <div key={s.title}>
              <div className="text-[9px] uppercase tracking-[0.18em] mb-1" style={{ color: s.color }}>
                {s.title} ({s.items.length})
              </div>
              <ul className="space-y-0.5 text-[10px] text-zinc-300">
                {s.items.slice(0, 6).map((it) => (
                  <li key={it.priority_key} className="truncate">{it.headline}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      {data && data.new.length === 0 && data.worsened.length === 0 && data.stabilized.length === 0 && data.resolved.length === 0 && data.reappeared.length === 0 && (
        <div className="text-[11px] text-muted">No material changes in the last {window}h.</div>
      )}
    </div>
  );
}

function OperatorPrioritiesPanel() {
  const [data, setData] = useState<OperatorPriorities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [historyData, setHistoryData] = useState<OperatorEscalationHistory | null>(null);
  const [showFilter, setShowFilter] = useState<"active" | "all" | "acked">("active");

  const refresh = () =>
    getOperatorPriorities()
      .then(setData)
      .catch((e) => setError(e?.message ?? "failed"));

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 60_000);
    return () => window.clearInterval(id);
  }, []);

  const handleAction = async (key: string, action: OperatorAckAction) => {
    setBusyKey(key);
    try {
      await postOperatorAck(key, { action, mute_minutes: action === "mute" ? 60 : undefined });
      await refresh();
    } catch (e: any) {
      console.error("ack failed", e);
    } finally {
      setBusyKey(null);
    }
  };

  const handleHistory = async (key: string) => {
    if (historyKey === key) {
      setHistoryKey(null);
      setHistoryData(null);
      return;
    }
    setHistoryKey(key);
    setHistoryData(null);
    try {
      const h = await getOperatorEscalationHistory(key, 50);
      setHistoryData(h);
    } catch (e: any) {
      console.error("history fetch failed", e);
    }
  };

  if (error) return null;
  if (!data) return null;

  const ec = data.escalation_counts ?? {};
  const nCritical = ec.CRITICAL ?? 0;
  const nImportant = ec.IMPORTANT ?? 0;
  const nWatch = ec.WATCH ?? 0;
  const nNormal = ec.NORMAL ?? 0;

  const overallColor =
    nCritical > 0 ? ESCALATION_COLOR.CRITICAL :
    nImportant > 0 ? ESCALATION_COLOR.IMPORTANT :
    nWatch > 0 ? ESCALATION_COLOR.WATCH :
    "rgba(82, 185, 122, 0.85)";

  return (
    <div
      className="rounded-lg border bg-panel/70 px-3 py-3"
      style={{ borderColor: overallColor.replace(/0\.95\)$|0\.85\)$/, "0.55)") }}
    >
      <div className="flex items-baseline gap-3 mb-2.5 flex-wrap">
        <span className="text-[11px] uppercase tracking-[0.22em]" style={{ color: overallColor }}>
          ● operator queue
        </span>
        <span className="text-[10px] uppercase tracking-[0.2em] text-muted flex-1">
          {data.summary}
        </span>
        <div className="text-[9px] uppercase tracking-[0.16em] flex gap-2 items-baseline">
          {nCritical > 0 && <span style={{ color: ESCALATION_COLOR.CRITICAL }}>{nCritical} crit</span>}
          {nImportant > 0 && <span style={{ color: ESCALATION_COLOR.IMPORTANT }}>{nImportant} imp</span>}
          {nWatch > 0 && <span style={{ color: ESCALATION_COLOR.WATCH }}>{nWatch} watch</span>}
          {nNormal > 0 && <span className="text-muted">{nNormal} normal</span>}
        </div>
      </div>

      {data.narrative_headline && (
        <div className="text-[10px] text-muted leading-snug mb-2 italic">
          “{data.narrative_headline}”
        </div>
      )}

      <div className="flex items-baseline gap-2 mb-2 text-[9px] uppercase tracking-[0.14em]">
        <span className="text-muted">show:</span>
        {(["active", "acked", "all"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setShowFilter(f)}
            className={`px-1.5 py-0.5 rounded border ${
              showFilter === f ? "border-accent text-accent" : "border-border/50 text-muted hover:text-zinc-200"
            }`}
          >{f}</button>
        ))}
      </div>

      {(() => {
        const filtered = data.items.filter((it) => {
          if (showFilter === "all") return true;
          if (showFilter === "acked") return it.ack != null;
          // "active": exclude resolved + items that are ignored/muted+expired
          return it.ack?.action !== "resolve" && !(it.ack?.action === "ignore");
        });
        if (filtered.length === 0) {
          return (
            <div className="text-xs text-muted">
              {showFilter === "active"
                ? "Nothing active above the priority floor (try 'all' to see ignored/muted)."
                : showFilter === "acked"
                ? "No acknowledged items right now."
                : "Operator queue empty."}
            </div>
          );
        }
        return (
          <ul className="space-y-1.5">
            {filtered.map((it) => (
              <OperatorPriorityItemRow
                key={it.key}
                item={it}
                onAction={handleAction}
                onHistory={handleHistory}
                busy={busyKey === it.key}
              />
            ))}
          </ul>
        );
      })()}

      {historyKey && historyData && (
        <div className="mt-3">
          <OperatorHistoryDrawer
            data={historyData}
            onClose={() => { setHistoryKey(null); setHistoryData(null); }}
          />
        </div>
      )}

      {data.filtered_count > 0 && (
        <div className="text-[10px] text-muted mt-2.5">
          + {data.filtered_count} more item(s) below the attention budget ({data.attention_budget}).
        </div>
      )}

      {data.resolved.length > 0 && (
        <div className="mt-3 pt-2.5 border-t border-border/40">
          <div className="text-[10px] uppercase tracking-[0.18em] text-muted mb-1.5">
            recently resolved
          </div>
          <ul className="space-y-1 text-[11px] font-mono text-muted">
            {data.resolved.map((r) => (
              <li key={r.key} className="truncate">
                <span className="text-[9px] uppercase tracking-[0.14em] mr-2" style={{ color: LIFECYCLE_COLOR.RESOLVED }}>
                  resolved
                </span>
                {r.headline}
              </li>
            ))}
          </ul>
        </div>
      )}

      <OperatorDigestBlock />
    </div>
  );
}

// ── Adaptation State (Phase 16) ─────────────────────────────────────────

const MODIFIER_LABEL: Record<string, string> = {
  narrative_confidence_modifier:  "narrative confidence",
  alert_sensitivity_modifier:     "alert sensitivity",
  causal_strictness_modifier:     "causal strictness",
  discovery_suppression_modifier: "discovery suppression",
  global_trust_modifier:          "global trust",
};

function modifierColor(value: number): string {
  if (Math.abs(value - 1.0) < 0.01) return "rgba(140, 140, 140, 0.75)";
  // Below 1.0 = suppression (cooler / yellow), above 1.0 = amplification (red)
  if (value < 1.0) return "rgba(227, 180, 87, 0.95)";
  return "rgba(214, 105, 105, 0.95)";
}

function AdaptationStatePanel() {
  const [data, setData] = useState<AdaptationState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getAdaptationState()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, 60_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (error) return null;
  if (!data) return null;

  const anyChanged = Object.values(data.modifiers).some((v) => Math.abs(v - 1.0) > 1e-9);

  return (
    <div
      className="rounded-lg border bg-panel/60 px-3 py-2.5"
      style={{
        borderColor: anyChanged ? "rgba(227, 180, 87, 0.45)" : "rgba(82, 185, 122, 0.35)",
      }}
    >
      <div className="flex items-baseline gap-3 mb-2">
        <span className="text-[11px] uppercase tracking-[0.22em]"
              style={{ color: anyChanged ? "rgba(227, 180, 87, 0.95)" : "rgba(82, 185, 122, 0.95)" }}>
          ● adaptation loop · {anyChanged ? "active" : "neutral"}
        </span>
        <span className="text-[10px] uppercase tracking-[0.18em] text-muted">
          {data.summary}
        </span>
      </div>

      <div className="text-[10px] text-muted leading-snug mb-2.5">
        Bounded modifier coefficients fed back into downstream layers. Every
        modifier is clipped to a documented range, every change is logged with
        its trigger reason, and downstream consumers apply them externally —
        the loop is reversible by simply ignoring the modifier.
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-[11px]">
        {Object.entries(data.modifiers).map(([name, value]) => {
          const audit = data.audit_trail.find((a) => a.layer === name);
          const color = modifierColor(value);
          return (
            <div key={name} className="flex items-baseline gap-2" title={audit?.reason ?? "no change"}>
              <span
                className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em] shrink-0 tabular-nums"
                style={{
                  color,
                  borderColor: color.replace(/0\.95\)$|0\.75\)$/, "0.5)"),
                  background: color.replace(/0\.95\)$|0\.75\)$/, "0.10)"),
                  minWidth: "3.4rem",
                  textAlign: "center",
                }}
              >
                ×{value.toFixed(2)}
              </span>
              <span className="text-muted shrink-0 w-44">{MODIFIER_LABEL[name] ?? name}</span>
              <span className="text-zinc-300 flex-1 truncate text-[10px]">
                {audit ? audit.reason : "no trigger"}
              </span>
            </div>
          );
        })}
      </div>

      {/* Upstream anchors */}
      <div className="mt-3 pt-2.5 border-t border-border/40 text-[10px] text-muted font-mono">
        <span className="uppercase tracking-[0.18em] mr-2">upstream</span>
        narrative {(data.upstream_snapshot.narrative_overall_confidence * 100).toFixed(0)}%
        <span className="mx-1.5 text-muted/60">·</span>
        genesis {data.upstream_snapshot.genesis_score.toFixed(0)} ({(data.upstream_snapshot.genesis_verdict ?? "—").toLowerCase().replace(/_/g, " ")})
        <span className="mx-1.5 text-muted/60">·</span>
        flicker {(data.upstream_snapshot.transitions_flicker_ratio * 100).toFixed(0)}%{data.upstream_snapshot.transitions_oscillating ? " ⤧" : ""}
        <span className="mx-1.5 text-muted/60">·</span>
        sanity {data.upstream_snapshot.sanity_overall_state.toLowerCase()}
        {data.upstream_snapshot.meta_confidence_score != null && (
          <>
            <span className="mx-1.5 text-muted/60">·</span>
            meta-conf {data.upstream_snapshot.meta_confidence_score.toFixed(0)}
          </>
        )}
        {data.upstream_snapshot.structural_break_score != null && (
          <>
            <span className="mx-1.5 text-muted/60">·</span>
            structural-break {data.upstream_snapshot.structural_break_score.toFixed(0)}
          </>
        )}
      </div>
    </div>
  );
}

// ── Crisis Genesis Detection (Phase 15 #4) ──────────────────────────────

const GENESIS_VERDICT_COLOR: Record<CrisisGenesisVerdict, string> = {
  CALM:             "rgba(82, 185, 122, 0.95)",
  EARLY_DISTORTION: "rgba(140, 170, 235, 0.95)",
  ELEVATED_RISK:    "rgba(227, 180, 87, 0.95)",
  PRE_CASCADE:      "rgba(214, 75, 75, 0.95)",
  INSUFFICIENT:     "rgba(140, 140, 140, 0.85)",
};

const GENESIS_VERDICT_LABEL: Record<CrisisGenesisVerdict, string> = {
  CALM:             "calm — no precursor signals materially elevated",
  EARLY_DISTORTION: "early distortion — one or two precursor signals firing",
  ELEVATED_RISK:    "elevated risk — multiple precursor signals firing",
  PRE_CASCADE:      "pre-cascade — most precursor signals firing",
  INSUFFICIENT:     "insufficient — not enough data to score",
};

const PROBE_STATUS_COLOR: Record<CrisisGenesisStatus, string> = {
  calm:         "rgba(82, 185, 122, 0.95)",
  elevated:     "rgba(227, 180, 87, 0.95)",
  hot:          "rgba(214, 75, 75, 0.95)",
  insufficient: "rgba(140, 140, 140, 0.75)",
};

function CrisisGenesisPanel() {
  const [data, setData] = useState<CrisisGenesis | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getCrisisGenesis()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, 60_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (error) return null;
  if (!data) return null;

  const verdictColor = GENESIS_VERDICT_COLOR[data.verdict];
  const verdictLabel = GENESIS_VERDICT_LABEL[data.verdict];

  return (
    <div
      className="rounded-lg border bg-panel/60 px-3 py-2.5"
      style={{ borderColor: verdictColor.replace(/0\.95\)$|0\.85\)$/, "0.55)") }}
    >
      <div className="flex items-baseline gap-3 mb-2.5">
        <span className="text-[11px] uppercase tracking-[0.22em]" style={{ color: verdictColor }}>
          ● crisis genesis · {data.verdict.toLowerCase().replace(/_/g, " ")}
        </span>
        <span className="text-[10px] uppercase tracking-[0.18em] text-muted">
          score {data.genesis_score.toFixed(0)}/100 · confidence {(data.confidence * 100).toFixed(0)}% · {data.probe_count - data.insufficient_count}/{data.probe_count} probes contributing
        </span>
      </div>

      <div className="text-[11px] text-muted mb-3 leading-snug">
        {verdictLabel}. {data.verdict === "PRE_CASCADE" ? (
          <span className="text-[#d68b8b]">Take action before propagation widens further.</span>
        ) : data.verdict === "ELEVATED_RISK" ? (
          <span className="text-[#e3b457]">Watch for the next hot probe to confirm.</span>
        ) : data.verdict === "EARLY_DISTORTION" ? (
          <span>Treat as exploratory — could resolve or escalate.</span>
        ) : data.verdict === "INSUFFICIENT" ? (
          <span>The system is silent because data is missing, not because the market is calm.</span>
        ) : (
          <span>Precursor probes are quiet.</span>
        )}
      </div>

      <ul className="space-y-1.5 font-mono text-[11px]">
        {data.probes.map((p) => {
          const sc = PROBE_STATUS_COLOR[p.status];
          return (
            <li key={p.kind} className="flex items-baseline gap-2" title={p.rationale}>
              <span
                className="inline-block rounded-sm border px-1 py-0.5 text-[9px] uppercase tracking-[0.12em] shrink-0 tabular-nums"
                style={{
                  color: sc,
                  borderColor: sc.replace(/0\.95\)$|0\.85\)$|0\.75\)$/, "0.5)"),
                  background: sc.replace(/0\.95\)$|0\.85\)$|0\.75\)$/, "0.10)"),
                  minWidth: "3.4rem",
                  textAlign: "center",
                }}
              >
                {p.status === "insufficient" ? "—" : p.score.toFixed(0)}
              </span>
              <span className="text-muted shrink-0 w-44 truncate">{p.name}</span>
              <span className="text-zinc-300 flex-1 truncate">{p.rationale}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ── Narrative Causality (Phase 15 #6) ───────────────────────────────────

const NARRATIVE_SECTION_ICON: Record<string, string> = {
  state:        "●",
  propagation:  "▸",
  structural:   "◆",
  genesis:      "⚠",
  uncertainty:  "?",
};

function NarrativeCausalityPanel() {
  const [data, setData] = useState<NarrativeCausality | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getNarrativeCausality()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, 60_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  return (
    <Panel
      title="Narrative Causality"
      subtitle={data ? data.headline : "deterministic narrative over Phase 15 layers"}
      toolbar={
        data ? (
          <span className="text-[9px] uppercase tracking-[0.18em] text-muted">
            confidence {(data.overall_confidence * 100).toFixed(0)}%
          </span>
        ) : null
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="text-[10px] text-muted leading-snug">
            Composed deterministically from causal_propagation +
            structural_dependencies + market_state_transitions +
            crisis_genesis. Every claim is template-built from
            upstream numbers — no model generation, no opaque scoring.
          </div>
          <ul className="space-y-2.5">
            {data.sections.map((s) => {
              const icon = NARRATIVE_SECTION_ICON[s.kind] ?? "·";
              const confColor =
                s.confidence == null ? "text-muted" :
                s.confidence >= 0.7 ? "text-[#52b97a]" :
                s.confidence >= 0.4 ? "text-[#8caaeb]" :
                "text-[#e3b457]";
              return (
                <li key={s.kind} className="leading-relaxed">
                  <div className="flex items-baseline gap-2 mb-0.5">
                    <span className="text-[11px] uppercase tracking-[0.18em] text-muted shrink-0">
                      {icon} {s.title}
                    </span>
                    {s.confidence != null && (
                      <span className={`text-[9px] uppercase tracking-[0.12em] ${confColor}`}>
                        conf {(s.confidence * 100).toFixed(0)}
                      </span>
                    )}
                  </div>
                  <p className="text-[12px] text-zinc-300 pl-5">
                    {s.text}
                  </p>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </Panel>
  );
}

// ── Pattern discovery ───────────────────────────────────────────────────

function PatternDiscoveryPanel() {
  const [data, setData] = useState<PatternDiscovery | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [minSupport, setMinSupport] = useState(8);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getPatternDiscovery({ min_support: minSupport })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [minSupport]);

  return (
    <Panel
      title="Pattern Discovery"
      subtitle={
        data
          ? `base rate ${(data.base_rate * 100).toFixed(2)}% · ${data.total_buckets} buckets · ${data.patterns.length} patterns${data.suppressed_count ? ` (${data.suppressed_count} suppressed)` : ""}`
          : "recurring metric combos → downstream alert rates"
      }
      toolbar={
        <div className="flex items-center gap-3">
          <DataQualityChip q={data?.data_quality} />
          <div className="flex items-center gap-1">
            {[5, 8, 15, 30].map((s) => (
              <button
                key={s}
                onClick={() => setMinSupport(s)}
                className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                  minSupport === s ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
                }`}
              >
                n≥{s}
              </button>
            ))}
          </div>
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.patterns.length === 0 && (
        <div className="text-xs text-muted space-y-1">
          <div>No patterns at min_support={data.min_support}.</div>
          {data.data_quality === "INSUFFICIENT" && (
            <div className="text-[11px]">
              Engine has {data.total_buckets} qualifying buckets. Need ≥20 for patterns to
              be statistically meaningful — accumulating sample data.
            </div>
          )}
          {data.data_quality !== "INSUFFICIENT" && (
            <div className="text-[11px]">Try a lower support threshold, or accumulate more history.</div>
          )}
        </div>
      )}
      {data && data.patterns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">ID</th>
                {data.metrics.map((m) => (
                  <th key={m} className="text-left py-2 px-1">{m.replace(/_score/g, "").replace(/_/g, " ").slice(0, 6)}</th>
                ))}
                <th className="text-right py-2">N</th>
                <th className="text-right py-2">RATE</th>
                <th className="text-right py-2" title="effective lift = raw lift × stability">LIFT</th>
                <th className="text-right py-2" title="stability score; pattern_confidence = stability × scarcity">STAB · CONF</th>
                <th className="text-left py-2">FLAGS</th>
                <th className="text-left py-2">DOM KIND</th>
              </tr>
            </thead>
            <tbody>
              {data.patterns.map((p) => {
                const eff = p.effective_lift;
                const effColor =
                  eff >= 1.5 ? "text-[#52b97a]"
                    : eff >= 1.0 ? "text-[#e3b457]"
                      : eff > 0 ? "text-[#d68b8b]" : "text-muted";
                const stab = p.stability_score;
                const stabColor = stab >= 0.6 ? "text-[#52b97a]" : stab >= 0.3 ? "text-[#e3b457]" : "text-[#d68b8b]";
                const rowClass = p.suppressed_reason ? "opacity-40" : "";
                const tip = [
                  `raw_lift     ${p.lift != null ? p.lift.toFixed(2) + "×" : "—"}`,
                  `effective    ${eff.toFixed(2)}×  (raw × stability)`,
                  `stability    ${(p.stability_score * 100).toFixed(0)}/100`,
                  `confidence   ${p.pattern_confidence.toFixed(0)}/100`,
                  `day_span     ${p.day_span} days`,
                  `half balance ${p.first_half_support}/${p.second_half_support}`,
                  p.suppressed_reason ? `suppressed: ${p.suppressed_reason}` : "",
                ].filter(Boolean).join("\n");
                const flagAbbr: Record<string, string> = {
                  LOW_SUPPORT: "thin",
                  HIGH_LIFT_LOW_SUPPORT: "lift/N",
                  SINGLE_WINDOW: "1 window",
                  LOW_RECURRENCE: "burst",
                  REGIME_FRAGILE: "1 regime",
                  BUCKET_SENSITIVE: "dense",
                };
                return (
                  <tr key={p.discovered_pattern_id} className={`border-t border-border/40 ${rowClass}`} title={tip}>
                    <td className="py-1 text-muted text-[10px]">{p.discovered_pattern_id}</td>
                    {data.metrics.map((m) => (
                      <td key={m} className="py-1 px-1"><TertileChip v={p.signature[m] as "low" | "mid" | "high" | "na"} /></td>
                    ))}
                    <td className="py-1 text-right text-zinc-200">{p.support}</td>
                    <td className="py-1 text-right text-zinc-200">{(p.outcome_rate * 100).toFixed(0)}%</td>
                    <td className={`py-1 text-right ${effColor}`}>
                      <div className="leading-tight">
                        <div>{eff > 0 ? `${eff.toFixed(2)}×` : "—"}</div>
                        {p.lift != null && Math.abs(p.lift - eff) > 0.02 && (
                          <div className="text-[9px] text-muted/70">raw {p.lift.toFixed(2)}×</div>
                        )}
                      </div>
                    </td>
                    <td className={`py-1 text-right tabular-nums ${stabColor}`}>
                      {(stab * 100).toFixed(0)}<span className="text-muted/60"> · </span>
                      <span className="text-muted">{p.pattern_confidence.toFixed(0)}</span>
                    </td>
                    <td className="py-1 text-[9px]">
                      {p.robustness_flags.length === 0 ? (
                        <span className="text-muted">—</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {p.robustness_flags.map((f) => (
                            <span
                              key={f}
                              className="inline-block rounded-sm border border-[#d68b8b]/40 bg-[#d68b8b]/10 text-[#d68b8b] px-1 py-0.5 uppercase tracking-[0.08em]"
                              title={f}
                            >
                              {flagAbbr[f] ?? f.toLowerCase()}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="py-1 text-[10px] text-zinc-300">{p.dominant_alert_kind ? `${p.dominant_alert_kind.replace(/_/g, " ")} (${p.dominant_alert_count})` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── Crisis archetypes ───────────────────────────────────────────────────

const ARCHETYPE_COLOR: Record<string, string> = {
  slow_deterioration: "rgba(227, 180, 87, 0.95)",
  liquidity_evaporation: "rgba(214, 139, 105, 0.95)",
  instability_propagation: "rgba(214, 105, 105, 0.95)",
  explosive_cascade: "rgba(214, 75, 75, 0.95)",
  venue_fragmentation: "rgba(140, 170, 235, 0.95)",
  speculative_overheating: "rgba(214, 139, 105, 0.95)",
  recovery_exhaustion: "rgba(140, 170, 235, 0.95)",
  isolated_outlier: "rgba(140, 140, 140, 0.85)",
};

function CrisisArchetypesPanel() {
  const [data, setData] = useState<CrisisArchetypes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCrisisArchetypes()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Crisis Archetypes"
      subtitle={data ? `${data.archetypes.length} clusters · ${data.anomaly_count} anomalies` : "auto-labelled archetype clusters"}
      toolbar={<DataQualityChip q={data?.data_quality} />}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.archetypes.length === 0 && (
        <div className="text-xs text-muted">
          No archetypes yet — anomaly memory has {data.anomaly_count} record(s).
          {data.data_quality === "INSUFFICIENT" && " Need ≥5 anomalies for clustering to be meaningful."}
        </div>
      )}
      {data && data.archetypes.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">ID</th>
              <th className="text-left py-2">LABEL</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-right py-2">FREQ/D</th>
              <th className="text-right py-2">SEV</th>
              <th className="text-right py-2">RECOV</th>
              <th className="text-left py-2">DOMINANT</th>
            </tr>
          </thead>
          <tbody>
            {data.archetypes.map((a) => {
              const color = ARCHETYPE_COLOR[a.archetype_label] ?? "rgba(140, 170, 235, 0.95)";
              return (
                <tr key={a.archetype_id} className="border-t border-border/40">
                  <td className="py-1.5 text-muted">{a.archetype_id}</td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                      style={{
                        color,
                        borderColor: color.replace(/0\.95\)$/, "0.5)"),
                        background: color.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {a.archetype_label.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="py-1.5 text-right text-zinc-200">{a.size}</td>
                  <td className="py-1.5 text-right text-muted">{a.frequency_per_day.toFixed(2)}</td>
                  <td className="py-1.5 text-right text-muted">{(a.structural_severity * 100).toFixed(0)}</td>
                  <td className="py-1.5 text-right text-muted">{(a.recovery_probability * 100).toFixed(0)}%</td>
                  <td className="py-1.5 text-[10px] text-zinc-300">{a.dominant_kind.replace(/_/g, " ")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Hidden regimes ──────────────────────────────────────────────────────

function HiddenRegimesPanel() {
  const [data, setData] = useState<HiddenRegimes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getHiddenRegimes()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Hidden Regimes"
      subtitle={data ? `${data.clusters.length} clusters · ${data.snapshot_count} snapshots` : "clusters in engine-state space"}
      toolbar={<DataQualityChip q={data?.data_quality} />}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.clusters.length === 0 && (
        <div className="text-xs text-muted">
          {data.snapshot_count} intelligence snapshots so far.
          {data.data_quality === "INSUFFICIENT" && " Need ≥20 for clusters to be statistically meaningful."}
          {" "}Worker snapshots every 5 min.
        </div>
      )}
      {data && data.clusters.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">CL</th>
              <th className="text-left py-2">LABEL HINT</th>
              <th className="text-left py-2">DOMINANT STATE</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-right py-2">EMERGENT</th>
              <th className="text-right py-2">STABILITY</th>
            </tr>
          </thead>
          <tbody>
            {data.clusters.map((c) => {
              const emergentColor = c.is_emergent ? "rgba(227, 180, 87, 0.95)" : "rgba(140, 170, 235, 0.95)";
              return (
                <tr key={c.cluster_id} className="border-t border-border/40">
                  <td className="py-1.5 text-muted">{c.cluster_id}</td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                      style={{
                        color: emergentColor,
                        borderColor: emergentColor.replace(/0\.95\)$/, "0.5)"),
                        background: emergentColor.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {c.label_hint.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="py-1.5 text-[10px] text-zinc-300">{c.dominant_coordinated_state?.replace(/_/g, " ") ?? "—"}</td>
                  <td className="py-1.5 text-right text-zinc-200">{c.size}</td>
                  <td className="py-1.5 text-right" style={{ color: emergentColor }}>{c.emergent_regime_score.toFixed(0)}</td>
                  <td className="py-1.5 text-right text-muted">{c.regime_stability.toFixed(0)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Propagation graph ───────────────────────────────────────────────────

function PropagationPanel() {
  const [data, setData] = useState<Propagation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPropagation()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  const validatedCount = data ? data.edges.filter((e) => e.confidence === "HIGH").length : 0;
  const mediumCount = data ? data.edges.filter((e) => e.confidence === "MEDIUM").length : 0;
  const candidateCount = data ? data.edges.filter((e) => e.confidence === "LOW").length : 0;
  const integrityState =
    data == null ? "" :
    data.integrity_score >= 70 ? "trustworthy" :
    data.integrity_score >= 50 ? "borderline" :
    data.integrity_score >= 30 ? "weak" : "near-noise";
  const integrityColor =
    data == null ? "rgba(140, 140, 140, 0.85)" :
    data.integrity_score >= 70 ? "rgba(82, 185, 122, 0.95)" :
    data.integrity_score >= 50 ? "rgba(140, 170, 235, 0.95)" :
    data.integrity_score >= 30 ? "rgba(227, 180, 87, 0.95)" : "rgba(214, 105, 105, 0.95)";

  return (
    <Panel
      title="Structural Propagation"
      subtitle={
        data
          ? `${validatedCount} validated · ${mediumCount} provisional · ${candidateCount} candidate edges`
          : "symbol→symbol lead-lag"
      }
      toolbar={
        data ? (
          <div className="flex items-center gap-2 text-[9px] uppercase tracking-[0.18em]">
            <span
              className="rounded-sm border px-1.5 py-0.5"
              style={{
                color: integrityColor,
                borderColor: integrityColor.replace(/0\.95\)$/, "0.5)"),
                background: integrityColor.replace(/0\.95\)$/, "0.10)"),
              }}
              title={`Graph-level integrity score combines avg edge confidence, symmetric-pair share, weak-edge share and coverage. ${integrityState}.`}
            >
              integrity {data.integrity_score.toFixed(0)}/100
            </span>
            <span className="text-muted">{integrityState}</span>
          </div>
        ) : null
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.edges.length === 0 && (
        <div className="text-xs text-muted">No propagation edges yet — needs alert history across multiple symbols.</div>
      )}
      {data && data.edges.length > 0 && (
        <>
        <div className="text-[10px] text-muted mb-3">
          {validatedCount === 0 ? (
            <>
              <span className="text-[#e3b457]">Zero validated edges.</span>{" "}
              All {data.edges.length} are candidates only — symbol pairs with too little independent recurrence,
              dominated by single-burst days, or with symmetric reverse edges (coincidence rather than lead-lag).
              Don't act on these as alpha.
            </>
          ) : (
            <>
              <span className="text-[#52b97a]">{validatedCount} edge(s) reached HIGH confidence</span>
              {" "}— stable across the lookback window with clean lead times. Candidates below are exploratory.
            </>
          )}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">top edges</div>
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">A → B</th>
                  <th className="text-right py-2">N</th>
                  <th className="text-right py-2">LEAD±σ</th>
                  <th className="text-right py-2">SCORE</th>
                  <th className="text-right py-2">CONF</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.slice(0, 12).map((e, i) => {
                  const tip = [
                    `volume      ${(e.volume_strength * 100).toFixed(0)}`,
                    `lead clarity ${(e.lead_clarity * 100).toFixed(0)}`,
                    `lead consist ${(e.lead_consistency * 100).toFixed(0)}`,
                    `temporal     ${(e.temporal_consistency * 100).toFixed(0)}`,
                    `recurrence   ${(e.recurrence_stability * 100).toFixed(0)}`,
                    `leader stab  ${(e.leader_stability * 100).toFixed(0)}`,
                    `symmetry pen ${(e.symmetry_penalty * 100).toFixed(0)}`,
                    `base → final ${(e.base_confidence * 100).toFixed(0)} → ${(e.confidence_score * 100).toFixed(0)}`,
                  ].join("\n");
                  return (
                    <tr key={i} className="border-t border-border/40" title={tip}>
                      <td className="py-1 text-[10px]">
                        <span className="text-zinc-200">{e.from_symbol}</span>
                        <span className="text-muted"> → </span>
                        <span className="text-zinc-200">{e.to_symbol}</span>
                        {e.symmetry_penalty >= 0.5 && (
                          <span className="ml-1 text-[#e3b457]" title="symmetric reverse edge — likely coincidence">⇄</span>
                        )}
                      </td>
                      <td className="py-1 text-right text-zinc-200">{e.count}</td>
                      <td className="py-1 text-right text-muted">
                        {e.avg_lead_s.toFixed(0)}±{e.lead_std_s.toFixed(0)}s
                      </td>
                      <td className="py-1 text-right text-zinc-300">{(e.confidence_score * 100).toFixed(0)}</td>
                      <td className="py-1 text-right"><EdgeConfidenceChip c={e.confidence} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">leaders</div>
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">SYMBOL</th>
                  <th className="text-right py-2">OUT</th>
                  <th className="text-right py-2">IN</th>
                  <th className="text-right py-2">NET</th>
                  <th className="text-right py-2">STAB</th>
                </tr>
              </thead>
              <tbody>
                {data.nodes.slice(0, 12).map((n) => {
                  const stabPct = n.leader_stability * 100;
                  const stabColor = stabPct >= 70 ? "text-[#52b97a]" : stabPct >= 45 ? "text-[#8caaeb]" : "text-[#e3b457]";
                  return (
                    <tr key={n.symbol} className="border-t border-border/40" title={`leader_stability ${stabPct.toFixed(0)} · follower_stability ${(n.follower_stability * 100).toFixed(0)}`}>
                      <td className="py-1 text-zinc-200">{n.symbol}</td>
                      <td className="py-1 text-right text-muted">{n.out_count}</td>
                      <td className="py-1 text-right text-muted">{n.in_count}</td>
                      <td className={`py-1 text-right ${n.net_lead > 0 ? "text-[#d68b8b]" : n.net_lead < 0 ? "text-[#52b97a]" : "text-zinc-300"}`}>
                        {n.net_lead > 0 ? "+" : ""}{n.net_lead}
                      </td>
                      <td className={`py-1 text-right ${stabColor}`}>{stabPct.toFixed(0)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
        </>
      )}
      {data?.average_propagation_velocity_s != null && (
        <div className="text-[10px] uppercase tracking-[0.16em] text-muted pt-3 border-t border-border/40 mt-3">
          avg propagation velocity: <span className="text-zinc-300">{data.average_propagation_velocity_s.toFixed(0)}s</span>
        </div>
      )}
    </Panel>
  );
}

// ── Causal Propagation (Phase 15) ───────────────────────────────────────

const VERDICT_COLOR: Record<CausalVerdict, string> = {
  DIRECTIONAL:     "rgba(82, 185, 122, 0.95)",
  COMMON_DRIVEN:   "rgba(227, 180, 87, 0.95)",
  AMBIGUOUS:       "rgba(140, 170, 235, 0.95)",
  UNDER_EVIDENCED: "rgba(140, 140, 140, 0.85)",
  COINCIDENCE:     "rgba(214, 105, 105, 0.95)",
  EXPLORATORY:     "rgba(120, 120, 120, 0.85)",
};

const VERDICT_LABEL: Record<CausalVerdict, string> = {
  DIRECTIONAL:     "directional",
  COMMON_DRIVEN:   "common-driven",
  AMBIGUOUS:       "ambiguous",
  UNDER_EVIDENCED: "under-evidenced",
  COINCIDENCE:     "coincidence",
  EXPLORATORY:     "exploratory",
};

const ROLE_COLOR: Record<CausalRole, string> = {
  LEADER:          "rgba(82, 185, 122, 0.95)",
  AMPLIFIER:       "rgba(140, 170, 235, 0.95)",
  FOLLOWER:        "rgba(170, 170, 180, 0.85)",
  INSTABILITY_HUB: "rgba(227, 180, 87, 0.95)",
  ISOLATED:        "rgba(120, 120, 120, 0.75)",
};

function VerdictChip({ v }: { v: CausalVerdict }) {
  const color = VERDICT_COLOR[v];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$|0\.85\)$/, "0.5)"),
        background: color.replace(/0\.95\)$|0\.85\)$/, "0.10)"),
      }}
    >
      {VERDICT_LABEL[v]}
    </span>
  );
}

function RoleChip({ r }: { r: CausalRole }) {
  const color = ROLE_COLOR[r];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$|0\.85\)$/, "0.5)"),
        background: color.replace(/0\.95\)$|0\.85\)$/, "0.10)"),
      }}
    >
      {r.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

function CausalPropagationPanel() {
  const [data, setData] = useState<CausalPropagation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getCausalPropagation()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const directional = data?.verdict_counts.DIRECTIONAL ?? 0;
  const commonDriven = data?.verdict_counts.COMMON_DRIVEN ?? 0;
  const coincidence = data?.verdict_counts.COINCIDENCE ?? 0;
  const underEvidenced = data?.verdict_counts.UNDER_EVIDENCED ?? 0;
  const exploratory = data?.verdict_counts.EXPLORATORY ?? 0;

  return (
    <Panel
      title="Causal Propagation"
      subtitle={
        data
          ? `${directional} directional · ${commonDriven} common-driven · ${coincidence} coincidence · ${exploratory + underEvidenced} not-evidenced`
          : "lead-lag pairs with verdicts (probabilistic, not deterministic)"
      }
      toolbar={
        data ? (
          <div className="flex items-center gap-2">
            <DataQualityChip q={data.data_quality} />
            <span className="text-[9px] uppercase tracking-[0.18em] text-muted">
              {data.lookback_days}d · {data.n_windows} sub-windows
            </span>
          </div>
        ) : null
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.edges.length === 0 && (
        <div className="text-xs text-muted">
          No edges above the count threshold. Need alert history across multiple symbols.
        </div>
      )}
      {data && data.edges.length > 0 && (
        <div className="space-y-3">
          <div className="text-[10px] text-muted leading-snug">
            {data.data_quality === "INSUFFICIENT" || data.data_quality === "LOW" ? (
              <>
                <span className="text-[#e3b457]">All pairs labeled EXPLORATORY</span> — too few alerts
                ({data.total_alerts}) for causal claims. The verdicts below reflect statistical signals
                only and should not be treated as evidence of influence.
              </>
            ) : directional > 0 ? (
              <>
                <span className="text-[#52b97a]">{directional} edge(s) survived all four tests</span>
                {" "}(asymmetric direction, present in ≥ 2 sub-windows, no common-driver, sufficient
                data). Other edges fail at least one — they are not claims of influence, only
                surfaced for context.
              </>
            ) : (
              <>
                <span className="text-[#e3b457]">Zero directional edges.</span> Pairs are either
                coincidence, common-driven, or have only one sub-window of support. Do not treat
                lead-lag here as influence.
              </>
            )}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">edges by verdict</div>
              <table className="w-full text-sm font-mono">
                <thead>
                  <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                    <th className="text-left py-2">A → B</th>
                    <th className="text-right py-2">N/REV</th>
                    <th className="text-right py-2">CONF</th>
                    <th className="text-left py-2">VERDICT</th>
                  </tr>
                </thead>
                <tbody>
                  {data.edges.slice(0, 15).map((e: CausalEdge, i) => {
                    const tip = [
                      `verdict      ${e.verdict}`,
                      `count A→B    ${e.count}    B→A: ${e.reverse_count}`,
                      `asymmetry    ${(e.asymmetry * 100).toFixed(0)}%`,
                      `evidence     ${e.evidence_count}/${e.n_windows} sub-windows`,
                      `symmetry     ${(e.symmetry_penalty * 100).toFixed(0)}%`,
                      e.common_driver ? `common driver ${e.common_driver} (strength ${e.common_driver_strength})` : "no common driver detected",
                      ``,
                      `confidence decomposition:`,
                      `  volume    ${(e.factors.volume * 100).toFixed(0)}`,
                      `  asym      ${(e.factors.asymmetry * 100).toFixed(0)}`,
                      `  evidence  ${(e.factors.evidence * 100).toFixed(0)}`,
                      `  cd pen    ${(e.factors.common_driver_penalty * 100).toFixed(0)}`,
                      `  symmetry  ${(e.factors.symmetry * 100).toFixed(0)}`,
                      `  scarcity  ${(e.factors.scarcity * 100).toFixed(0)}`,
                      ``,
                      `rationale: ${e.rationale}`,
                    ].join("\n");
                    const isCert = e.verdict === "DIRECTIONAL";
                    return (
                      <tr
                        key={i}
                        className={`border-t border-border/40 ${isCert ? "" : "opacity-70"}`}
                        title={tip}
                      >
                        <td className="py-1 text-[10px]">
                          <span className="text-zinc-200">{e.from_symbol}</span>
                          <span className="text-muted"> → </span>
                          <span className="text-zinc-200">{e.to_symbol}</span>
                          {e.common_driver && (
                            <span className="ml-1 text-[#e3b457]" title={`common driver ${e.common_driver}`}>↶</span>
                          )}
                        </td>
                        <td className="py-1 text-right text-muted tabular-nums">{e.count}/{e.reverse_count}</td>
                        <td className="py-1 text-right text-zinc-300 tabular-nums">{(e.causal_confidence * 100).toFixed(0)}</td>
                        <td className="py-1"><VerdictChip v={e.verdict} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">influence hierarchy</div>
              <table className="w-full text-sm font-mono">
                <thead>
                  <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                    <th className="text-left py-2">SYMBOL</th>
                    <th className="text-right py-2">OUT/IN</th>
                    <th className="text-right py-2">STAB</th>
                    <th className="text-left py-2">ROLE</th>
                  </tr>
                </thead>
                <tbody>
                  {data.nodes.slice(0, 15).map((n) => {
                    const stabPct = n.stability * 100;
                    const stabColor = stabPct >= 60 ? "text-[#52b97a]" : stabPct >= 30 ? "text-[#e3b457]" : "text-[#d68b8b]";
                    return (
                      <tr key={n.symbol} className="border-t border-border/40" title={n.role_rationale}>
                        <td className="py-1 text-zinc-200">{n.symbol}</td>
                        <td className="py-1 text-right text-muted tabular-nums">{n.out_count}/{n.in_count}</td>
                        <td className={`py-1 text-right tabular-nums ${stabColor}`}>{stabPct.toFixed(0)}</td>
                        <td className="py-1"><RoleChip r={n.role} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Structural Dependencies (Phase 15 #2) ───────────────────────────────

function StructuralDependenciesPanel() {
  const [data, setData] = useState<StructuralDependencies | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getStructuralDependencies()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  return (
    <Panel
      title="Structural Dependencies"
      subtitle={data ? data.summary : "influence chains · co-driver clusters · dominant drivers · synchronized groups"}
      toolbar={data ? <DataQualityChip q={data.data_quality} /> : null}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.exploratory && (
        <div className="text-[10px] text-muted leading-snug mb-3">
          <span className="text-[#e3b457]">All findings are candidates only.</span>{" "}
          Data quality is {data.data_quality} — the layer below shows the structural
          patterns the engine sees, but they are not committed claims of dependency.
        </div>
      )}
      {data && !data.exploratory && (
        <div className="text-[10px] text-muted leading-snug mb-3">
          Composed from {data.directional_edge_count} directional,{" "}
          {data.common_driven_edge_count} common-driven, and{" "}
          {data.coincidence_edge_count} coincidence edges. Each finding inherits the
          causal_confidence of the edges it composes — a chain is only as strong
          as its weakest link.
        </div>
      )}
      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">
              influence chains ({data.influence_chains.length})
            </div>
            {data.influence_chains.length === 0 ? (
              <div className="text-[11px] text-muted">
                {data.exploratory ? "No multi-hop chains — no DIRECTIONAL edges available yet." : "No multi-hop chains detected."}
              </div>
            ) : (
              <ul className="space-y-1.5 font-mono text-[11px]">
                {data.influence_chains.slice(0, 8).map((c, i) => (
                  <li key={i} className="flex items-baseline gap-2" title={c.rationale}>
                    <span className="text-zinc-300 tabular-nums w-10 text-right">
                      {(c.min_confidence * 100).toFixed(0)}
                    </span>
                    <span className="text-zinc-200">
                      {c.path.join(" → ")}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">
              dominant drivers ({data.dominant_drivers.length})
            </div>
            {data.dominant_drivers.length === 0 ? (
              <div className="text-[11px] text-muted">
                {data.exploratory ? "No dominant drivers — no DIRECTIONAL edges available yet." : "No driver dominance detected."}
              </div>
            ) : (
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-[9px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                    <th className="text-left py-1.5">SYMBOL</th>
                    <th className="text-right py-1.5">REACH</th>
                    <th className="text-right py-1.5">OUT</th>
                    <th className="text-right py-1.5">CONF</th>
                  </tr>
                </thead>
                <tbody>
                  {data.dominant_drivers.slice(0, 8).map((d) => (
                    <tr key={d.symbol} className="border-t border-border/40" title={d.rationale + "\nreachable: " + d.reachable_sample.join(", ")}>
                      <td className="py-1 text-zinc-200">{d.symbol}</td>
                      <td className="py-1 text-right text-zinc-300 tabular-nums">{d.reach_size}</td>
                      <td className="py-1 text-right text-muted tabular-nums">{d.direct_out_count}</td>
                      <td className="py-1 text-right text-muted tabular-nums">{(d.avg_out_confidence * 100).toFixed(0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">
              co-driver clusters ({data.dependency_clusters.length})
            </div>
            {data.dependency_clusters.length === 0 ? (
              <div className="text-[11px] text-muted">No common-driver groups detected.</div>
            ) : (
              <ul className="space-y-1.5 font-mono text-[11px]">
                {data.dependency_clusters.slice(0, 6).map((c) => (
                  <li key={c.cluster_id} title={c.rationale} className="leading-tight">
                    <div className="flex items-baseline gap-2">
                      <span className="text-[#e3b457]">{c.driver}</span>
                      <span className="text-muted">drives</span>
                      <span className="text-zinc-300">{c.size}</span>
                    </div>
                    <div className="text-[10px] text-muted truncate">
                      {c.members.join(", ")}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">
              synchronized stress groups ({data.synchronized_groups.length})
            </div>
            {data.synchronized_groups.length === 0 ? (
              <div className="text-[11px] text-muted">No coincidence clusters detected.</div>
            ) : (
              <ul className="space-y-1.5 font-mono text-[11px]">
                {data.synchronized_groups.slice(0, 6).map((g) => (
                  <li key={g.group_id} title={g.rationale} className="leading-tight">
                    <div className="flex items-baseline gap-2">
                      <span className="text-[#d68b8b]">group {g.group_id}</span>
                      <span className="text-muted">·</span>
                      <span className="text-zinc-300">{g.size} symbols</span>
                      <span className="text-muted">·</span>
                      <span className="text-muted">{g.coincidence_edges} pairs</span>
                    </div>
                    <div className="text-[10px] text-muted truncate">
                      {g.members.join(", ")}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Market State Transitions (Phase 15 #3) ──────────────────────────────

const TRANSITION_VERDICT_COLOR: Record<TransitionVerdict, string> = {
  PERSISTENT:   "rgba(82, 185, 122, 0.95)",
  ACCELERATING: "rgba(214, 139, 105, 0.95)",
  FLICKER:      "rgba(140, 140, 140, 0.85)",
  REVERSED:     "rgba(214, 105, 105, 0.95)",
};

function TransitionVerdictChip({ v }: { v: TransitionVerdict }) {
  const color = TRANSITION_VERDICT_COLOR[v];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$|0\.85\)$/, "0.5)"),
        background: color.replace(/0\.95\)$|0\.85\)$/, "0.10)"),
      }}
    >
      {v.toLowerCase()}
    </span>
  );
}

function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600 * 10) / 10}h`;
  return `${Math.round(seconds / 86400 * 10) / 10}d`;
}

function MarketStateTransitionsPanel() {
  const [data, setData] = useState<MarketStateTransitions | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getMarketStateTransitions()
        .then((d) => { if (!cancelled) setData(d); })
        .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  return (
    <Panel
      title="State Transition Intelligence"
      subtitle={data ? data.summary : "how the market moves between coordinated states"}
      toolbar={data ? <DataQualityChip q={data.data_quality} /> : null}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          {/* Current state + vocabulary */}
          <div className="flex flex-wrap items-baseline gap-3 text-[11px] font-mono">
            <div>
              <span className="text-[10px] uppercase tracking-[0.18em] text-muted mr-2">now</span>
              <span className="text-zinc-200">{data.current_state ?? "—"}</span>
              {data.current_state && data.current_state_duration_snapshots > 1 && (
                <span className="text-muted ml-2">
                  for {fmtDuration(data.current_state_duration_seconds / 1000)} ({data.current_state_duration_snapshots} snapshots)
                </span>
              )}
            </div>
            <div className="text-muted">·</div>
            <div>
              <span className="text-[10px] uppercase tracking-[0.18em] text-muted mr-2">vocabulary</span>
              <span className="text-zinc-300">{data.state_vocabulary.length} states</span>
            </div>
            <div className="text-muted">·</div>
            <div>
              <span className="text-[10px] uppercase tracking-[0.18em] text-muted mr-2">rate</span>
              <span className="text-zinc-300">{data.transition_rate_per_day.toFixed(1)}/day</span>
            </div>
            <div className="text-muted">·</div>
            <div>
              <span className="text-[10px] uppercase tracking-[0.18em] text-muted mr-2">flicker</span>
              <span className={data.flicker_ratio < 0.25 ? "text-[#52b97a]" : data.flicker_ratio < 0.5 ? "text-[#e3b457]" : "text-[#d68b8b]"}>
                {(data.flicker_ratio * 100).toFixed(0)}%
              </span>
            </div>
          </div>

          {/* Oscillation warning */}
          {data.oscillation_periods.length > 0 && (
            <div className="rounded-sm border border-[#e3b457]/50 bg-[#e3b457]/10 px-2 py-1.5 text-[10px]">
              <span className="text-[#e3b457] uppercase tracking-[0.16em] mr-2">oscillation</span>
              <span className="text-muted">
                {data.oscillation_periods.length} period(s) where the system fired ≥3 transitions within 1h —
                treat any single transition there as exploratory
              </span>
            </div>
          )}

          {/* Transition table */}
          {data.transitions.length === 0 ? (
            <div className="text-xs text-muted">
              No transitions yet — {data.snapshot_count} snapshot(s) all in same state.
              {data.exploratory && " Engine is still accumulating intelligence history."}
            </div>
          ) : (
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">AT</th>
                  <th className="text-left py-2">FROM → TO</th>
                  <th className="text-right py-2">HELD</th>
                  <th className="text-right py-2">ACCEL</th>
                  <th className="text-right py-2">META</th>
                  <th className="text-right py-2">CONF</th>
                  <th className="text-left py-2">VERDICT</th>
                </tr>
              </thead>
              <tbody>
                {data.transitions.slice(0, 15).map((t, i) => {
                  const at = new Date(t.ts_ms).toISOString().slice(11, 16);
                  const accStr = t.acceleration != null ? `${t.acceleration > 0 ? "+" : ""}${t.acceleration.toFixed(1)}` : "—";
                  const accColor = t.acceleration == null ? "text-muted" :
                    Math.abs(t.acceleration) >= 5 ? "text-[#e3b457]" : "text-muted";
                  return (
                    <tr key={i} className="border-t border-border/40" title={t.rationale}>
                      <td className="py-1 text-[10px] text-muted">{at}</td>
                      <td className="py-1 text-[10px]">
                        <span className="text-muted">{t.from_state.replace(/_/g, " ")}</span>
                        <span className="text-muted"> → </span>
                        <span className="text-zinc-200">{t.to_state.replace(/_/g, " ")}</span>
                      </td>
                      <td className="py-1 text-right text-muted tabular-nums">
                        {fmtDuration(t.persistence_seconds / 1000)}
                      </td>
                      <td className={`py-1 text-right tabular-nums ${accColor}`}>{accStr}</td>
                      <td className="py-1 text-right text-muted tabular-nums">{t.meta_confidence_at.toFixed(0)}</td>
                      <td className="py-1 text-right text-zinc-300 tabular-nums">{(t.confidence * 100).toFixed(0)}</td>
                      <td className="py-1"><TransitionVerdictChip v={t.verdict} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Evolutionary behavior ───────────────────────────────────────────────

const EVOLUTIONARY_STATE_COLOR: Record<string, string> = {
  STABLE_MATURATION: "rgba(82, 185, 122, 0.95)",
  SLOW_MUTATION: "rgba(140, 170, 235, 0.95)",
  INSTABILITY_GROWTH: "rgba(227, 180, 87, 0.95)",
  DETERIORATION: "rgba(214, 75, 75, 0.95)",
};

function EvolutionaryPanel() {
  const [data, setData] = useState<EvolutionaryBehavior | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEvolutionaryBehavior()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Evolutionary Behavior" subtitle="long-horizon structural shift">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted">evolutionary state</div>
            <div className="text-3xl font-bold mt-1" style={{ color: EVOLUTIONARY_STATE_COLOR[data.evolutionary_state] ?? "#5a5f6b" }}>
              {data.evolutionary_state.replace(/_/g, " ")}
            </div>
            <div className="text-[10px] text-muted mt-2 space-y-0.5">
              <div>shift rate <span className="text-zinc-300">{data.behavioral_shift_rate.toFixed(1)}</span></div>
              <div>instability accel <span className="text-zinc-300">{data.instability_acceleration.toFixed(1)}</span></div>
              <div>maturity score <span className="text-zinc-300">{data.structural_maturity_score.toFixed(0)}</span></div>
              <div>bad directions <span className="text-zinc-300">{data.bad_directions}</span></div>
            </div>
          </div>
          <div className="lg:col-span-2">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">metric trend slopes / day</div>
            <ul className="space-y-1 font-mono text-[10px]">
              {data.metric_trends.slice(0, 12).map((t) => {
                const slope = t.slope_per_day;
                const sign = slope == null ? "—" : (slope > 0 ? "+" : "");
                const slopeColor = slope == null ? "text-muted" : slope > 0 ? "text-[#e3b457]" : "text-[#52b97a]";
                return (
                  <li key={t.metric} className="flex items-center justify-between">
                    <span className="text-muted">{t.metric}</span>
                    <span className={slopeColor}>{slope != null ? `${sign}${slope.toExponential(2)}` : "—"}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Memory abstraction ──────────────────────────────────────────────────

function MemoryAbstractionPanel() {
  const [data, setData] = useState<MemoryAbstraction | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMemoryAbstraction()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Memory Abstraction"
      subtitle={data ? `density ${data.memory_density_score.toFixed(0)}% · ${data.total_anomalies} anomalies` : "compressed archetype view"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.abstractions.length === 0 && (
        <div className="text-xs text-muted">Memory is empty.</div>
      )}
      {data && data.abstractions.length > 0 && (
        <div className="space-y-1.5">
          {data.abstractions.map((a) => {
            const color = ARCHETYPE_COLOR[a.label] ?? "rgba(140, 170, 235, 0.95)";
            return (
              <div key={a.archetype_id} className="flex items-center gap-2 text-[10px] font-mono">
                <span className="text-muted w-12">{a.archetype_id}</span>
                <span
                  className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                  style={{
                    color, borderColor: color.replace(/0\.95\)$/, "0.5)"),
                    background: color.replace(/0\.95\)$/, "0.10)"),
                  }}
                >
                  {a.label.replace(/_/g, " ")}
                </span>
                <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                  <div className="h-full" style={{ width: `${(a.share_of_memory * 100).toFixed(0)}%`, background: color }} />
                </div>
                <span className="text-zinc-300 w-12 text-right">{(a.share_of_memory * 100).toFixed(0)}%</span>
                <span className="text-muted w-12 text-right">n={a.members}</span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// ── Intelligence forecast ───────────────────────────────────────────────

const TRAJECTORY_COLOR: Record<string, string> = {
  ESCALATING: "rgba(214, 75, 75, 0.95)",
  DEESCALATING: "rgba(82, 185, 122, 0.95)",
  STEADY: "rgba(140, 170, 235, 0.95)",
  DRIFTING: "rgba(227, 180, 87, 0.95)",
  UNKNOWN: "rgba(140, 140, 140, 0.85)",
};

function IntelligenceForecastPanel() {
  const [data, setData] = useState<IntelligenceForecast | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getIntelligenceForecast(days)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [days]);

  return (
    <Panel
      title="Intelligence Forecast"
      subtitle="OLS extrapolation of engine state (not price)"
      toolbar={
        <div className="flex items-center gap-3">
          <DataQualityChip q={data?.data_quality} />
          <div className="flex items-center gap-1">
            {[3, 7, 14].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                  days === d ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
                }`}
              >
                +{d}d
              </button>
            ))}
          </div>
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.forecasts.length === 0 && (
        <div className="text-xs text-muted">
          {data.data_quality === "INSUFFICIENT"
            ? `Need ≥24 intelligence snapshots — have ${data.snapshot_count}. Forecast suppressed.`
            : "No metrics with enough non-null history yet."}
        </div>
      )}
      {data && data.forecasts.length > 0 && (
        <div className="space-y-3">
          {data.trajectory && (
            <div className="flex items-baseline gap-3">
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted">trajectory</span>
              <span className="text-2xl font-bold" style={{ color: TRAJECTORY_COLOR[data.trajectory] ?? "#5a5f6b" }}>
                {data.trajectory}
              </span>
              <span className="text-[10px] text-muted">{data.snapshot_count} snapshots</span>
            </div>
          )}
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">METRIC</th>
                <th className="text-right py-2">NOW</th>
                <th className="text-right py-2">+{data.horizon_days}D</th>
                <th className="text-right py-2">SLOPE/D</th>
                <th className="text-right py-2">FLAGS</th>
                <th className="text-right py-2">CONF</th>
                <th className="text-right py-2">Q</th>
              </tr>
            </thead>
            <tbody>
              {data.forecasts.map((f) => {
                const tip = [
                  `raw slope     ${f.raw_slope_per_day.toFixed(2)}/d`,
                  `slope_capped  ${f.slope_capped ? "yes" : "no"}`,
                  `extrap_capped ${f.extrapolation_capped ? "yes" : "no"}`,
                  `consistency   ${(f.slope_consistency * 100).toFixed(0)}`,
                  `horizon decay ${f.horizon_decay.toFixed(2)}×`,
                  `rmse          ${f.rmse.toFixed(2)}`,
                ].join("\n");
                return (
                  <tr key={f.metric} className="border-t border-border/40" title={tip}>
                    <td className="py-1.5 text-zinc-200">{f.metric.replace(/_/g, " ")}</td>
                    <td className="py-1.5 text-right text-muted">{f.current.toFixed(1)}</td>
                    <td className="py-1.5 text-right text-zinc-200 font-semibold">{f.forecast_value.toFixed(1)}</td>
                    <td className={`py-1.5 text-right ${f.slope_per_day > 0.1 ? "text-[#e3b457]" : f.slope_per_day < -0.1 ? "text-[#52b97a]" : "text-muted"}`}>
                      {f.slope_per_day > 0 ? "+" : ""}{f.slope_per_day.toFixed(2)}
                    </td>
                    <td className="py-1.5 text-right text-[9px]">
                      {f.slope_capped && <span className="text-[#e3b457] mr-1" title="slope capped at ±25/day">⌛</span>}
                      {f.extrapolation_capped && <span className="text-[#d68b8b] mr-1" title="raw forecast left [0,100] band">⚠</span>}
                      {f.slope_consistency < 0.3 && <span className="text-[#d68b8b]" title="trend reversing between halves">↯</span>}
                    </td>
                    <td className="py-1.5 text-right text-muted">{f.confidence.toFixed(0)}</td>
                    <td className="py-1.5 text-right"><DataQualityChip q={f.data_quality} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── Adaptation recommendations ──────────────────────────────────────────

const ACTION_COLOR: Record<string, string> = {
  STRENGTHEN: "rgba(82, 185, 122, 0.95)",
  STRENGTHEN_EDGE: "rgba(82, 185, 122, 0.95)",
  WEAKEN: "rgba(214, 105, 105, 0.95)",
  TIGHTEN_THRESHOLD: "rgba(227, 180, 87, 0.95)",
  LOOSEN_THRESHOLD: "rgba(140, 170, 235, 0.95)",
  REWEIGHT_UNSTABLE: "rgba(214, 139, 105, 0.95)",
};

function AdaptationPanel() {
  // Use the Phase-16 adapted endpoint so the discovery_suppression_modifier
  // affects what the operator sees. The raw endpoint is still available
  // for callers that want pre-modifier values.
  const [data, setData] = useState<Awaited<ReturnType<typeof getAdaptedRecommendations>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getAdaptedRecommendations();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  return (
    <Panel
      title="Adaptation Recommendations"
      subtitle={
        data
          ? `adaptation score ${data.adaptation_score.toFixed(0)} · ${data.recommendations.length} suggestions${data.modifier_applied ? ` · ×${data.discovery_suppression_modifier.toFixed(2)} suppression applied` : ""}`
          : "what to up-weight / tighten — read-only"
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.modifier_applied && data.modifier_reason && (
        <div className="text-[10px] text-[#e3b457] leading-snug mb-2.5 px-2 py-1.5 rounded border border-[#e3b457]/40 bg-[#e3b457]/10">
          <span className="uppercase tracking-[0.16em] mr-2">feedback loop active</span>
          all importance shifts scaled ×{data.discovery_suppression_modifier.toFixed(2)} — {data.modifier_reason}
        </div>
      )}
      {data && data.recommendations.length === 0 && (
        <div className="text-xs text-muted">Engine has no actionable suggestions right now — calibration is balanced.</div>
      )}
      {data && data.recommendations.length > 0 && (
        <ul className="space-y-2">
          {data.recommendations.map((r, i) => {
            const color = ACTION_COLOR[r.action] ?? "rgba(140, 170, 235, 0.95)";
            return (
              <li key={i} className="rounded border border-border/60 bg-bg/40 px-3 py-2">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span
                    className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                    style={{
                      color,
                      borderColor: color.replace(/0\.95\)$/, "0.5)"),
                      background: color.replace(/0\.95\)$/, "0.10)"),
                    }}
                  >
                    {r.action.replace(/_/g, " ")}
                  </span>
                  <span className="font-mono text-zinc-200">{r.target.replace(/_/g, " ")}</span>
                  <span className="text-[9px] text-muted ml-auto tabular-nums">
                    Δ {r.importance_shift > 0 ? "+" : ""}{r.importance_shift.toFixed(2)}
                    {r.raw_importance_shift != null && Math.abs(r.raw_importance_shift - r.importance_shift) > 1e-9 && (
                      <span className="text-muted/70"> (raw {r.raw_importance_shift > 0 ? "+" : ""}{r.raw_importance_shift.toFixed(2)})</span>
                    )}
                  </span>
                </div>
                <div className="text-[11px] text-zinc-300">{r.rationale}</div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
