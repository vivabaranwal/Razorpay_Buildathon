/** Tab 3 - stuck payments and the visible automatic re-check cycle. */

import { useState } from "react";
import {
  api,
  countdown,
  inr,
  relativeTime,
  type Connectivity,
  type SchedulerStatus,
  type StuckOrder,
} from "../api";
import {
  Card,
  EmptyState,
  ErrorNote,
  SectionTitle,
  Spinner,
  StatusPill,
  Table,
  Td,
  Th,
  Tr,
} from "./ui";

/**
 * The automation's heartbeat.
 *
 * Shown prominently because a background process the operator cannot observe is
 * indistinguishable from one that has quietly died - and FR-2.2's whole point
 * is that re-checks happen without anyone clicking anything.
 */
function SchedulerPanel({ status }: { status: SchedulerStatus | null }) {
  if (!status) {
    return (
      <Card>
        <Spinner label="Checking automation status…" />
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <SectionTitle hint="Re-checks run on their own schedule; no button required.">
            Automatic re-check cycle
          </SectionTitle>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <StatusPill
              tone={status.running ? "success" : "danger"}
              pulse={status.running}
            >
              {status.running ? "Running" : "Stopped"}
            </StatusPill>
            <span className="text-secondary">
              Last check: {relativeTime(status.last_run_at)}
            </span>
            <span className="text-secondary">
              Next check: {countdown(status.next_run_at)}
            </span>
          </div>
        </div>

        <div className="flex gap-6 text-right">
          {[
            ["Ticks", status.total_ticks],
            ["Re-checked", status.total_rechecked],
            ["Corrected", status.total_corrected],
          ].map(([label, value]) => (
            <div key={label as string}>
              <div className="label">{label as string}</div>
              <div className="font-display tabular mt-1.5 text-2xl font-semibold text-primary">
                {value as number}
              </div>
            </div>
          ))}
        </div>
      </div>

      {status.last_error && (
        <div className="mt-4">
          <ErrorNote message={`Last tick failed: ${status.last_error}`} />
        </div>
      )}

      {status.recent.length > 0 && (
        <div className="mt-6 border-t pt-5" style={{ borderColor: "var(--border)" }}>
          <div className="label mb-3">Recent ticks</div>
          <div className="flex flex-wrap gap-2">
            {status.recent.slice(0, 8).map((tick) => (
              <span
                key={tick.at}
                className="tabular rounded-control border px-2.5 py-1.5 text-xs"
                style={
                  tick.corrected > 0
                    ? {
                        borderColor: "var(--success)",
                        color: "var(--success-text)",
                        background: "color-mix(in srgb, var(--success) 8%, transparent)",
                      }
                    : { borderColor: "var(--border)", color: "var(--text-secondary)" }
                }
                title={new Date(tick.at).toLocaleString()}
              >
                {relativeTime(tick.at)} · {tick.rechecked} checked
                {tick.corrected > 0 && ` · ${tick.corrected} fixed`}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

/**
 * A live, read-only call to Razorpay, on demand.
 *
 * Kept behind a button rather than run on load: it is a real network round
 * trip to a third party, and putting an external dependency in the path of
 * every page render would make the dashboard as slow and as flaky as the
 * slowest thing it talks to.
 */
function ConnectivityPanel() {
  const [result, setResult] = useState<Connectivity | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function check() {
    setChecking(true);
    setError(null);
    try {
      setResult(await api.connectivity());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }

  const tone = !result
    ? "muted"
    : result.reachable
      ? "success"
      : result.error_kind === "not_configured"
        ? "muted"
        : "danger";

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex-1 min-w-[18rem]">
          <SectionTitle hint="Makes one real read-only call to Razorpay's API with your configured test-mode credentials.">
            Razorpay connectivity
          </SectionTitle>

          {result ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-3">
                <StatusPill tone={tone as "success" | "danger" | "muted"}>
                  {result.reachable ? "Connected" : "Not connected"}
                </StatusPill>
                <span className="id">{result.endpoint}</span>
                {result.latency_ms != null && (
                  <span className="text-xs text-muted tabular">
                    {result.latency_ms} ms
                  </span>
                )}
              </div>
              <p className="text-sm text-slate-300">{result.detail}</p>
              {result.sample_payment_id && (
                <p className="text-xs text-secondary">
                  Sample payment read:{" "}
                  <span className="id">{result.sample_payment_id}</span>
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted">
              Not checked yet in this session.
            </p>
          )}

          {error && (
            <div className="mt-3">
              <ErrorNote message={error} />
            </div>
          )}
        </div>

        <button className="btn-ghost" onClick={check} disabled={checking}>
          {checking ? <Spinner label="Calling Razorpay…" /> : "Test connection"}
        </button>
      </div>
    </Card>
  );
}

export function PaymentState({
  orders,
  scheduler,
  loading,
  error,
  onRefresh,
}: {
  orders: StuckOrder[];
  scheduler: SchedulerStatus | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function handleRecheck(orderId: string) {
    setBusy(orderId);
    setNote(null);
    setActionError(null);
    try {
      const result = await api.recheck(orderId);
      setNote(
        result.corrected
          ? `Corrected ${result.order_id}: ${result.previous_status} → ${result.actual_status}`
          : `${result.order_id} still reports ${result.actual_status} on Razorpay. No change made.`,
      );
      await onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-8">
      <SchedulerPanel status={scheduler} />
      <ConnectivityPanel />

      <Card>
        <SectionTitle hint="Orders sitting in a non-terminal state past the expected window for their payment method.">
          Stuck payments
        </SectionTitle>

        {actionError && (
          <div className="mb-4">
            <ErrorNote message={actionError} />
          </div>
        )}
        {note && <p className="mb-5 text-sm text-success-text">{note}</p>}

        {error ? (
          <ErrorNote message={error} />
        ) : loading ? (
          <Spinner label="Loading orders…" />
        ) : orders.length === 0 ? (
          <EmptyState
            title="No stuck payments"
            detail="Every order has resolved within its expected window."
            tone="success"
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Order</Th>
                <Th>Method</Th>
                <Th align="right">Amount</Th>
                <Th>Local status</Th>
                <Th align="right">Re-checks</Th>
                <Th>Next check</Th>
                <Th>Action</Th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order) => (
                <Tr key={order.order_id}>
                  <Td>
                    <span className="id">{order.order_id}</span>
                  </Td>
                  <Td className="text-xs uppercase tracking-wide text-secondary">
                    {order.method}
                  </Td>
                  <Td align="right" className="tabular">
                    {inr(order.amount_paise)}
                  </Td>
                  <Td>
                    {/* Exceeded its window already, so red rather than amber:
                        anything on this list is past due by definition. */}
                    <StatusPill tone="danger">{order.local_status}</StatusPill>
                  </Td>
                  <Td align="right" className="tabular text-secondary">
                    {order.recheck_attempts}
                  </Td>
                  <Td className="text-xs text-secondary">
                    {countdown(order.next_recheck_at)}
                  </Td>
                  <Td>
                    <button
                      className="btn-ghost !px-3 !py-1 !text-xs"
                      onClick={() => handleRecheck(order.order_id)}
                      disabled={busy === order.order_id}
                    >
                      {busy === order.order_id ? <Spinner /> : "Re-check now"}
                    </button>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
