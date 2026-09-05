/**
 * Typed client for the SettleTrace API.
 *
 * Types mirror the Pydantic response models in `settletrace/api/schemas.py`.
 * Amounts arrive as integer paise and are formatted for display at the edge -
 * never converted to a float and passed around, since these are the figures a
 * merchant will quote back to Razorpay.
 */

/**
 * Backend origin. Both names are accepted because the deployment host and the
 * local .env drifted apart, and a mistyped variable name fails as a silent
 * fallback to localhost - which in production means every request fails with
 * no clue why. Trailing slashes are stripped so a pasted dashboard value like
 * "https://api.example.com/" cannot produce "//health".
 */
const CONFIGURED_BASE =
  import.meta.env.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE ?? "";

const BASE = CONFIGURED_BASE.trim().replace(/\/+$/, "") || "http://127.0.0.1:8000";

export type ReasonCode =
  | "unmatched_transaction"
  | "amount_mismatch"
  | "fee_mismatch"
  | "gst_mismatch"
  | "reserve_mismatch"
  | "late_authorization_pending"
  | "settlement_total_mismatch";

export type ExplanationSource = "llm" | "fallback";

export interface Health {
  status: string;
  data_source: string;
  data_source_is_live: boolean;
  llm_explanations: string;
  llm_configured: boolean;
  scheduler_running: boolean;
  data_degraded: boolean;
  degraded_reason: string | null;
}

export interface BatchSummary {
  id: number;
  settlement_id: string;
  started_at: string;
  completed_at: string | null;
  transactions_processed: number;
  transactions_verified: number;
  transactions_exception: number;
  accuracy: number | null;
  accuracy_pct: number | null;
  accuracy_is_measured: boolean;
  is_labeled: boolean;
  precision: number | null;
  recall: number | null;
  precision_pct: number | null;
  recall_pct: number | null;
  f1_pct: number | null;
  true_positives: number | null;
  false_positives: number | null;
  false_negatives: number | null;
}

export interface ExceptionRow {
  id: number;
  batch_id: number;
  reason_code: ReasonCode;
  transaction_id: string | null;
  settlement_id: string | null;
  expected_paise: number;
  actual_paise: number;
  delta_paise: number;
  delta_inr: number;
  impact_inr: number;
  impact_score: number;
  impact_rank: number | null;
  explanation_text: string | null;
  explanation_source: ExplanationSource | null;
  /** Name must match the API's computed field exactly; a mismatch
      reads as undefined -> falsy, silently labelling model output
      as fallback text. */
  is_ai_explained: boolean;
  resolved_flag: boolean;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface StuckOrder {
  order_id: string;
  payment_id: string | null;
  amount_paise: number;
  amount_inr: number;
  method: string;
  local_status: string;
  created_at: string;
  is_stuck_candidate: boolean;
  recheck_attempts: number;
  next_recheck_at: string | null;
}

export interface AuditRow {
  id: number;
  entity_type: string;
  entity_id: string;
  field_changed: string;
  old_value: string | null;
  new_value: string | null;
  changed_by: string;
  changed_at: string;
  reason: string | null;
  is_automatic: boolean;
}

export interface SchedulerStatus {
  running: boolean;
  tick_seconds: number;
  last_run_at: string | null;
  next_run_at: string | null;
  total_ticks: number;
  total_rechecked: number;
  total_corrected: number;
  last_error: string | null;
  recent: Array<{
    at: string;
    newly_stuck: number;
    rechecked: number;
    corrected: number;
  }>;
}

export interface Connectivity {
  reachable: boolean;
  detail: string;
  checked_at: string;
  latency_ms: number | null;
  endpoint: string;
  payments_visible: number | null;
  sample_payment_id: string | null;
  error_kind: string | null;
}

export interface RecheckResult {
  order_id: string;
  previous_status: string;
  actual_status: string;
  corrected: boolean;
  checked_at: string;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    // A dead backend is the most likely failure in a demo, and "Failed to
    // fetch" tells the operator nothing actionable.
    throw new ApiError(
      `Cannot reach the SettleTrace API at ${BASE}. Is the backend running?`,
      0,
    );
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail, response.status);
  }

  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  batches: () => request<BatchSummary[]>("/batches?limit=25"),
  batchSummary: (id: number) => request<BatchSummary>(`/batches/${id}/summary`),

  runBatch: (settlementId: string) =>
    request<BatchSummary>("/batches/settlement", {
      method: "POST",
      body: JSON.stringify({ settlement_id: settlementId, explain: true }),
    }),

  exceptions: (params: {
    batchId?: number;
    reasonCode?: ReasonCode | "";
    search?: string;
    includeResolved?: boolean;
  }) => {
    const q = new URLSearchParams();
    if (params.batchId != null) q.set("batch_id", String(params.batchId));
    if (params.reasonCode) q.set("reason_code", params.reasonCode);
    if (params.search) q.set("search", params.search);
    if (params.includeResolved) q.set("include_resolved", "true");
    return request<ExceptionRow[]>(`/exceptions?${q}`);
  },

  resolve: (id: number, resolvedBy: string) =>
    request<ExceptionRow>(`/exceptions/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolved_by: resolvedBy }),
    }),

  stuckOrders: () => request<StuckOrder[]>("/orders/stuck"),
  recheck: (orderId: string) =>
    request<RecheckResult>(`/orders/${orderId}/recheck`, { method: "POST" }),

  auditLog: () => request<AuditRow[]>("/audit-log?limit=300"),
  schedulerStatus: () => request<SchedulerStatus>("/scheduler/status"),

  /** One real, read-only call to Razorpay. Not part of health: it is a live
      network round trip and does not belong in every page load. */
  connectivity: () => request<Connectivity>("/connectivity/razorpay"),

  /**
   * Download a batch report. The filename comes from Content-Disposition,
   * which the API explicitly exposes via CORS - without that header the
   * browser hides it and every export saves as "download".
   */
  async downloadExport(batchId: number, format: "csv" | "json") {
    const response = await fetch(
      `${BASE}/batches/${batchId}/export?format=${format}`,
    );
    if (!response.ok) {
      throw new ApiError(`Export failed (${response.status})`, response.status);
    }

    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match?.[1] ?? `settletrace-batch${batchId}.${format}`;

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    return filename;
  },
};

// --- display helpers -------------------------------------------------------

export function inr(paise: number): string {
  return `₹${(paise / 100).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export const REASON_LABELS: Record<ReasonCode, string> = {
  fee_mismatch: "Fee mismatch",
  gst_mismatch: "GST mismatch",
  unmatched_transaction: "Unmatched transaction",
  late_authorization_pending: "Late authorisation pending",
  settlement_total_mismatch: "Settlement total mismatch",
  amount_mismatch: "Amount mismatch",
  reserve_mismatch: "Reserve mismatch",
};

/**
 * Fixed reason-code colours.
 *
 * Keyed to the reason code itself, never to a row's position, so a filter that
 * changes which codes are on screen never repaints the survivors - and the
 * same reason is the same colour in the Overview chart and the Exceptions
 * table alike. Drawn from the warm palette rather than default status hues, so
 * they read as one considered set.
 */
export const REASON_COLORS: Record<ReasonCode, string> = {
  fee_mismatch: "#C89B5C",
  late_authorization_pending: "#6B7A4F",
  unmatched_transaction: "#B5544A",
  gst_mismatch: "#D2924C",
  settlement_total_mismatch: "#8F5A46",
  // Not produced by the current engine, but typed as required; kept in family.
  amount_mismatch: "#7A9B76",
  reserve_mismatch: "#8D8677",
};

/**
 * Parse a timestamp from the API as UTC.
 *
 * Two shapes arrive: SQLite-backed rows come back naive (no zone) because
 * SQLite drops tzinfo on write, while the in-memory scheduler emits a full
 * "+00:00" offset. Both are UTC, so a zone is appended only when one is
 * genuinely absent - blindly suffixing "Z" corrupts the offset form into an
 * unparseable string and renders every affected timestamp as a dash.
 */
function parseUtc(iso: string): number {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`).getTime();
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = parseUtc(iso);
  const seconds = Math.round((Date.now() - then) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 0) return `in ${Math.abs(seconds)}s`;
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function countdown(iso: string | null): string {
  if (!iso) return "—";
  const target = parseUtc(iso);
  const seconds = Math.round((target - Date.now()) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  return seconds <= 0 ? "due now" : `in ${seconds}s`;
}
