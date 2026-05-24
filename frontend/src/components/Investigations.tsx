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

import { useEffect, useState } from "react";
import {
  addInvestigationNote,
  createInvestigation,
  getInvestigation,
  getInvestigationCausalTree,
  getInvestigationExport,
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
        <div className="text-[9px] uppercase tracking-[0.14em] flex gap-1.5 items-baseline">
          <span className="text-muted">sev:</span>
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
              {s}
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
        >
          {c.severity}
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
        <span className="text-[9px] uppercase tracking-[0.14em] text-muted">sev:</span>
        {(["critical", "warn", "info"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSeverity(s)}
            className={`text-[9px] uppercase tracking-[0.14em] px-1.5 py-0.5 rounded border ${
              severity === s
                ? "border-accent text-accent"
                : "border-border/50 text-muted hover:text-zinc-200"
            }`}
          >
            {s}
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
  const [tab, setTab] = useState<"evidence" | "notes" | "timeline" | "tree" | "similar" | "export">("evidence");
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
        {(["evidence", "notes", "timeline", "tree", "similar", "export"] as const).map((t) => (
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
          className="bg-bg/60 border border-border/40 text-[10px] px-1 py-0.5 rounded text-zinc-200 uppercase tracking-[0.14em]"
        >
          {(["critical", "warn", "info"] as const).map((s) => <option key={s} value={s}>{s}</option>)}
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
