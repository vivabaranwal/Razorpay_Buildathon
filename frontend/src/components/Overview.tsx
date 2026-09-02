/** Tab 1 - headline metrics, the reason breakdown, and batch actions. */

import { useState } from "react";
import {
  REASON_COLORS,
  REASON_LABELS,
  api,
  inr,
  type BatchSummary,
  type ExceptionRow,
  type ReasonCode,
} from "../api";
import { Card, EmptyState, ErrorNote, SectionTitle, Spinner, Stat } from "./ui";

/**
 * Counts and value at risk per reason code.
 *
 * Built as a labelled row list rather than a plotted bar chart: the label sits
 * in its own column instead of being squeezed against an axis, so a long reason
 * name is never truncated, and the count and amount are both readable without
 * hovering.
 */
function ReasonBreakdown({ exceptions }: { exceptions: ExceptionRow[] }) {
  const open = exceptions.filter((e) => !e.resolved_flag);
  if (open.length === 0) {
    return (
      <EmptyState
        title="0 exceptions — everything reconciled"
        detail="Every transaction in this batch matched within tolerance."
        tone="success"
      />
    );
  }

  const grouped = new Map<ReasonCode, { count: number; atRisk: number }>();
  for (const row of open) {
    const entry = grouped.get(row.reason_code) ?? { count: 0, atRisk: 0 };
    entry.count += 1;
    entry.atRisk += row.impact_score;
    grouped.set(row.reason_code, entry);
  }

  const rows = [...grouped.entries()].sort((a, b) => b[1].count - a[1].count);
  const largest = Math.max(...rows.map(([, v]) => v.count));

  return (
    <div className="space-y-4">
      {rows.map(([code, { count, atRisk }]) => (
        <div key={code} className="flex items-center gap-6">
          <div className="w-56 shrink-0 text-right text-sm text-secondary">
            {REASON_LABELS[code] ?? code}
          </div>
          <div className="min-w-[3rem] flex-1">
            <div
              className="h-7 rounded-r-[4px]"
              style={{
                width: `${Math.max((count / largest) * 100, 2)}%`,
                background: REASON_COLORS[code] ?? "var(--text-muted)",
              }}
            />
          </div>
          <div className="tabular w-48 shrink-0 text-sm">
            <span className="font-display text-base font-semibold text-primary">
              {count}
            </span>
            <span className="text-secondary"> · {inr(atRisk)} at risk</span>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Detection quality against the labelled batch.
 *
 * Precision and recall are shown side by side rather than blended into one
 * figure: a system that flags every row scores perfect recall, and only
 * precision reveals it. They call for opposite fixes, so a single number would
 * hide which one this batch needs.
 */
function DetectionQuality({ batch }: { batch: BatchSummary }) {
  if (batch.precision_pct == null || batch.recall_pct == null) {
    return (
      <Card>
        <SectionTitle hint="This batch ran against unlabelled data. Without a known correct answer there is nothing to measure detection against, so the match rate above is throughput, not accuracy.">
          Detection quality
        </SectionTitle>
        <p className="text-sm text-warning-text">
          Not measurable on this batch.
        </p>
      </Card>
    );
  }

  return (
    <Card>
      <SectionTitle hint="Measured against the batch's known planted defects.">
        Detection quality
      </SectionTitle>
      <div className="mb-6 grid grid-cols-3 gap-8">
        {[
          ["Precision", batch.precision_pct, "of flags were real"],
          ["Recall", batch.recall_pct, "of defects found"],
          ["F1", batch.f1_pct, "harmonic mean"],
        ].map(([label, value, hint]) => (
          <div key={label as string}>
            <div className="label">{label as string}</div>
            <div
              className="font-display tabular mt-2 text-3xl font-semibold text-primary"
              style={{ fontVariationSettings: '"opsz" 60' }}
            >
              {value as number}%
            </div>
            <div className="mt-1.5 text-xs text-muted">{hint as string}</div>
          </div>
        ))}
      </div>
      <div
        className="tabular flex gap-8 border-t pt-5 text-xs text-secondary"
        style={{ borderColor: "var(--border)" }}
      >
        <span>True positives: {batch.true_positives}</span>
        <span>False positives: {batch.false_positives}</span>
        <span>False negatives: {batch.false_negatives}</span>
      </div>
    </Card>
  );
}

export function Overview({
  batch,
  exceptions,
  loading,
  onRunBatch,
}: {
  batch: BatchSummary | null;
  exceptions: ExceptionRow[];
  loading: boolean;
  onRunBatch: (settlementId: string) => Promise<void>;
}) {
  const [settlementId, setSettlementId] = useState("setl_DEMO001");
  const [running, setRunning] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function handleRun() {
    setRunning(true);
    setError(null);
    setNote(null);
    try {
      await onRunBatch(settlementId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  async function handleExport(format: "csv" | "json") {
    if (!batch) return;
    setExporting(true);
    setError(null);
    try {
      const filename = await api.downloadExport(batch.id, format);
      setNote(`Downloaded ${filename}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
    }
  }

  const openExceptions = exceptions.filter((e) => !e.resolved_flag);
  const atRisk = openExceptions.reduce((sum, e) => sum + e.impact_score, 0);

  return (
    <div className="space-y-8">
      <Card>
        <div className="flex flex-wrap items-end gap-5">
          <div className="flex-1 min-w-[16rem]">
            <label className="label" htmlFor="settlement">
              Settlement ID
            </label>
            <input
              id="settlement"
              className="input w-full"
              value={settlementId}
              onChange={(e) => setSettlementId(e.target.value)}
              disabled={running}
            />
          </div>
          <button
            className="btn-primary"
            onClick={handleRun}
            disabled={running || !settlementId.trim()}
          >
            {running ? <Spinner label="Reconciling…" /> : "Run reconciliation"}
          </button>
          <button
            className="btn-ghost"
            onClick={() => handleExport("csv")}
            disabled={!batch || exporting}
          >
            {exporting ? "Preparing…" : "Export CSV"}
          </button>
          <button
            className="btn-ghost"
            onClick={() => handleExport("json")}
            disabled={!batch || exporting}
          >
            Export JSON
          </button>
        </div>
        {error && (
          <div className="mt-4">
            <ErrorNote message={error} />
          </div>
        )}
        {note && <p className="mt-4 text-sm text-success-text">{note}</p>}
      </Card>

      {loading && !batch ? (
        <Card>
          <Spinner label="Loading batch…" />
        </Card>
      ) : !batch ? (
        <EmptyState
          title="No batches yet"
          detail="Run a reconciliation above to get started."
        />
      ) : (
        <>
          {/* Deliberately asymmetric: accuracy and value at risk are the two
              numbers a merchant opens this page for, so they take a full half
              of the grid each and carry the display type and glow. Throughput
              and exception count are context, sized as such. */}
          <div className="grid gap-6 lg:grid-cols-2">
            <Stat
              label="Auto-matched"
              value={`${batch.accuracy_pct?.toFixed(1) ?? "—"}%`}
              hero
              countTo={batch.accuracy_pct ?? 0}
              format={(n) => `${n.toFixed(1)}%`}
              tone={
                (batch.accuracy_pct ?? 0) >= 95
                  ? "success"
                  : (batch.accuracy_pct ?? 0) >= 85
                    ? "warning"
                    : "danger"
              }
              sub={
                batch.accuracy_is_measured
                  ? "Measured against known ground truth"
                  : "Throughput only — this data is unlabelled"
              }
            />
            <Stat
              label="Value at risk"
              value={inr(atRisk)}
              hero
              countTo={atRisk}
              format={(n) => inr(Math.round(n))}
              tone={atRisk > 0 ? "danger" : "success"}
              sub="Across open exceptions"
            />
          </div>

          <div className="grid gap-6 sm:grid-cols-2">
            <Stat
              label="Transactions processed"
              value={batch.transactions_processed.toLocaleString("en-IN")}
              sub={`${batch.transactions_verified.toLocaleString("en-IN")} verified`}
            />
            <Stat
              label="Open exceptions"
              value={String(openExceptions.length)}
              tone={openExceptions.length === 0 ? "success" : "warning"}
              sub={`${exceptions.length} total in this batch`}
            />
          </div>

          {!batch.accuracy_is_measured && (
            <div className="rounded-card border px-6 py-5"
              style={{ borderColor: "var(--warning)", background: "color-mix(in srgb, var(--warning) 8%, transparent)" }}>
              <p className="text-sm leading-relaxed text-warning-text">
                This batch ran against unlabelled data, so the match rate is a
                throughput figure, not a measured accuracy result.
              </p>
            </div>
          )}

          <Card>
            <SectionTitle hint="Open exceptions grouped by cause, with the value at stake for each.">
              Exceptions by reason
            </SectionTitle>
            <ReasonBreakdown exceptions={exceptions} />
          </Card>

          <DetectionQuality batch={batch} />
        </>
      )}
    </div>
  );
}
