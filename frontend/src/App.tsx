import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AuditRow,
  type BatchSummary,
  type ExceptionRow,
  type Health,
  type SchedulerStatus,
  type StuckOrder,
} from "./api";
import { AuditLog } from "./components/AuditLog";
import { Exceptions } from "./components/Exceptions";
import { Overview } from "./components/Overview";
import { PaymentState } from "./components/PaymentState";
import { ErrorNote, StatusPill } from "./components/ui";
import { TheItch } from "./components/TheItch";
import { usePath } from "./router";
import { useTheme, type Theme } from "./theme";
import { Moon, Sun } from "lucide-react";

type Tab = "overview" | "exceptions" | "payments" | "audit";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "exceptions", label: "Exceptions" },
  { id: "payments", label: "Payment state" },
  { id: "audit", label: "Audit log" },
];

// The scheduler ticks every 15s server-side; polling a little faster keeps the
// countdown honest without hammering the API.
const POLL_MS = 5000;

// Remembers that the reader has already found the narrative page, so the nav
// cue points once rather than pulsing at them forever.
const ITCH_SEEN_KEY = "settletrace-itch-seen";

function hasSeenItch(): boolean {
  try {
    return localStorage.getItem(ITCH_SEEN_KEY) === "1";
  } catch {
    // Private browsing and blocked site data both throw. Showing the cue is
    // the harmless default.
    return false;
  }
}

/**
 * Provenance shown as persistent pills rather than a footnote.
 *
 * A reader must never have to guess whether a number came from Razorpay or from
 * generated data, or whether an explanation was written by a model or is canned
 * template text. Both are consequential enough to stay on screen.
 */
function Header({
  health,
  theme,
  onToggleTheme,
  onOpenItch,
  showCue,
}: {
  health: Health | null;
  theme: Theme;
  onToggleTheme: () => void;
  onOpenItch: () => void;
  showCue: boolean;
}) {
  return (
    <header
      className="sticky top-0 z-30 border-b"
      style={{
        borderColor: "var(--border)",
        background: "color-mix(in srgb, var(--bg) 88%, transparent)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
      }}
    >
      <div className="mx-auto flex max-w-[92rem] flex-wrap items-center gap-x-8 gap-y-3 px-8 py-5">
        <div className="flex items-baseline gap-4">
          <span className="wordmark">SettleTrace</span>
          <span className="hidden text-sm text-secondary sm:inline">
            Reconciliation copilot for Razorpay merchants
          </span>
        </div>

        <button
          onClick={onOpenItch}
          className="itch-link group inline-flex items-center gap-2.5 transition-opacity hover:opacity-80"
          title="Why SettleTrace exists"
        >
          {/* The cue only runs for someone who has not opened the page yet.
              Continuing to pulse at a reader who has already been there would
              be nagging rather than pointing. */}
          {showCue && (
            <span
              className="itch-dot h-[7px] w-[7px] shrink-0 rounded-full"
              style={{ background: "var(--accent)" }}
            />
          )}
          <span
            className={`font-display text-[15px] font-medium ${
              showCue ? "itch-shimmer" : ""
            }`}
            style={showCue ? undefined : { color: "var(--accent-text)" }}
          >
            The Itch
          </span>
        </button>

        <div className="ml-auto flex flex-wrap items-center gap-2.5">
          {health ? (
            <>
              <StatusPill tone={health.llm_configured ? "success" : "warning"}>
                {health.llm_configured ? "AI connected" : "AI fallback mode"}
              </StatusPill>
              <StatusPill tone={health.data_source_is_live ? "success" : "muted"}>
                {health.data_source_is_live ? "Razorpay sandbox" : "Sample data"}
              </StatusPill>
              <StatusPill
                tone={health.scheduler_running ? "success" : "danger"}
                pulse={health.scheduler_running}
              >
                {health.scheduler_running ? "Auto re-check on" : "Auto re-check off"}
              </StatusPill>
            </>
          ) : (
            <StatusPill tone="danger">API unreachable</StatusPill>
          )}

          <button
            onClick={onToggleTheme}
            className="btn-ghost !p-2.5"
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? (
              <Sun size={18} strokeWidth={1.5} />
            ) : (
              <Moon size={18} strokeWidth={1.5} />
            )}
          </button>
        </div>
      </div>
    </header>
  );
}

export default function App() {
  const { theme, toggle } = useTheme();
  const [path, navigate] = usePath();
  const [itchSeen, setItchSeen] = useState(hasSeenItch);

  const openItch = useCallback(() => {
    try {
      localStorage.setItem(ITCH_SEEN_KEY, "1");
    } catch {
      // Persisting failed; the cue simply returns on the next load.
    }
    setItchSeen(true);
    navigate("/the-itch");
  }, [navigate]);
  const [tab, setTab] = useState<Tab>("overview");

  const [health, setHealth] = useState<Health | null>(null);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [batchId, setBatchId] = useState<number | null>(null);
  const [exceptions, setExceptions] = useState<ExceptionRow[]>([]);
  const [orders, setOrders] = useState<StuckOrder[]>([]);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);

  const [loading, setLoading] = useState(true);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);

  const activeBatch = batches.find((b) => b.id === batchId) ?? batches[0] ?? null;

  /** Load everything the dashboard shows, tolerating partial failure. */
  const loadAll = useCallback(
    async (selectBatch?: number) => {
      try {
        const [healthResult, batchList] = await Promise.all([
          api.health(),
          api.batches(),
        ]);
        setHealth(healthResult);
        setBatches(batchList);
        setFatalError(null);

        const target = selectBatch ?? batchId ?? batchList[0]?.id ?? null;
        setBatchId(target);

        // These four are independent panels; one failing must not blank the
        // others, so failures are captured rather than thrown.
        const [exc, stuck, sched, trail] = await Promise.allSettled([
          target != null
            ? api.exceptions({ batchId: target, includeResolved: true })
            : Promise.resolve([]),
          api.stuckOrders(),
          api.schedulerStatus(),
          api.auditLog(),
        ]);

        if (exc.status === "fulfilled") setExceptions(exc.value);
        if (stuck.status === "fulfilled") setOrders(stuck.value);
        if (sched.status === "fulfilled") setScheduler(sched.value);
        if (trail.status === "fulfilled") setAudit(trail.value);

        const failed = [exc, stuck, sched, trail].find(
          (r) => r.status === "rejected",
        );
        setPanelError(
          failed && failed.status === "rejected"
            ? String(failed.reason?.message ?? failed.reason)
            : null,
        );
      } catch (err) {
        setFatalError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [batchId],
  );

  const onDashboard = path === "/";

  useEffect(() => {
    if (onDashboard || itchSeen) return;
    try {
      localStorage.setItem(ITCH_SEEN_KEY, "1");
    } catch {
      // Persisting failed; harmless.
    }
    setItchSeen(true);
  }, [onDashboard, itchSeen]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // Poll so the scheduler countdown and any autonomous correction appear
  // without the operator refreshing.
  useEffect(() => {
    const timer = setInterval(() => {
      void Promise.allSettled([
        api.schedulerStatus().then(setScheduler),
        api.stuckOrders().then(setOrders),
        api.auditLog().then(setAudit),
        api.health().then(setHealth),
      ]);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  async function handleRunBatch(settlementId: string) {
    const created = await api.runBatch(settlementId);
    await loadAll(created.id);
    setTab("overview");
  }

  async function handleResolve(id: number, resolvedBy: string) {
    await api.resolve(id, resolvedBy);
    await loadAll();
  }

  // Returned before the dashboard's effects run, so its polling loop does not
  // keep firing while the reader is on the narrative page.
  if (!onDashboard) {
    return <TheItch onNavigate={navigate} />;
  }

  return (
    // Column layout so the footer sits at the bottom of short pages rather
    // than floating directly under the content.
    <div className="flex min-h-screen flex-col">
      <Header
        health={health}
        theme={theme}
        onToggleTheme={toggle}
        onOpenItch={openItch}
        showCue={!itchSeen}
      />

      <main className="mx-auto w-full max-w-[92rem] flex-1 space-y-8 px-8 py-10">
        {/* Live data was asked for but could not be used. The system keeps
            running on generated data, so it has to say so unmissably rather
            than let a reader assume these numbers came from Razorpay. */}
        {health?.data_degraded && (
          <div
            className="rounded-card border px-6 py-5"
            style={{
              borderColor: "var(--warning)",
              background: "color-mix(in srgb, var(--warning) 8%, transparent)",
            }}
          >
            <p className="font-display text-base text-warning-text">
              Sandbox unavailable — showing generated data
            </p>
            {health.degraded_reason && (
              <p className="mt-1.5 text-sm leading-relaxed text-secondary">
                {health.degraded_reason}
              </p>
            )}
          </div>
        )}

        {fatalError && <ErrorNote message={fatalError} />}
        {panelError && !fatalError && (
          <ErrorNote message={`Some panels failed to load: ${panelError}`} />
        )}

        {batches.length > 1 && (
          <div className="flex items-center gap-4">
            <label className="label" htmlFor="batch-select">
              Batch
            </label>
            <select
              id="batch-select"
              className="input"
              value={activeBatch?.id ?? ""}
              onChange={(e) => void loadAll(Number(e.target.value))}
            >
              {batches.map((b) => (
                <option key={b.id} value={b.id}>
                  #{b.id} · {b.settlement_id} · {b.transactions_processed} txns
                </option>
              ))}
            </select>
          </div>
        )}

        <nav
          className="flex gap-2 border-b"
          style={{ borderColor: "var(--border)" }}
        >
          {TABS.map(({ id, label }) => {
            const active = tab === id;
            const badge =
              id === "exceptions"
                ? exceptions.filter((e) => !e.resolved_flag).length
                : id === "payments"
                  ? orders.length
                  : 0;

            return (
              <button
                key={id}
                onClick={() => setTab(id)}
                className="relative -mb-px px-5 py-3 transition-colors"
                style={{
                  // Fraunces on the tabs, per the type brief - they read as
                  // section headings rather than as controls.
                  fontFamily: "Fraunces, Georgia, serif",
                  fontSize: "16px",
                  fontWeight: 500,
                  color: active ? "var(--text-primary)" : "var(--text-secondary)",
                  borderBottom: active
                    ? "2px solid var(--accent)"
                    : "2px solid transparent",
                }}
              >
                {label}
                {badge > 0 && (
                  <span
                    className="tabular ml-2.5 rounded-full px-2 py-0.5 text-xs"
                    style={{
                      background: "color-mix(in srgb, var(--accent) 16%, transparent)",
                      color: "var(--accent-text)",
                      fontFamily: "Inter, sans-serif",
                    }}
                  >
                    {badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div key={tab} className="animate-fade-in space-y-8">
        {tab === "overview" && (
          <Overview
            batch={activeBatch}
            exceptions={exceptions}
            loading={loading}
            onRunBatch={handleRunBatch}
          />
        )}
        {tab === "exceptions" && (
          <Exceptions
            exceptions={exceptions}
            loading={loading}
            error={null}
            onResolve={handleResolve}
          />
        )}
        {tab === "payments" && (
          <PaymentState
            orders={orders}
            scheduler={scheduler}
            loading={loading}
            error={null}
            onRefresh={() => loadAll()}
          />
        )}
        {tab === "audit" && (
          <AuditLog rows={audit} loading={loading} error={null} />
        )}
        </div>
      </main>

      <footer className="border-t" style={{ borderColor: "var(--border)" }}>
        <div className="mx-auto max-w-[92rem] px-8 py-8">
          <p className="text-sm text-secondary">
            Built by{" "}
            <a
              href="https://vivabaranwal.vercel.app/"
              target="_blank"
              // noopener/noreferrer because target="_blank" otherwise hands the
              // opened page a reference back to this window.
              rel="noopener noreferrer"
              className="underline decoration-1 underline-offset-4 transition-colors"
              style={{
                color: "var(--accent-text)",
                textDecorationColor: "var(--border-strong)",
              }}
            >
              Viva Baranwal
            </a>
          </p>
        </div>
      </footer>
    </div>
  );
}
