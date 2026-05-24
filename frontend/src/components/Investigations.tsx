/**
 * Phase-18 Investigations & Casework Layer.
 *
 * Operator-owned cases that aggregate evidence + notes + lifecycle history
 * around a finding worth tracking over time. Cases are either manually
 * created or auto-drafted by the worker on crisis_genesis = PRE_CASCADE.
 *
 * This panel is a list + drawer-detail view. The drawer shows linked
 * evidence, append-only operator notes, and a hybrid timeline that JOINs
 * upstream events (operator priorities, alerts, anomalies) over the
 * linked evidence. No auto-trading — every action is workflow.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { investigationSeverityLabel } from "../lib/labels";
import {
  addInvestigationNote,
  captureInvestigationReplay,
  retryInvestigationReplayCapture,
  createInvestigation,
  getInvestigation,
  getInvestigationCausalTree,
  getInvestigationExport,
  getInvestigationReplayDiff,
  getInvestigationReplayPropagation,
  getInvestigationReplayState,
  getInvestigationReplayTimeline,
  getInvestigationSimilar,
  getInvestigationTimeline,
  investigationExportDownloadUrl,
  linkInvestigationEvidence,
  listInvestigations,
  unlinkInvestigationEvidence,
  updateInvestigation,
  type Investigation,
  type InvestigationDetail,
  type InvestigationEvidenceInput,
  type InvestigationEvidenceType,
  type InvestigationExport,
  type InvestigationNoteType,
  type InvestigationSeverity,
  type InvestigationSimilar,
  type InvestigationStatus,
  type InvestigationTimeline,
  type InvestigationTree,
  type ReplayDiff,
  type ReplayKeyframe,
  type ReplayPropagation,
  type ReplayState,
  type ReplayTimeline as ReplayTimelineT,
} from "../lib/api";

const REFRESH_MS = 60_000;

const STATUS_COLOR: Record<InvestigationStatus, string> = {
  OPEN: "rgba(140, 170, 235, 0.85)",
  INVESTIGATING: "rgba(227, 180, 87, 0.85)",
  MONITORING: "rgba(140, 170, 235, 0.6)",
  RESOLVED: "rgba(82, 185, 122, 0.85)",
  ARCHIVED: "rgba(125, 125, 125, 0.7)",
};

const SEVERITY_COLOR: Record<InvestigationSeverity, string> = {
  critical: "rgba(221, 99, 99, 0.95)",
  warn: "rgba(227, 180, 87, 0.9)",
  info: "rgba(140, 170, 235, 0.85)",
};

const NOTE_TYPE_LABEL: Record<InvestigationNoteType, string> = {
  note: "note",
  hypothesis: "hypothesis",
  conclusion: "conclusion",
  false_positive: "false positive",
  needs_monitoring: "needs monitoring",
  confirmed_structural: "confirmed structural",
  coincidence: "likely coincidence",
  comment: "comment",
};

const EVIDENCE_TYPE_LABEL: Record<InvestigationEvidenceType, string> = {
  alert: "alert",
  anomaly: "anomaly",
  operator_priority: "priority",
  propagation_edge: "edge",
  causal_chain: "chain",
  narrative_section: "narrative",
  symbol: "symbol",
  transition: "transition",
  dependency_cluster: "cluster",
  file: "file",
};

function fmtTs(ms: number): string {
  const d = new Date(ms);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

function ago(ms: number): string {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function Investigations() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">INV</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          investigation & casework · operator-owned cases · append-only history
        </div>
      </div>
      <InvestigationListPanel />
    </div>
  );
}

function InvestigationListPanel() {
  const [list, setList] = useState<Investigation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("active");
  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [selected, setSelected] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = () =>
    listInvestigations({
      status: statusFilter || undefined,
      severity: (severityFilter || undefined) as InvestigationSeverity | undefined,
      search: search || undefined,
      limit: 200,
    })
      .then((r) => setList(r.items))
      .catch((e) => setError(e?.message ?? "failed"));

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [statusFilter, severityFilter, search]);

  // Cross-component navigation: other pages dispatch
  // "kazus:open-investigation" with detail.case_id to select a case.
  useEffect(() => {
    const handler = (ev: Event) => {
      const detail = (ev as CustomEvent<{ case_id: number }>).detail;
      if (detail?.case_id) {
        setSelected(detail.case_id);
        refresh();
      }
    };
    window.addEventListener("kazus:open-investigation", handler as EventListener);
    return () => window.removeEventListener("kazus:open-investigation", handler as EventListener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) {
    return (
      <div className="rounded-lg border border-border/40 bg-panel/70 px-3 py-3 text-xs text-red-300">
        {error}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border/40 bg-panel/70 px-3 py-3">
      <div className="flex items-baseline gap-3 mb-2.5 flex-wrap">
        <span className="text-[11px] uppercase tracking-[0.22em] text-zinc-200">
          ● cases
        </span>
        <div className="text-[9px] uppercase tracking-[0.14em] flex gap-1.5 items-baseline">
          <span className="text-muted">status:</span>
          {["active", "OPEN", "INVESTIGATING", "MONITORING", "RESOLVED", "ARCHIVED"].map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s === statusFilter ? "" : s)}
              className={`px-1.5 py-0.5 rounded border ${
                statusFilter === s
                  ? "border-accent text-accent"
                  : "border-border/50 text-muted hover:text-zinc-200"
              }`}
            >
              {s.toLowerCase()}
            </button>
          ))}
        </div>
        <div className="text-[9px] uppercase tracking-[0.14em] flex gap-1.5 items-baseline" title="Operator-chosen case priority (not engine severity)">
          <span className="text-muted">priority:</span>
          {(["critical", "warn", "info"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSeverityFilter(s === severityFilter ? "" : s)}
              className={`px-1.5 py-0.5 rounded border ${
                severityFilter === s
                  ? "border-accent text-accent"
                  : "border-border/50 text-muted hover:text-zinc-200"
              }`}
            >
              {investigationSeverityLabel(s)}
            </button>
          ))}
        </div>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="search…"
          className="bg-bg/60 border border-border/40 text-[11px] px-2 py-0.5 rounded text-zinc-200 w-32 focus:border-accent outline-none"
        />
        <button
          onClick={() => setCreating(true)}
          className="ml-auto text-[10px] uppercase tracking-[0.16em] px-2 py-1 rounded border border-accent/60 text-accent hover:bg-accent/10"
        >
          + new
        </button>
      </div>

      {creating && (
        <CreateCaseInline
          onCancel={() => setCreating(false)}
          onCreated={(c) => {
            setCreating(false);
            setSelected(c.id);
            refresh();
          }}
        />
      )}

      {list == null ? (
        <div className="text-[11px] text-muted">loading…</div>
      ) : list.length === 0 ? (
        <div className="text-[11px] text-muted">
          No cases match current filters. Auto-drafts appear on PRE_CASCADE genesis verdicts.
        </div>
      ) : (
        <ul className="space-y-1.5">
          {list.map((c) => (
            <CaseRow
              key={c.id}
              c={c}
              selected={selected === c.id}
              onSelect={() => setSelected(selected === c.id ? null : c.id)}
            />
          ))}
        </ul>
      )}

      {selected != null && (
        <div className="mt-3">
          <CaseDrawer caseId={selected} onChanged={refresh} onClose={() => setSelected(null)} />
        </div>
      )}
    </div>
  );
}

function CaseRow({
  c,
  selected,
  onSelect,
}: {
  c: Investigation;
  selected: boolean;
  onSelect: () => void;
}) {
  const sev = SEVERITY_COLOR[c.severity];
  const st = STATUS_COLOR[c.status];
  return (
    <li
      className={`rounded border px-3 py-2 cursor-pointer ${
        selected ? "border-accent/60 bg-bg/60" : "border-border/40 bg-bg/40 hover:bg-bg/60"
      }`}
      onClick={onSelect}
    >
      <div className="flex items-baseline gap-2 flex-wrap mb-0.5">
        <span className="text-[9px] uppercase tracking-[0.14em] tabular-nums text-muted">
          #{c.id}
        </span>
        <span
          className="text-[9px] uppercase tracking-[0.14em] px-1 py-0.5 rounded-sm border"
          style={{
            color: sev,
            borderColor: sev.replace(/0\.95\)$|0\.9\)$|0\.85\)$/, "0.5)"),
            background: sev.replace(/0\.95\)$|0\.9\)$|0\.85\)$/, "0.10)"),
          }}
          title="Operator-chosen case priority. Not the engine's alert severity."
        >
          {investigationSeverityLabel(c.severity)}
        </span>
        <span
          className="text-[9px] uppercase tracking-[0.14em] px-1 py-0.5 rounded-sm border"
          style={{
            color: st,
            borderColor: st.replace(/0\.85\)$|0\.7\)$|0\.6\)$/, "0.5)"),
            background: st.replace(/0\.85\)$|0\.7\)$|0\.6\)$/, "0.10)"),
          }}
        >
          {c.status}
        </span>
        {c.origin_kind === "auto_pre_cascade" && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#e3b457]">
            auto-draft
          </span>
        )}
        {c.capture_status === "PENDING" && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#8caaeb]" title="frozen snapshot capture queued; worker drains every 30s">
            capturing…
          </span>
        )}
        {c.capture_status === "FAILED" && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#dd6363]" title={c.capture_error || "capture failed; open the case to retry"}>
            capture failed
          </span>
        )}
        {c.tags.map((t) => (
          <span key={t} className="text-[9px] uppercase tracking-[0.14em] text-muted">
            #{t}
          </span>
        ))}
        <span className="ml-auto text-[10px] text-muted tabular-nums" title={fmtTs(c.updated_at_ms)}>
          {ago(c.updated_at_ms)}
        </span>
      </div>
      <div className="text-[12px] text-zinc-200 leading-snug">{c.title}</div>
      {c.description && (
        <div className="text-[10px] text-muted mt-0.5 line-clamp-2">{c.description}</div>
      )}
    </li>
  );
}

function CreateCaseInline({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (c: Investigation) => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<InvestigationSeverity>("warn");
  const [tagsStr, setTagsStr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="rounded border border-accent/40 bg-bg/40 px-3 py-2.5 mb-2.5 space-y-1.5">
      <div className="text-[10px] uppercase tracking-[0.16em] text-accent">new case</div>
      <input
        autoFocus
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="title (required)"
        className="w-full bg-bg/60 border border-border/40 text-[12px] px-2 py-1 rounded text-zinc-200"
      />
      <textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="description / context"
        rows={2}
        className="w-full bg-bg/60 border border-border/40 text-[11px] px-2 py-1 rounded text-zinc-200"
      />
      <div className="flex items-baseline gap-2">
        <span className="text-[9px] uppercase tracking-[0.14em] text-muted">priority:</span>
        {(["critical", "warn", "info"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSeverity(s)}
            title={`Operator-chosen ${investigationSeverityLabel(s)}; not the engine's alert severity`}
            className={`text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border ${
              severity === s
                ? "border-accent text-accent"
                : "border-border/50 text-muted hover:text-zinc-200"
            }`}
          >
            {investigationSeverityLabel(s)}
          </button>
        ))}
        <input
          value={tagsStr}
          onChange={(e) => setTagsStr(e.target.value)}
          placeholder="tags (comma)"
          className="bg-bg/60 border border-border/40 text-[11px] px-2 py-0.5 rounded text-zinc-200 w-40"
        />
        <div className="ml-auto flex gap-1.5">
          <button
            disabled={busy}
            onClick={onCancel}
            className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
          >cancel</button>
          <button
            disabled={busy || !title.trim()}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                const tags = tagsStr.split(",").map((t) => t.trim()).filter(Boolean);
                const c = await createInvestigation({
                  title: title.trim(),
                  description,
                  severity,
                  tags: tags.length ? tags : undefined,
                });
                onCreated(c);
              } catch (e: any) {
                setErr(e?.message ?? "create failed");
              } finally {
                setBusy(false);
              }
            }}
            className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10 disabled:opacity-30"
          >create</button>
        </div>
      </div>
      {err && <div className="text-[10px] text-red-300">{err}</div>}
    </div>
  );
}

function CaseDrawer({
  caseId,
  onChanged,
  onClose,
}: {
  caseId: number;
  onChanged: () => void;
  onClose: () => void;
}) {
  const [data, setData] = useState<InvestigationDetail | null>(null);
  const [timeline, setTimeline] = useState<InvestigationTimeline | null>(null);
  const [tree, setTree] = useState<InvestigationTree | null>(null);
  const [similar, setSimilar] = useState<InvestigationSimilar | null>(null);
  const [exportData, setExportData] = useState<InvestigationExport | null>(null);
  const [tab, setTab] = useState<"evidence" | "notes" | "timeline" | "tree" | "similar" | "export" | "replay">("evidence");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    getInvestigation(caseId).then(setData).catch((e) => setError(e?.message ?? "fetch failed"));
    if (tab === "timeline") getInvestigationTimeline(caseId, 200).then(setTimeline).catch(() => {});
    if (tab === "tree") getInvestigationCausalTree(caseId).then(setTree).catch(() => {});
    if (tab === "similar") getInvestigationSimilar(caseId).then(setSimilar).catch(() => {});
    if (tab === "export") getInvestigationExport(caseId).then(setExportData).catch(() => {});
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  useEffect(() => {
    if (data == null) return;
    if (tab === "timeline" && timeline?.id !== caseId) {
      getInvestigationTimeline(caseId, 200).then(setTimeline).catch(() => {});
    }
    if (tab === "tree" && tree?.id !== caseId) {
      getInvestigationCausalTree(caseId).then(setTree).catch(() => {});
    }
    if (tab === "similar" && similar?.id !== caseId) {
      getInvestigationSimilar(caseId).then(setSimilar).catch(() => {});
    }
    if (tab === "export" && exportData?.id !== caseId) {
      getInvestigationExport(caseId).then(setExportData).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, caseId, data]);

  if (error) return <div className="text-[11px] text-red-300">{error}</div>;
  if (data == null) return <div className="text-[11px] text-muted">loading case…</div>;

  return (
    <div className="rounded border border-border/60 bg-bg/60 px-3 py-2.5">
      <CaseHeader
        c={data}
        onChange={async (payload) => {
          try {
            await updateInvestigation(caseId, payload);
            refresh();
            onChanged();
          } catch (e: any) {
            alert(e?.message ?? "update failed");
          }
        }}
        onClose={onClose}
      />

      <div className="flex items-baseline gap-2 mt-2.5 mb-2 text-[10px] uppercase tracking-[0.16em] flex-wrap">
        {(["evidence", "notes", "timeline", "tree", "similar", "replay", "export"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-2 py-0.5 rounded border ${
              tab === t ? "border-accent text-accent" : "border-border/50 text-muted hover:text-zinc-200"
            }`}
          >
            {t}
            {t === "evidence" && <span className="ml-1 text-muted/70 normal-case">({data.evidence_count})</span>}
            {t === "notes" && <span className="ml-1 text-muted/70 normal-case">({data.note_count})</span>}
            {t === "timeline" && <span className="ml-1 text-muted/70 normal-case">({data.event_count})</span>}
          </button>
        ))}
      </div>

      {tab === "evidence" && (
        <EvidencePanel
          caseId={caseId}
          evidence={data.evidence}
          onRefresh={() => { refresh(); onChanged(); }}
        />
      )}
      {tab === "notes" && (
        <NotesPanel
          caseId={caseId}
          notes={data.notes}
          onRefresh={() => { refresh(); onChanged(); }}
        />
      )}
      {tab === "timeline" && (
        <TimelinePanel timeline={timeline} />
      )}
      {tab === "tree" && (
        <TreePanel tree={tree} />
      )}
      {tab === "similar" && (
        <SimilarPanel
          similar={similar}
          onOpen={(id) => window.dispatchEvent(new CustomEvent("kazus:open-investigation", { detail: { case_id: id } }))}
        />
      )}
      {tab === "export" && (
        <ExportPanel caseId={caseId} exp={exportData} />
      )}
      {tab === "replay" && (
        <ReplayPanel caseId={caseId} />
      )}
    </div>
  );
}

function CaseHeader({
  c,
  onChange,
  onClose,
}: {
  c: InvestigationDetail;
  onChange: (payload: Parameters<typeof updateInvestigation>[1]) => void;
  onClose: () => void;
}) {
  const [resolving, setResolving] = useState(false);
  const [summary, setSummary] = useState(c.resolution_summary ?? "");
  return (
    <div>
      <div className="flex items-baseline gap-2 flex-wrap mb-1.5">
        <span className="text-[10px] uppercase tracking-[0.16em] text-muted tabular-nums">#{c.id}</span>
        <select
          value={c.severity}
          onChange={(e) => onChange({ severity: e.target.value as InvestigationSeverity })}
          title="Operator-chosen case priority (not engine severity)"
          className="bg-bg/60 border border-border/40 text-[10px] px-1 py-0.5 rounded text-zinc-200 uppercase tracking-[0.14em]"
        >
          {(["critical", "warn", "info"] as const).map((s) =>
            <option key={s} value={s}>{investigationSeverityLabel(s)}</option>)}
        </select>
        <select
          value={c.status}
          onChange={(e) => {
            const next = e.target.value as InvestigationStatus;
            if (next === "RESOLVED") {
              setResolving(true);
              return;
            }
            onChange({ status: next });
          }}
          className="bg-bg/60 border border-border/40 text-[10px] px-1 py-0.5 rounded text-zinc-200 uppercase tracking-[0.14em]"
        >
          {(["OPEN", "INVESTIGATING", "MONITORING", "RESOLVED", "ARCHIVED"] as const).map((s) =>
            <option key={s} value={s}>{s}</option>)}
        </select>
        {c.origin_kind === "auto_pre_cascade" && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#e3b457]">auto-draft</span>
        )}
        {c.capture_status === "PENDING" && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#8caaeb]">capturing snapshot…</span>
        )}
        {c.capture_status === "FAILED" && (
          <button
            onClick={async () => {
              try {
                await retryInvestigationReplayCapture(c.id);
                alert("Capture re-queued. Worker drains every 30s.");
              } catch (e: any) {
                alert(e?.message ?? "retry failed");
              }
            }}
            title={c.capture_error || "capture failed; click to re-queue"}
            className="text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-[#dd6363]/60 text-[#dd6363] hover:bg-[#dd6363]/10"
          >capture failed · retry</button>
        )}
        {c.replay_anchor_ms != null && (
          <button
            onClick={() => {
              window.dispatchEvent(new CustomEvent("kazus:open-replay", {
                detail: {
                  symbol: c.primary_symbol,
                  related: c.related_symbols,
                  anchor_ms: c.replay_anchor_ms,
                  window_start_ms: c.replay_window_start_ms,
                  window_end_ms: c.replay_window_end_ms,
                },
              }));
            }}
            className="text-[10px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-[#8caaeb]/50 text-[#8caaeb] hover:bg-[#8caaeb]/10"
            title={`Jump to ${fmtTs(c.replay_anchor_ms)} on ${c.primary_symbol ?? "(no symbol)"}`}
          >
            replay
          </button>
        )}
        <span className="ml-auto text-[10px] text-muted">
          updated {ago(c.updated_at_ms)} · created {ago(c.created_at_ms)}
          {c.last_touched_by != null && c.last_touched_at_ms != null && (
            <> · touched by user_id={c.last_touched_by} {ago(c.last_touched_at_ms)}</>
          )}
        </span>
        <button
          onClick={onClose}
          className="text-[10px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
        >close</button>
      </div>
      <div className="text-[14px] text-zinc-100 font-medium">{c.title}</div>
      {c.description && <div className="text-[11px] text-muted mt-1 whitespace-pre-wrap">{c.description}</div>}
      {c.tags.length > 0 && (
        <div className="text-[10px] text-muted mt-1">
          tags: {c.tags.map((t) => <span key={t} className="mr-2">#{t}</span>)}
        </div>
      )}
      <CaseContextEditors c={c} onChange={onChange} />
      {c.collaborators.length > 0 && (
        <div className="text-[10px] text-muted mt-1">
          collaborators: {c.collaborators.map((u) => <span key={u} className="mr-2">user_id={u}</span>)}
        </div>
      )}
      {c.status === "RESOLVED" && c.resolution_summary && (
        <div className="text-[10px] mt-1 text-[#52b97a]">
          resolved: {c.resolution_summary}
        </div>
      )}
      {resolving && (
        <div className="mt-2 rounded border border-[#52b97a]/40 bg-bg/40 px-2 py-2 space-y-1.5">
          <div className="text-[10px] uppercase tracking-[0.14em] text-[#52b97a]">
            resolution summary (required)
          </div>
          <textarea
            autoFocus
            rows={2}
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="what was the conclusion? what action (if any) was taken?"
            className="w-full bg-bg/60 border border-border/40 text-[11px] px-2 py-1 rounded text-zinc-200"
          />
          <div className="flex gap-1.5 justify-end">
            <button
              onClick={() => { setResolving(false); setSummary(c.resolution_summary ?? ""); }}
              className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted"
            >cancel</button>
            <button
              disabled={!summary.trim()}
              onClick={() => {
                onChange({ status: "RESOLVED", resolution_summary: summary.trim() });
                setResolving(false);
              }}
              className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-[#52b97a]/60 text-[#52b97a] hover:bg-[#52b97a]/10 disabled:opacity-30"
            >resolve</button>
          </div>
        </div>
      )}
    </div>
  );
}

function EvidencePanel({
  caseId,
  evidence,
  onRefresh,
}: {
  caseId: number;
  evidence: InvestigationDetail["evidence"];
  onRefresh: () => void;
}) {
  const [adding, setAdding] = useState(false);
  return (
    <div className="space-y-1.5">
      {evidence.length === 0 && !adding && (
        <div className="text-[11px] text-muted">
          No evidence linked. Use the operator queue's "inv" button to link a priority, or add manually.
        </div>
      )}
      {evidence.map((e) => (
        <div key={e.id} className="rounded border border-border/40 bg-bg/40 px-2 py-1.5 flex items-baseline gap-2">
          <span className="text-[9px] uppercase tracking-[0.14em] text-[#8caaeb] shrink-0">
            {EVIDENCE_TYPE_LABEL[e.evidence_type]}
          </span>
          <span className="text-[11px] font-mono text-zinc-200 truncate flex-1" title={e.ref_key}>
            {e.ref_key}
          </span>
          {e.note && <span className="text-[10px] text-muted truncate max-w-[40%]" title={e.note}>{e.note}</span>}
          <span className="text-[10px] text-muted tabular-nums">{ago(e.linked_at_ms)}</span>
          <button
            onClick={async () => {
              if (!confirm("Unlink this evidence?")) return;
              try {
                await unlinkInvestigationEvidence(caseId, e.id);
                onRefresh();
              } catch (err: any) {
                alert(err?.message ?? "unlink failed");
              }
            }}
            className="text-[9px] uppercase tracking-[0.14em] px-1 py-0.5 rounded border border-border/50 text-muted hover:text-red-300"
          >×</button>
        </div>
      ))}
      {adding ? (
        <EvidenceAddInline
          caseId={caseId}
          onCancel={() => setAdding(false)}
          onLinked={() => { setAdding(false); onRefresh(); }}
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
        >+ link evidence</button>
      )}
    </div>
  );
}

function EvidenceAddInline({
  caseId,
  onCancel,
  onLinked,
}: {
  caseId: number;
  onCancel: () => void;
  onLinked: () => void;
}) {
  const [type, setType] = useState<InvestigationEvidenceType>("symbol");
  const [refKey, setRefKey] = useState("");
  const [refId, setRefId] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="rounded border border-accent/40 bg-bg/40 px-2 py-2 space-y-1.5">
      <div className="flex items-baseline gap-2 flex-wrap">
        <select
          value={type}
          onChange={(e) => setType(e.target.value as InvestigationEvidenceType)}
          className="bg-bg/60 border border-border/40 text-[10px] px-1 py-0.5 rounded text-zinc-200 uppercase tracking-[0.14em]"
        >
          {(Object.keys(EVIDENCE_TYPE_LABEL) as InvestigationEvidenceType[]).map((t) =>
            <option key={t} value={t}>{EVIDENCE_TYPE_LABEL[t]}</option>)}
        </select>
        <input
          value={refKey}
          onChange={(e) => setRefKey(e.target.value)}
          placeholder="ref_key (required)"
          className="flex-1 bg-bg/60 border border-border/40 text-[11px] px-2 py-0.5 rounded text-zinc-200"
        />
        <input
          value={refId}
          onChange={(e) => setRefId(e.target.value)}
          placeholder="ref_id (optional)"
          className="bg-bg/60 border border-border/40 text-[11px] px-2 py-0.5 rounded text-zinc-200 w-28"
        />
      </div>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="why linked? (optional)"
        className="w-full bg-bg/60 border border-border/40 text-[11px] px-2 py-0.5 rounded text-zinc-200"
      />
      <div className="flex gap-1.5 justify-end">
        <button
          onClick={onCancel}
          className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted"
        >cancel</button>
        <button
          disabled={busy || !refKey.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              const payload: InvestigationEvidenceInput = {
                evidence_type: type,
                ref_key: refKey.trim(),
                note: note || null,
              };
              const idNum = refId.trim() ? parseInt(refId.trim(), 10) : NaN;
              if (!Number.isNaN(idNum)) payload.ref_id = idNum;
              await linkInvestigationEvidence(caseId, payload);
              onLinked();
            } catch (err: any) {
              alert(err?.message ?? "link failed");
            } finally {
              setBusy(false);
            }
          }}
          className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10 disabled:opacity-30"
        >link</button>
      </div>
    </div>
  );
}

function NotesPanel({
  caseId,
  notes,
  onRefresh,
}: {
  caseId: number;
  notes: InvestigationDetail["notes"];
  onRefresh: () => void;
}) {
  const [body, setBody] = useState("");
  const [noteType, setNoteType] = useState<InvestigationNoteType>("note");
  const [busy, setBusy] = useState(false);
  return (
    <div className="space-y-1.5">
      <div className="rounded border border-border/40 bg-bg/40 px-2 py-2 space-y-1.5">
        <div className="flex items-baseline gap-2 flex-wrap">
          <select
            value={noteType}
            onChange={(e) => setNoteType(e.target.value as InvestigationNoteType)}
            className="bg-bg/60 border border-border/40 text-[10px] px-1 py-0.5 rounded text-zinc-200 uppercase tracking-[0.14em]"
          >
            {(Object.keys(NOTE_TYPE_LABEL) as InvestigationNoteType[]).map((t) =>
              <option key={t} value={t}>{NOTE_TYPE_LABEL[t]}</option>)}
          </select>
          <span className="text-[10px] text-muted italic">notes are append-only — corrections add new notes</span>
        </div>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={2}
          placeholder="add note…"
          className="w-full bg-bg/60 border border-border/40 text-[11px] px-2 py-1 rounded text-zinc-200"
        />
        <div className="flex justify-end">
          <button
            disabled={busy || !body.trim()}
            onClick={async () => {
              setBusy(true);
              try {
                await addInvestigationNote(caseId, body.trim(), noteType);
                setBody("");
                onRefresh();
              } catch (e: any) {
                alert(e?.message ?? "add note failed");
              } finally {
                setBusy(false);
              }
            }}
            className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10 disabled:opacity-30"
          >add</button>
        </div>
      </div>
      {notes.length === 0 ? (
        <div className="text-[11px] text-muted">No notes yet.</div>
      ) : (
        <ul className="space-y-1">
          {notes.map((n) => (
            <li key={n.id} className="rounded border border-border/40 bg-bg/40 px-2 py-1.5">
              <div className="flex items-baseline gap-2 mb-0.5">
                <span className="text-[9px] uppercase tracking-[0.14em] text-[#8caaeb]">
                  {NOTE_TYPE_LABEL[n.note_type]}
                </span>
                <span className="ml-auto text-[10px] text-muted tabular-nums" title={fmtTs(n.created_at_ms)}>
                  {ago(n.created_at_ms)}
                </span>
              </div>
              <div className="text-[11px] text-zinc-200 whitespace-pre-wrap">{n.body}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CaseContextEditors({
  c,
  onChange,
}: {
  c: InvestigationDetail;
  onChange: (payload: Parameters<typeof updateInvestigation>[1]) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [primary, setPrimary] = useState(c.primary_symbol ?? "");
  const [related, setRelated] = useState(c.related_symbols.join(", "));
  const [collab, setCollab] = useState(c.collaborators.join(", "));
  const [anchor, setAnchor] = useState(c.replay_anchor_ms != null ? new Date(c.replay_anchor_ms).toISOString().slice(0, 16) : "");
  const [handoff, setHandoff] = useState("");
  if (!editing) {
    return (
      <div className="text-[10px] text-muted mt-1 flex gap-2 flex-wrap items-baseline">
        {c.primary_symbol && <span>primary: <span className="text-zinc-200">{c.primary_symbol}</span></span>}
        {c.related_symbols.length > 0 && <span>related: <span className="text-zinc-200">{c.related_symbols.join(", ")}</span></span>}
        <button
          onClick={() => setEditing(true)}
          className="text-[10px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 hover:text-zinc-200"
        >context…</button>
      </div>
    );
  }
  return (
    <div className="mt-1 rounded border border-border/40 bg-bg/40 px-2 py-2 text-[10px] space-y-1">
      <div className="flex gap-2 items-baseline">
        <span className="w-20 uppercase tracking-[0.14em] text-muted">primary</span>
        <input value={primary} onChange={(e) => setPrimary(e.target.value)} placeholder="BTCUSDT"
               className="flex-1 bg-bg/60 border border-border/40 px-2 py-0.5 rounded text-zinc-200" />
      </div>
      <div className="flex gap-2 items-baseline">
        <span className="w-20 uppercase tracking-[0.14em] text-muted">related</span>
        <input value={related} onChange={(e) => setRelated(e.target.value)} placeholder="ETHUSDT, SOLUSDT"
               className="flex-1 bg-bg/60 border border-border/40 px-2 py-0.5 rounded text-zinc-200" />
      </div>
      <div className="flex gap-2 items-baseline">
        <span className="w-20 uppercase tracking-[0.14em] text-muted">collab</span>
        <input value={collab} onChange={(e) => setCollab(e.target.value)} placeholder="user_ids: 2, 3"
               className="flex-1 bg-bg/60 border border-border/40 px-2 py-0.5 rounded text-zinc-200" />
      </div>
      <div className="flex gap-2 items-baseline">
        <span className="w-20 uppercase tracking-[0.14em] text-muted">replay@</span>
        <input type="datetime-local" value={anchor} onChange={(e) => setAnchor(e.target.value)}
               className="flex-1 bg-bg/60 border border-border/40 px-2 py-0.5 rounded text-zinc-200" />
      </div>
      <div className="flex gap-2 items-baseline">
        <span className="w-20 uppercase tracking-[0.14em] text-muted">handoff</span>
        <input value={handoff} onChange={(e) => setHandoff(e.target.value)} placeholder="logged on assignment change"
               className="flex-1 bg-bg/60 border border-border/40 px-2 py-0.5 rounded text-zinc-200" />
      </div>
      <div className="flex justify-end gap-1.5 pt-1">
        <button onClick={() => setEditing(false)}
                className="uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted">cancel</button>
        <button
          onClick={() => {
            const collabIds = collab.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => Number.isFinite(n));
            const relatedArr = related.split(",").map((s) => s.trim()).filter(Boolean);
            const anchorMs = anchor ? new Date(anchor).getTime() : undefined;
            onChange({
              primary_symbol: primary.trim() || null,
              related_symbols: relatedArr,
              collaborators: collabIds,
              replay_anchor_ms: anchorMs ?? undefined,
              handoff_note: handoff.trim() || undefined,
            });
            setEditing(false);
            setHandoff("");
          }}
          className="uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10"
        >save</button>
      </div>
    </div>
  );
}

function TreePanel({ tree }: { tree: InvestigationTree | null }) {
  if (tree == null) return <div className="text-[11px] text-muted">loading tree…</div>;
  if (!tree.found || tree.edges.length === 0) {
    return <div className="text-[11px] text-muted">
      No structural edges found for the linked evidence. Link a symbol or operator priority to grow the tree.
    </div>;
  }
  // Group edges by kind for readability.
  const byKind = new Map<string, InvestigationTree["edges"]>();
  for (const e of tree.edges) {
    const arr = byKind.get(e.kind) ?? [];
    arr.push(e);
    byKind.set(e.kind, arr);
  }
  return (
    <div className="space-y-2">
      <div className="text-[10px] text-muted italic">{tree.rationale_note}</div>
      <div className="text-[10px] text-muted">
        {tree.node_count} nodes · {tree.edge_count} edges · lookback {tree.lookback_days}d
      </div>
      {[...byKind.entries()].map(([kind, edges]) => (
        <div key={kind} className="rounded border border-border/40 bg-bg/40 px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-[0.14em] text-[#8caaeb] mb-1">{kind} ({edges.length})</div>
          <ul className="space-y-1">
            {edges.map((e, i) => (
              <li key={i} className="text-[11px] flex items-baseline gap-2 flex-wrap">
                <span className="font-mono text-zinc-200">{e.edge_from}</span>
                <span className="text-muted">→</span>
                <span className="font-mono text-zinc-200">{e.edge_to}</span>
                <span className="text-[9px] uppercase tracking-[0.14em] text-muted tabular-nums">
                  conf {e.confidence.toFixed(2)}
                </span>
                <span className="text-[10px] text-muted flex-1 min-w-[60%]">{e.rationale}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function SimilarPanel({
  similar,
  onOpen,
}: {
  similar: InvestigationSimilar | null;
  onOpen: (case_id: number) => void;
}) {
  if (similar == null) return <div className="text-[11px] text-muted">loading similar cases…</div>;
  if (!similar.found || similar.similar.length === 0) {
    return (
      <div className="text-[11px] text-muted">
        No prior cases above the similarity floor.
        ({similar.candidates_compared} compared · min score {similar.min_score})
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-muted">
        {similar.similar.length} match(es) · {similar.candidates_compared} compared · min score {similar.min_score}.
        Deterministic scoring — every reason is exposed.
      </div>
      <ul className="space-y-1.5">
        {similar.similar.map((s) => (
          <li key={s.id} className="rounded border border-border/40 bg-bg/40 px-2 py-1.5">
            <div className="flex items-baseline gap-2">
              <button
                onClick={() => onOpen(s.id)}
                className="text-[12px] text-accent hover:underline"
              >#{s.id}</button>
              <span className="text-[12px] text-zinc-200 truncate flex-1">{s.title}</span>
              <span className="text-[9px] uppercase tracking-[0.14em] text-muted">{s.status}</span>
              <span className="text-[9px] uppercase tracking-[0.14em] text-muted">{s.severity}</span>
              <span className="text-[10px] tabular-nums text-accent">score {s.similarity_score}</span>
            </div>
            <ul className="mt-1 text-[10px] text-muted list-disc pl-4 space-y-0.5">
              {s.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExportPanel({ caseId, exp }: { caseId: number; exp: InvestigationExport | null }) {
  if (exp == null) return <div className="text-[11px] text-muted">rendering export…</div>;
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline gap-2">
        <span className="text-[10px] text-muted">
          generated {ago(exp.generated_at_ms)} · {exp.char_count.toLocaleString()} chars
        </span>
        <a
          href={investigationExportDownloadUrl(caseId)}
          download={`investigation-${caseId}.md`}
          className="ml-auto text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10"
        >download .md</a>
        <button
          onClick={() => {
            navigator.clipboard?.writeText(exp.markdown).catch(() => {});
          }}
          className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
        >copy</button>
      </div>
      <pre className="rounded border border-border/40 bg-bg/40 p-2 text-[10px] text-zinc-200 font-mono max-h-[420px] overflow-auto whitespace-pre-wrap">
        {exp.markdown}
      </pre>
    </div>
  );
}

function TimelinePanel({ timeline }: { timeline: InvestigationTimeline | null }) {
  if (timeline == null) return <div className="text-[11px] text-muted">loading timeline…</div>;
  if (!timeline.found || timeline.events.length === 0) {
    return <div className="text-[11px] text-muted">No timeline events.</div>;
  }
  return (
    <ul className="space-y-1">
      {timeline.events.map((e, i) => (
        <li key={i} className="rounded border border-border/30 bg-bg/40 px-2 py-1.5">
          <div className="flex items-baseline gap-2">
            <span className="text-[9px] uppercase tracking-[0.14em] text-muted shrink-0">
              {e.source}
            </span>
            <span className="text-[10px] text-[#8caaeb] truncate">{e.event_type}</span>
            <span className="ml-auto text-[10px] text-muted tabular-nums" title={fmtTs(e.ts_ms)}>
              {fmtTs(e.ts_ms)}
            </span>
          </div>
          {e.note && <div className="text-[10px] text-zinc-200 mt-0.5">{e.note}</div>}
          {e.payload != null && (
            <div className="text-[10px] text-muted font-mono mt-0.5 truncate">
              {Object.entries(e.payload).slice(0, 4).map(([k, v]) =>
                `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("  ")}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

// ── Phase 19 Pass B — Replay surface ─────────────────────────────────
//
// Lazy-mounted forensic replay inside the INV drawer. Composes four
// endpoints: capture / state(frozen) / state(live, at_ms) / timeline /
// diff / propagation. NO cinematic animation — the only moving element
// is the cursor when `playing=true`, advanced by requestAnimationFrame
// against wall-clock time × speed factor. Everything else (overlays,
// mini-charts, propagation frames) is deterministic + cached against
// the cursor position.
//
// Performance: every fetch is one-shot per case open; the cursor moves
// purely client-side over already-fetched keyframes/frames. No polling.

const KEYFRAME_COLOR: Record<string, string> = {
  critical: "rgba(221, 99, 99, 0.9)",
  warn:     "rgba(227, 180, 87, 0.9)",
  info:     "rgba(140, 170, 235, 0.8)",
};

const KEYFRAME_SOURCE_COLOR: Record<string, string> = {
  operator_priority: "rgba(140, 170, 235, 0.95)",
  alert:             "rgba(227, 180, 87, 0.95)",
  anomaly:           "rgba(221, 99, 99, 0.95)",
  case:              "rgba(125, 125, 125, 0.85)",
};

const REPLAY_SPEEDS = [0.5, 1, 2, 4, 8, 16] as const;
type ReplaySpeed = typeof REPLAY_SPEEDS[number];

function ReplayPanel({ caseId }: { caseId: number }) {
  const [state, setState] = useState<ReplayState | null>(null);
  const [liveAtCursor, setLiveAtCursor] = useState<ReplayState | null>(null);
  const [timeline, setTimeline] = useState<ReplayTimelineT | null>(null);
  const [diff, setDiff] = useState<ReplayDiff | null>(null);
  const [propagation, setPropagation] = useState<ReplayPropagation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Source-overlay toggles. Default all on.
  const [overlayOn, setOverlayOn] = useState<Record<string, boolean>>({
    operator_priority: true,
    alert: true,
    anomaly: true,
    case: true,
  });

  // Scrubber state.
  const [cursorMs, setCursorMs] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<ReplaySpeed>(1);
  const [showFrozenOnly, setShowFrozenOnly] = useState(false);

  // Lazy-load all replay data on mount.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      getInvestigationReplayState(caseId, { mode: "frozen" }),
      getInvestigationReplayTimeline(caseId),
      getInvestigationReplayDiff(caseId),
      getInvestigationReplayPropagation(caseId),
    ])
      .then(([st, tl, df, pr]) => {
        if (cancelled) return;
        setState(st);
        setTimeline(tl);
        setDiff(df);
        setPropagation(pr);
        const anchor = tl.anchor_ms ?? st.anchor_ms ?? null;
        if (anchor != null) setCursorMs(anchor);
      })
      .catch((e: any) => { if (!cancelled) setError(e?.message ?? "replay load failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [caseId]);

  // Re-fetch the per-cursor live reconstruction when cursor settles.
  // Debounced so scrubbing doesn't spam the backend.
  useEffect(() => {
    if (cursorMs == null) return;
    const timer = window.setTimeout(() => {
      getInvestigationReplayState(caseId, { mode: "live", at_ms: cursorMs })
        .then(setLiveAtCursor)
        .catch(() => {});
    }, 250);
    return () => window.clearTimeout(timer);
  }, [cursorMs, caseId]);

  // Play loop. RAF-based; speed multiplies wall-clock progression.
  const lastTickRef = useRef<number | null>(null);
  useEffect(() => {
    if (!playing || cursorMs == null || timeline == null) return;
    let raf = 0;
    const step = (now: number) => {
      if (lastTickRef.current == null) lastTickRef.current = now;
      const dtMs = now - lastTickRef.current;
      lastTickRef.current = now;
      // Map 1s wall ≈ 1 minute of case time at speed=1. So at speed=8,
      // 1s wall ≈ 8 minutes case time. Cap to window_end.
      const advance = dtMs * 60 * speed;
      setCursorMs((prev) => {
        if (prev == null) return prev;
        const next = prev + advance;
        if (timeline.window_end_ms != null && next >= timeline.window_end_ms) {
          setPlaying(false);
          lastTickRef.current = null;
          return timeline.window_end_ms;
        }
        return next;
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      lastTickRef.current = null;
    };
  }, [playing, speed, timeline, cursorMs == null]);

  if (loading) return <div className="text-[11px] text-muted">loading replay surface…</div>;
  if (error) return <div className="text-[11px] text-red-300">{error}</div>;
  if (!timeline?.found) {
    return <div className="text-[11px] text-muted">No replay anchor yet — set a replay anchor on the case to enable forensic replay.</div>;
  }

  const visibleKeyframes = timeline.keyframes.filter((k) => overlayOn[k.source]);

  return (
    <div className="space-y-3">
      <ReplayDiffBanner
        diff={diff}
        onRecapture={async () => {
          if (!confirm("Write a new frozen-snapshot revision from the current engine state? Snapshot history is append-only — the prior revision is preserved.")) return;
          try {
            await captureInvestigationReplay(caseId, { force: true });
            const [st, df] = await Promise.all([
              getInvestigationReplayState(caseId, { mode: "frozen" }),
              getInvestigationReplayDiff(caseId),
            ]);
            setState(st); setDiff(df);
          } catch (e: any) { alert(e?.message ?? "recapture failed"); }
        }}
      />

      <ReplayScrubber
        timeline={timeline}
        cursorMs={cursorMs}
        setCursorMs={setCursorMs}
        playing={playing}
        setPlaying={setPlaying}
        speed={speed}
        setSpeed={setSpeed}
        visibleKeyframes={visibleKeyframes}
        overlayOn={overlayOn}
        setOverlayOn={setOverlayOn}
      />

      <ReplayCursorSnapshot
        showFrozenOnly={showFrozenOnly}
        setShowFrozenOnly={setShowFrozenOnly}
        frozen={state}
        live={liveAtCursor}
        cursorMs={cursorMs}
      />

      <ReplayStateEvolution
        timeline={timeline}
        propagation={propagation}
        cursorMs={cursorMs}
      />

      <ReplayPropagationPlayback
        propagation={propagation}
        cursorMs={cursorMs}
      />
    </div>
  );
}

function ReplayDiffBanner({ diff, onRecapture }: { diff: ReplayDiff | null; onRecapture: () => void }) {
  if (diff == null) return null;
  // Integrity Repair Pass §2: explicit labels per comparison_mode.
  // We never call this "FROZEN vs LIVE" — cursor snapshot is a
  // separate panel with different semantics.
  const isFrozenFrozen = diff.comparison_mode === "frozen_vs_frozen";
  const headerLabel = isFrozenFrozen
    ? `● frozen rev ${diff.from_revision} vs rev ${diff.to_revision}`
    : "● frozen vs now (engine current view)";
  if (!diff.frozen_present) {
    return (
      <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2 flex items-baseline gap-3 flex-wrap">
        <span className="text-[11px] uppercase tracking-[0.22em] text-muted">{headerLabel}</span>
        <span className="text-[11px] text-muted flex-1">
          No frozen snapshot yet — capture lands shortly after case creation, or click recapture.
        </span>
        <button
          onClick={onRecapture}
          className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10"
        >capture</button>
      </div>
    );
  }
  const driftCount = diff.diff_count ?? 0;
  const color =
    driftCount === 0 ? "rgba(82, 185, 122, 0.85)"
    : driftCount <= 2 ? "rgba(140, 170, 235, 0.85)"
    : driftCount <= 5 ? "rgba(227, 180, 87, 0.9)"
    : "rgba(221, 99, 99, 0.95)";
  const ageMinutes = Math.round((diff.frozen_age_seconds ?? 0) / 60);
  return (
    <div
      className="rounded-lg border bg-bg/40 px-3 py-2"
      style={{ borderColor: color.replace(/0\.95\)$|0\.9\)$|0\.85\)$/, "0.55)") }}
    >
      <div className="flex items-baseline gap-3 flex-wrap mb-1.5">
        <span className="text-[11px] uppercase tracking-[0.22em]" style={{ color }}>
          {headerLabel}
        </span>
        {!isFrozenFrozen && diff.frozen_revision != null && (
          <span className="text-[9px] uppercase tracking-[0.14em] text-muted">
            frozen rev {diff.frozen_revision} · {ageMinutes}m ago
          </span>
        )}
        <span className="text-[11px] text-zinc-200 flex-1">{diff.summary}</span>
        {!isFrozenFrozen && (
          <button
            onClick={onRecapture}
            title="append-only: writes a new snapshot revision; prior revisions are preserved"
            className="text-[10px] uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200 hover:border-zinc-400"
          >recapture</button>
        )}
      </div>
      {diff.diffs.length > 0 && (
        <ul className="text-[10px] text-zinc-200 space-y-0.5 font-mono">
          {diff.diffs.slice(0, 8).map((d, i) => (
            <li key={i} className="truncate">
              <span className="text-muted mr-2">{d.field}</span>
              {d.delta}
            </li>
          ))}
          {diff.diffs.length > 8 && (
            <li className="text-muted">…+{diff.diffs.length - 8} more</li>
          )}
        </ul>
      )}
      <div className="text-[9px] text-muted italic mt-1.5">
        {isFrozenFrozen
          ? "Diff between two preserved frozen revisions — append-only history."
          : "Comparison source: frozen snapshot vs engine view recomputed right now. NOT a cursor comparison."}
      </div>
    </div>
  );
}

function ReplayScrubber({
  timeline,
  cursorMs,
  setCursorMs,
  playing,
  setPlaying,
  speed,
  setSpeed,
  visibleKeyframes,
  overlayOn,
  setOverlayOn,
}: {
  timeline: ReplayTimelineT;
  cursorMs: number | null;
  setCursorMs: (n: number | null) => void;
  playing: boolean;
  setPlaying: (p: boolean) => void;
  speed: ReplaySpeed;
  setSpeed: (s: ReplaySpeed) => void;
  visibleKeyframes: ReplayKeyframe[];
  overlayOn: Record<string, boolean>;
  setOverlayOn: (next: Record<string, boolean>) => void;
}) {
  const ws = timeline.window_start_ms ?? 0;
  const we = timeline.window_end_ms ?? ws + 1;
  const span = Math.max(1, we - ws);
  const cursorPct = cursorMs != null ? ((cursorMs - ws) / span) * 100 : 0;
  const anchorPct = timeline.anchor_ms != null ? ((timeline.anchor_ms - ws) / span) * 100 : 0;

  // Critical keyframes for prev/next navigation: anything not severity=info.
  const criticalKfs = useMemo(
    () => visibleKeyframes.filter((k) => k.severity_hint && k.severity_hint !== "info" && k.severity_hint !== "INFO"),
    [visibleKeyframes],
  );

  const jumpCritical = (dir: 1 | -1) => {
    if (cursorMs == null || criticalKfs.length === 0) return;
    const sorted = [...criticalKfs].sort((a, b) => a.ts_ms - b.ts_ms);
    const next = dir > 0
      ? sorted.find((k) => k.ts_ms > cursorMs)
      : [...sorted].reverse().find((k) => k.ts_ms < cursorMs);
    if (next) setCursorMs(next.ts_ms);
  };

  const stepBy = (dir: 1 | -1) => {
    if (cursorMs == null) return;
    const sorted = [...visibleKeyframes].sort((a, b) => a.ts_ms - b.ts_ms);
    const next = dir > 0
      ? sorted.find((k) => k.ts_ms > cursorMs)
      : [...sorted].reverse().find((k) => k.ts_ms < cursorMs);
    if (next) setCursorMs(next.ts_ms);
  };

  return (
    <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2.5">
      {/* Transport controls. */}
      <div className="flex items-baseline gap-2 flex-wrap mb-2 text-[10px]">
        <button
          onClick={() => setPlaying(!playing)}
          className="uppercase tracking-[0.14em] px-2 py-0.5 rounded border border-accent/60 text-accent hover:bg-accent/10 w-14 text-center"
        >{playing ? "pause" : "play"}</button>
        <button onClick={() => jumpCritical(-1)}
                className="uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
                title="previous critical keyframe">«crit</button>
        <button onClick={() => stepBy(-1)}
                className="uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
                title="previous keyframe">‹step</button>
        <button onClick={() => stepBy(1)}
                className="uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
                title="next keyframe">step›</button>
        <button onClick={() => jumpCritical(1)}
                className="uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
                title="next critical keyframe">crit»</button>
        <button
          onClick={() => timeline.anchor_ms != null && setCursorMs(timeline.anchor_ms)}
          className="uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border border-border/50 text-muted hover:text-zinc-200"
          title="jump to case anchor"
        >anchor</button>
        <span className="text-muted ml-2 uppercase tracking-[0.14em]">speed</span>
        {REPLAY_SPEEDS.map((s) => (
          <button
            key={s}
            onClick={() => setSpeed(s)}
            className={`px-1.5 py-0.5 rounded border tabular-nums ${
              speed === s ? "border-accent text-accent" : "border-border/50 text-muted hover:text-zinc-200"
            }`}
          >{s}×</button>
        ))}
        <span className="ml-auto text-muted tabular-nums">
          {cursorMs != null ? fmtTs(cursorMs) : "—"}
        </span>
      </div>

      {/* Scrubber strip — SVG, dense but cheap. */}
      <div className="relative h-12 select-none">
        <svg
          width="100%" height="48" viewBox="0 0 1000 48" preserveAspectRatio="none"
          onClick={(ev) => {
            const rect = (ev.currentTarget as SVGSVGElement).getBoundingClientRect();
            const pct = (ev.clientX - rect.left) / rect.width;
            setCursorMs(ws + Math.max(0, Math.min(1, pct)) * span);
          }}
          className="cursor-crosshair"
        >
          {/* Background rail. */}
          <rect x={0} y={20} width={1000} height={8} fill="rgba(255,255,255,0.04)" />
          {/* Anchor mark. */}
          <line x1={anchorPct * 10} x2={anchorPct * 10} y1={4} y2={44}
                stroke="rgba(140,170,235,0.7)" strokeWidth={1} strokeDasharray="2 2" />
          {/* Keyframes. */}
          {visibleKeyframes.map((k, i) => {
            const x = ((k.ts_ms - ws) / span) * 1000;
            const c = KEYFRAME_COLOR[k.severity_hint ?? "info"] ?? KEYFRAME_SOURCE_COLOR[k.source] ?? "rgba(140,170,235,0.7)";
            const isCrit = k.severity_hint && k.severity_hint !== "info" && k.severity_hint !== "INFO";
            return (
              <line
                key={i}
                x1={x} x2={x}
                y1={isCrit ? 8 : 16} y2={isCrit ? 40 : 32}
                stroke={c} strokeWidth={isCrit ? 1.6 : 1.0}
              >
                <title>{`${fmtTs(k.ts_ms)} · ${k.source}/${k.kind}\n${k.label}`}</title>
              </line>
            );
          })}
          {/* Cursor. */}
          {cursorMs != null && (
            <line x1={cursorPct * 10} x2={cursorPct * 10} y1={0} y2={48}
                  stroke="rgba(255,255,255,0.85)" strokeWidth={1.3} />
          )}
        </svg>
      </div>

      <div className="flex items-baseline gap-3 mt-2 text-[9px] uppercase tracking-[0.14em] flex-wrap">
        <span className="text-muted">
          {fmtTs(ws)} → {fmtTs(we)} · {visibleKeyframes.length} keyframe(s)
        </span>
        <span className="text-muted ml-2">overlays:</span>
        {(["operator_priority", "alert", "anomaly", "case"] as const).map((src) => {
          const on = overlayOn[src];
          const color = KEYFRAME_SOURCE_COLOR[src];
          return (
            <button
              key={src}
              onClick={() => setOverlayOn({ ...overlayOn, [src]: !on })}
              className="px-1.5 py-0.5 rounded border tabular-nums"
              style={{
                color: on ? color : "rgba(125,125,125,0.7)",
                borderColor: on ? color.replace(/0\.95\)$/, "0.5)") : "rgba(125,125,125,0.3)",
                background: on ? color.replace(/0\.95\)$/, "0.10)") : "transparent",
              }}
              title={`toggle ${src} overlay`}
            >{src.replace("_", " ")}</button>
          );
        })}
      </div>
    </div>
  );
}

function ReplayCursorSnapshot({
  showFrozenOnly,
  setShowFrozenOnly,
  frozen,
  live,
  cursorMs,
}: {
  showFrozenOnly: boolean;
  setShowFrozenOnly: (b: boolean) => void;
  frozen: ReplayState | null;
  live: ReplayState | null;
  cursorMs: number | null;
}) {
  return (
    <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2.5">
      <div className="flex items-baseline gap-3 mb-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-[0.22em] text-zinc-200">● cursor snapshot</span>
        <span className="text-[10px] text-muted">{cursorMs != null ? fmtTs(cursorMs) : "—"}</span>
        <div className="ml-auto flex gap-1 text-[10px] uppercase tracking-[0.14em]">
          <button
            onClick={() => setShowFrozenOnly(false)}
            className={`px-2 py-0.5 rounded border ${!showFrozenOnly ? "border-accent text-accent" : "border-border/50 text-muted"}`}
          >live @ cursor</button>
          <button
            onClick={() => setShowFrozenOnly(true)}
            className={`px-2 py-0.5 rounded border ${showFrozenOnly ? "border-accent text-accent" : "border-border/50 text-muted"}`}
          >frozen</button>
        </div>
      </div>
      {showFrozenOnly ? (
        <FrozenSnapshotSummary state={frozen} />
      ) : (
        <LiveReconstructionSummary state={live} />
      )}
    </div>
  );
}

function FrozenSnapshotSummary({ state }: { state: ReplayState | null }) {
  if (state == null) return <div className="text-[11px] text-muted">loading frozen…</div>;
  if (!state.snapshot_present) return <div className="text-[11px] text-muted">no frozen snapshot.</div>;
  const p = (state.payload ?? {}) as Record<string, any>;
  const cg = p.crisis_genesis ?? {};
  const sa = p.sanity_audit ?? {};
  const op = p.operator_priorities ?? {};
  const ad = p.adaptation_state ?? {};
  const nc = p.narrative_causality ?? {};
  return (
    <div className="grid grid-cols-2 gap-2 text-[10px]">
      <SnapField label="genesis verdict" value={cg.verdict} />
      <SnapField label="genesis score" value={cg.genesis_score != null ? Math.round(cg.genesis_score) : "—"} />
      <SnapField label="sanity" value={sa.overall_state} />
      <SnapField label="queue total" value={op.total_items} />
      <SnapField label="queue critical" value={(op.escalation_counts ?? {}).CRITICAL ?? 0} />
      <SnapField label="adapt narrative_conf" value={(ad.modifiers ?? {}).narrative_confidence_modifier?.toFixed?.(2)} />
      <SnapField label="adapt alert_sens" value={(ad.modifiers ?? {}).alert_sensitivity_modifier?.toFixed?.(2)} />
      <SnapField label="adapt discovery_suppress" value={(ad.modifiers ?? {}).discovery_suppression_modifier?.toFixed?.(2)} />
      {nc.headline && (
        <div className="col-span-2 text-[10px] text-muted italic mt-1">&ldquo;{nc.headline}&rdquo;</div>
      )}
      <div className="col-span-2 text-[9px] text-muted">
        captured {state.captured_at_ms != null ? fmtTs(state.captured_at_ms) : "—"} · kind {state.captured_kind ?? "—"} · {state.payload_size?.toLocaleString()} bytes
      </div>
    </div>
  );
}

function LiveReconstructionSummary({ state }: { state: ReplayState | null }) {
  if (state == null) return <div className="text-[11px] text-muted">loading reconstruction…</div>;
  const rec = state.reconstructed;
  if (rec == null) return <div className="text-[11px] text-muted">no reconstruction available.</div>;

  const intelQ = rec.intel_snapshot?.data_quality;
  const intel = rec.intel_snapshot?.value;
  const intelColor =
    intelQ === "HIGH" ? "rgba(82, 185, 122, 0.85)"
    : intelQ === "PARTIAL" ? "rgba(227, 180, 87, 0.85)"
    : intelQ === "PRUNED" ? "rgba(221, 99, 99, 0.85)"
    : "rgba(125, 125, 125, 0.7)";

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        <SnapField label="intel quality" value={intelQ ?? "—"} valueColor={intelColor} />
        <SnapField label="stress" value={intel?.synthesized_stress?.toFixed?.(0) ?? "—"} />
        <SnapField label="state" value={intel?.coordinated_state ?? "—"} />
        <SnapField label="meta confidence" value={intel?.meta_confidence_score?.toFixed?.(0) ?? "—"} />
        <SnapField label="break score" value={intel?.structural_break_score?.toFixed?.(0) ?? "—"} />
        <SnapField label="risk state" value={intel?.risk_state_score?.toFixed?.(0) ?? "—"} />
      </div>
      <ReconSection
        title={`active operator priorities (${rec.active_operator_priorities?.length ?? 0})`}
        items={(rec.active_operator_priorities ?? []).slice(0, 5).map((r: any) => ({
          label: `${r.priority_key.slice(0, 64)}`,
          right: `${Math.round(r.priority_score)} · ${r.current_escalation}`,
        }))}
      />
      <ReconSection
        title={`alerts in window (${rec.alerts?.rows?.length ?? 0}, ${rec.alerts?.data_quality})`}
        items={(rec.alerts?.rows ?? []).slice(0, 5).map((a: any) => ({
          label: `${a.symbol} · ${a.kind}`,
          right: `${a.severity} · ${a.priority?.toFixed?.(0)}`,
        }))}
      />
      <ReconSection
        title={`anomalies in window (${rec.anomalies?.rows?.length ?? 0}, ${rec.anomalies?.data_quality})`}
        items={(rec.anomalies?.rows ?? []).slice(0, 5).map((a: any) => ({
          label: `${a.kind}`,
          right: `${a.severity} · novelty ${Math.round(a.novelty_score)}`,
        }))}
      />
      <div className="text-[9px] text-muted">
        live reconstruction at {state.at_ms != null ? fmtTs(state.at_ms) : "—"} ·
        each section publishes its own data_quality. PRUNED = window past retention.
      </div>
    </div>
  );
}

function SnapField({ label, value, valueColor }: { label: string; value: any; valueColor?: string }) {
  return (
    <div className="flex items-baseline justify-between border-b border-border/20 pb-0.5">
      <span className="text-[9px] uppercase tracking-[0.14em] text-muted">{label}</span>
      <span className="text-[11px] text-zinc-200 tabular-nums" style={valueColor ? { color: valueColor } : undefined}>
        {value ?? "—"}
      </span>
    </div>
  );
}

function ReconSection({ title, items }: { title: string; items: { label: string; right: string }[] }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-[0.14em] text-muted mb-0.5">{title}</div>
      {items.length === 0 ? (
        <div className="text-[10px] text-muted italic pl-2">— none —</div>
      ) : (
        <ul className="space-y-0.5">
          {items.map((it, i) => (
            <li key={i} className="text-[10px] text-zinc-200 flex items-baseline gap-2">
              <span className="truncate flex-1">{it.label}</span>
              <span className="text-muted tabular-nums">{it.right}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ReplayStateEvolution({
  timeline,
  propagation,
  cursorMs,
}: {
  timeline: ReplayTimelineT;
  propagation: ReplayPropagation | null;
  cursorMs: number | null;
}) {
  // We don't have a dedicated per-frame state-evolution endpoint in
  // Pass A (would require sampling intel_history across the window).
  // For now derive a few cheap series from data we already have:
  //   * keyframe density per bucket — operator activity proxy
  //   * propagation total per bucket — market activity proxy
  // Both are real (no invention). Replay-position-aware (cursor line).
  const ws = timeline.window_start_ms ?? 0;
  const we = timeline.window_end_ms ?? ws + 1;
  const span = Math.max(1, we - ws);

  const buckets = 60;
  const bucketMs = Math.max(1, Math.floor(span / buckets));

  const kfSeries = useMemo(() => {
    const counts = new Array(buckets).fill(0);
    for (const k of timeline.keyframes) {
      const idx = Math.min(buckets - 1, Math.max(0, Math.floor((k.ts_ms - ws) / bucketMs)));
      counts[idx] += 1;
    }
    return counts;
  }, [timeline, ws, bucketMs]);

  const propSeries = useMemo(() => {
    if (!propagation?.frames) return new Array(buckets).fill(0);
    const counts = new Array(buckets).fill(0);
    for (const f of propagation.frames) {
      const idx = Math.min(buckets - 1, Math.max(0, Math.floor((f.ts_ms - ws) / bucketMs)));
      counts[idx] += f.total_count;
    }
    return counts;
  }, [propagation, ws, bucketMs]);

  const cursorPct = cursorMs != null ? ((cursorMs - ws) / span) : null;

  return (
    <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-[0.22em] text-zinc-200 mb-2">● state evolution</div>
      <Sparkline label="keyframe density" series={kfSeries} cursorPct={cursorPct} color="rgba(140,170,235,0.95)" />
      <Sparkline label="alert activity (per bucket)" series={propSeries} cursorPct={cursorPct} color="rgba(227,180,87,0.95)" />
      <div className="text-[9px] text-muted italic mt-1.5">
        Series derived from already-fetched keyframes + propagation frames. No interpolation, no smoothing.
        For per-bucket synthesized stress / structural break / genesis score, capture-time history sampling lands later.
      </div>
    </div>
  );
}

function Sparkline({ label, series, cursorPct, color }: { label: string; series: number[]; cursorPct: number | null; color: string }) {
  const max = Math.max(1, ...series);
  const w = 1000;
  const h = 36;
  const step = w / Math.max(1, series.length);
  const points = series.map((v, i) => `${(i + 0.5) * step},${h - (v / max) * (h - 4) - 2}`).join(" ");
  return (
    <div className="mb-1.5">
      <div className="flex items-baseline justify-between mb-0.5">
        <span className="text-[9px] uppercase tracking-[0.14em] text-muted">{label}</span>
        <span className="text-[9px] text-muted tabular-nums">max {max}</span>
      </div>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="block">
        <rect x={0} y={0} width={w} height={h} fill="rgba(255,255,255,0.02)" />
        <polyline points={points} fill="none" stroke={color} strokeWidth={1.2} />
        {cursorPct != null && (
          <line x1={cursorPct * w} x2={cursorPct * w} y1={0} y2={h} stroke="rgba(255,255,255,0.6)" strokeWidth={1} />
        )}
      </svg>
    </div>
  );
}

function ReplayPropagationPlayback({
  propagation,
  cursorMs,
}: {
  propagation: ReplayPropagation | null;
  cursorMs: number | null;
}) {
  // Attention & Trust Pass §5: renamed from "propagation playback" to
  // "alert counts at cursor". The bars are NOT animated — playback
  // implied transmission causality the data does not support. Operator
  // scrubs the cursor manually; each frame is a STATIC snapshot of
  // alert-starts per symbol in that bucket.
  if (propagation == null) return null;
  if (!propagation.found || propagation.frames.length === 0) {
    return (
      <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2.5">
        <div className="text-[11px] uppercase tracking-[0.22em] text-zinc-200 mb-1">● alert counts at cursor</div>
        <div className="text-[10px] text-muted">No alerts inside the case window — nothing to show.</div>
      </div>
    );
  }

  // Find the bucket whose [ts, ts+bucket) contains cursor; if cursor
  // is before window start show the first frame.
  const bucket = propagation.bucket_ms ?? 1;
  const activeIdx = useMemo(() => {
    if (cursorMs == null) return 0;
    for (let i = 0; i < propagation.frames.length; i++) {
      const f = propagation.frames[i];
      if (cursorMs >= f.ts_ms && cursorMs < f.ts_ms + bucket) return i;
    }
    return propagation.frames.length - 1;
  }, [cursorMs, propagation, bucket]);

  const active = propagation.frames[activeIdx];
  const maxFrameTotal = Math.max(1, ...propagation.frames.map((f) => f.total_count));

  return (
    <div className="rounded-lg border border-border/40 bg-bg/40 px-3 py-2.5">
      <div className="flex items-baseline gap-3 mb-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-[0.22em] text-zinc-200">● alert counts at cursor</span>
        <span className="text-[10px] text-muted">
          bucket {activeIdx + 1}/{propagation.frame_count} · {Math.round(bucket / 60000)}m wide
        </span>
        <span className="text-[10px] text-muted ml-auto">
          {fmtTs(active.ts_ms)} → {fmtTs(active.ts_ms + bucket)}
        </span>
      </div>

      {/* Active bucket: per-symbol alert-start counts. Static. No
          animation: animation implied causal transmission. */}
      <div className="space-y-1">
        {propagation.symbols.map((sym) => {
          const count = active.per_symbol_count[sym] ?? 0;
          const pct = (count / Math.max(1, maxFrameTotal)) * 100;
          return (
            <div key={sym} className="flex items-baseline gap-2">
              <span className="text-[10px] text-muted font-mono w-20 truncate">{sym}</span>
              <div className="flex-1 h-3 rounded bg-bg/60 border border-border/30 relative overflow-hidden">
                <div
                  className="h-full"
                  style={{
                    width: `${pct}%`,
                    background: count > 0 ? "rgba(227,180,87,0.5)" : "transparent",
                    borderRight: count > 0 ? "1px solid rgba(227,180,87,0.9)" : "none",
                    // No CSS transition. Bars are a snapshot at cursor.
                  }}
                />
              </div>
              <span className="text-[10px] text-muted tabular-nums w-8 text-right">{count}</span>
            </div>
          );
        })}
      </div>
      <div className="text-[9px] text-muted italic mt-1">
        Counts are alert-starts per symbol in this bucket. The bars do NOT animate; advance the cursor to see other buckets.
      </div>

      {/* Static lead-lag edges from propagation_graph. */}
      {propagation.edges.length > 0 && (
        <div className="mt-2 pt-2 border-t border-border/30">
          <div className="text-[9px] uppercase tracking-[0.14em] text-muted mb-1">
            historical lead-lag edges ({propagation.edges.length}) — static, not per-frame
          </div>
          <ul className="space-y-0.5 text-[10px] font-mono">
            {propagation.edges.slice(0, 8).map((e, i) => (
              <li key={i} className="flex items-baseline gap-2">
                <span className="text-zinc-200">{e.edge_from}</span>
                <span className="text-muted">→</span>
                <span className="text-zinc-200">{e.edge_to}</span>
                <span className="text-muted tabular-nums ml-auto">
                  {e.confidence_label ?? "—"} · conf {(e.confidence_score ?? 0).toFixed(2)} · {e.count ?? 0} ev
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {propagation.rationale_note && (
        <div className="text-[9px] text-muted italic mt-1.5">{propagation.rationale_note}</div>
      )}
    </div>
  );
}
