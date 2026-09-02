/** Tab 2 - the filterable exception list with a slide-in detail panel. */

import { useEffect, useState } from "react";
import {
  REASON_LABELS,
  inr,
  type ExceptionRow,
  type ReasonCode,
} from "../api";
import { X } from "lucide-react";
import {
  Card,
  EmptyState,
  ErrorNote,
  ReasonBadge,
  SectionTitle,
  Spinner,
  StatusPill,
  Table,
  Td,
  Th,
  Tr,
} from "./ui";

/**
 * The explanation callout.
 *
 * The source label is driven by `explanation_source` from the database, never
 * inferred from whether text exists. Template text shown under an
 * "AI-generated" label is the one disclosure failure the design forbids
 * outright, so the badge and the text are read from the same record.
 */
function ExplanationBox({ row }: { row: ExceptionRow }) {
  if (!row.explanation_text) {
    return (
      <p className="text-sm text-muted italic">No explanation recorded.</p>
    );
  }

  const isAi = row.is_ai_explained;
  return (
    <div
      className="rounded-card border px-6 py-5"
      style={{
        borderColor: isAi ? "var(--accent)" : "var(--border)",
        background: isAi
          ? "color-mix(in srgb, var(--accent) 7%, transparent)"
          : "var(--surface-raised)",
      }}
    >
      <div className="mb-3.5">
        <StatusPill tone={isAi ? "accent" : "warning"}>
          {isAi ? "AI-generated explanation" : "Fallback explanation (AI unavailable)"}
        </StatusPill>
      </div>
      <p className="text-sm leading-relaxed text-primary">
        {/* Explanations carry amounts as "INR 1,234.56" so the stored text
            stays ASCII-safe through prompts, exports and logs. Swapped for the
            symbol here so the callout matches every other amount on screen. */}
        {row.explanation_text.replace(/\bINR\s/g, "₹")}
      </p>
      <p className="mt-4 text-xs leading-relaxed text-muted">
        Advisory text only. The match decision and every amount shown here are
        computed deterministically.
      </p>
    </div>
  );
}

function DetailPanel({
  row,
  onClose,
  onResolve,
}: {
  row: ExceptionRow;
  onClose: () => void;
  onResolve: (id: number, resolvedBy: string) => Promise<void>;
}) {
  const [reviewer, setReviewer] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Escape closes the panel - a modal that traps the user is worse than no
  // modal, especially one opened by a stray row click.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function handleResolve() {
    if (!reviewer.trim()) {
      setError("Enter your name — the audit trail records who signed this off.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onResolve(row.id, reviewer.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div
        className="animate-fade-in fixed inset-0 z-40"
        style={{ background: "rgba(0,0,0,0.5)", backdropFilter: "blur(2px)", WebkitBackdropFilter: "blur(2px)" }}
        onClick={onClose}
        aria-hidden
      />
      <aside
        className="animate-slide-in fixed right-0 top-0 z-50 h-full w-full max-w-xl overflow-y-auto border-l"
        style={{ borderColor: "var(--border)", background: "var(--bg)" }}
        role="dialog"
        aria-label="Exception detail"
      >
        <div
          className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b px-8 py-6"
          style={{ borderColor: "var(--border)", background: "var(--bg)" }}
        >
          <div>
            <ReasonBadge code={row.reason_code} />
            <p className="mt-1.5 text-xs text-secondary">
              Rank #{row.impact_rank} by revenue at stake
            </p>
          </div>
          <button className="btn-ghost !p-2" onClick={onClose} aria-label="Close">
            <X size={18} strokeWidth={1.5} />
          </button>
        </div>

        <div className="space-y-7 px-8 py-7">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="label">Transaction</div>
              <div className="id">{row.transaction_id ?? "—"}</div>
            </div>
            <div>
              <div className="label">Settlement</div>
              <div className="id">{row.settlement_id ?? "—"}</div>
            </div>
          </div>

          <div className="divide-y rounded-card border" style={{ borderColor: "var(--border)" }}>
            {[
              ["Expected", inr(row.expected_paise), "text-primary"],
              ["Actual", inr(row.actual_paise), "text-primary"],
              [
                "Difference",
                inr(row.delta_paise),
                row.delta_paise > 0 ? "text-danger-text" : "text-success-text",
              ],
              ["Value at risk", inr(row.impact_score), "text-warning-text"],
            ].map(([label, value, tone]) => (
              <div
                key={label}
                className="flex items-center justify-between px-5 py-3.5"
              >
                <span className="text-sm text-secondary">{label}</span>
                <span className={`text-sm font-semibold tabular ${tone}`}>
                  {value}
                </span>
              </div>
            ))}
          </div>

          <div>
            <div className="label">Explanation</div>
            <ExplanationBox row={row} />
          </div>

          <div className="border-t pt-7" style={{ borderColor: "var(--border)" }}>
            {row.resolved_flag ? (
              <div className="flex items-center gap-2">
                <StatusPill tone="success">Resolved</StatusPill>
                <span className="text-sm text-secondary">
                  by {row.resolved_by}
                </span>
              </div>
            ) : (
              <>
                <label className="label" htmlFor="reviewer">
                  Resolved by
                </label>
                <div className="flex gap-2">
                  <input
                    id="reviewer"
                    className="input flex-1"
                    placeholder="Your name"
                    value={reviewer}
                    onChange={(e) => setReviewer(e.target.value)}
                    disabled={saving}
                  />
                  <button
                    className="btn-primary"
                    onClick={handleResolve}
                    disabled={saving}
                  >
                    {saving ? <Spinner /> : "Mark resolved"}
                  </button>
                </div>
                {error && (
                  <div className="mt-3">
                    <ErrorNote message={error} />
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}

export function Exceptions({
  exceptions,
  loading,
  error,
  onResolve,
}: {
  exceptions: ExceptionRow[];
  loading: boolean;
  error: string | null;
  onResolve: (id: number, resolvedBy: string) => Promise<void>;
}) {
  const [reasonFilter, setReasonFilter] = useState<ReasonCode | "">("");
  const [search, setSearch] = useState("");
  const [showResolved, setShowResolved] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);

  // Filtering narrows the view only; `exceptions` is never mutated, so
  // clearing a filter always restores the full list.
  const visible = exceptions.filter((row) => {
    if (reasonFilter && row.reason_code !== reasonFilter) return false;
    if (!showResolved && row.resolved_flag) return false;
    if (search) {
      const term = search.trim().toLowerCase();
      const haystack = `${row.transaction_id ?? ""} ${row.settlement_id ?? ""}`;
      if (!haystack.toLowerCase().includes(term)) return false;
    }
    return true;
  });

  const present = [...new Set(exceptions.map((e) => e.reason_code))];
  const selectedRow = exceptions.find((e) => e.id === selected) ?? null;

  return (
    <Card>
      <SectionTitle hint="Ordered by revenue at stake — the largest discrepancy first. Select a row for detail.">
        Exception list
      </SectionTitle>

      <div className="mb-6 flex flex-wrap gap-5">
        <div>
          <label className="label" htmlFor="reason">
            Reason code
          </label>
          <select
            id="reason"
            className="input min-w-[14rem]"
            value={reasonFilter}
            onChange={(e) => setReasonFilter(e.target.value as ReasonCode | "")}
          >
            <option value="">All reason codes</option>
            {present.map((code) => (
              <option key={code} value={code}>
                {REASON_LABELS[code] ?? code}
              </option>
            ))}
          </select>
        </div>
        <div className="flex-1 min-w-[14rem]">
          <label className="label" htmlFor="search">
            Search
          </label>
          <input
            id="search"
            className="input w-full"
            placeholder="Transaction or settlement ID…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="flex items-end pb-2">
          <label className="flex cursor-pointer items-center gap-2.5 text-sm text-primary">
            <input
              type="checkbox"
              className="h-4 w-4"
              style={{ accentColor: "var(--accent)" }}
              checked={showResolved}
              onChange={(e) => setShowResolved(e.target.checked)}
            />
            Show resolved
          </label>
        </div>
      </div>

      <p className="tabular mb-4 text-xs text-secondary">
        Showing {visible.length} of {exceptions.length} exceptions
      </p>

      {error ? (
        <ErrorNote message={error} />
      ) : loading ? (
        <Spinner label="Loading exceptions…" />
      ) : exceptions.length === 0 ? (
        <EmptyState
          title="0 exceptions — everything reconciled"
          detail="No transaction in this batch failed a check."
          tone="success"
        />
      ) : visible.length === 0 ? (
        <EmptyState
          title="No exceptions match the current filter"
          detail="Clear the filter to see the full list."
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th align="right">Rank</Th>
              <Th>Reason</Th>
              <Th>Transaction</Th>
              <Th align="right">Expected</Th>
              <Th align="right">Actual</Th>
              <Th align="right">Difference</Th>
              <Th align="right">At risk</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <Tr
                key={row.id}
                selected={row.id === selected}
                onClick={() => setSelected(row.id)}
              >
                <Td align="right" className="tabular text-secondary">
                  {row.impact_rank}
                </Td>
                <Td>
                  <ReasonBadge code={row.reason_code} />
                </Td>
                <Td>
                  <span className="id">{row.transaction_id ?? "—"}</span>
                </Td>
                <Td align="right" className="tabular">
                  {inr(row.expected_paise)}
                </Td>
                <Td align="right" className="tabular">
                  {inr(row.actual_paise)}
                </Td>
                <Td
                  align="right"
                  className={`tabular font-medium ${
                    row.delta_paise > 0 ? "text-danger-text" : "text-success-text"
                  }`}
                >
                  {inr(row.delta_paise)}
                </Td>
                <Td align="right" className="tabular font-semibold text-warning-text">
                  {inr(row.impact_score)}
                </Td>
                <Td>
                  {row.resolved_flag ? (
                    <StatusPill tone="success">Resolved</StatusPill>
                  ) : (
                    <StatusPill tone="danger">Open</StatusPill>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}

      {selectedRow && (
        <DetailPanel
          row={selectedRow}
          onClose={() => setSelected(null)}
          onResolve={onResolve}
        />
      )}
    </Card>
  );
}
