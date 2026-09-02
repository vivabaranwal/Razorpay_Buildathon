/** Shared primitives. Every colour comes from a token; none is hard-coded. */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { REASON_COLORS, REASON_LABELS, type ReasonCode } from "../api";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={`card ${className}`}>{children}</div>;
}

export function SectionTitle({
  children,
  hint,
}: {
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="mb-6">
      <h2 className="heading">{children}</h2>
      {hint && (
        <p className="mt-2 text-sm leading-relaxed text-secondary">{hint}</p>
      )}
    </div>
  );
}

/**
 * Counts a number up when it changes.
 *
 * Short and eased-out rather than linear, so it settles rather than ticking.
 * Anyone who has asked for reduced motion gets the final value immediately.
 */
function useCountUp(target: number, durationMs = 900): number {
  const [value, setValue] = useState(target);
  const previous = useRef(target);

  useEffect(() => {
    const from = previous.current;
    previous.current = target;

    if (
      from === target ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setValue(target);
      return;
    }

    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min((now - start) / durationMs, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      setValue(from + (target - from) * eased);
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, durationMs]);

  return value;
}

/**
 * A headline metric.
 *
 * `hero` opts into the display type and the ambient glow. Reserved for the two
 * numbers on Overview that anchor the page - applied to every tile the glow
 * stops reading as emphasis.
 */
export function Stat({
  label,
  value,
  sub,
  hero = false,
  tone = "default",
  countTo,
  format,
}: {
  label: string;
  value: string;
  sub?: string;
  hero?: boolean;
  tone?: "default" | "success" | "warning" | "danger";
  countTo?: number;
  format?: (n: number) => string;
}) {
  const animated = useCountUp(countTo ?? 0);
  const display =
    countTo !== undefined && format ? format(animated) : value;

  const toneClass = {
    default: "text-primary",
    success: "text-success-text",
    warning: "text-warning-text",
    danger: "text-danger-text",
  }[tone];

  return (
    <Card>
      <div className="label">{label}</div>
      <div className={hero ? "glow mt-4" : "mt-3"}>
        <div
          className={
            hero
              ? `hero-number ${toneClass}`
              : `font-display tabular text-3xl font-semibold ${toneClass}`
          }
          style={hero ? undefined : { fontVariationSettings: '"opsz" 60' }}
        >
          {display}
        </div>
      </div>
      {sub && (
        <div className="mt-3 text-xs leading-relaxed text-secondary">{sub}</div>
      )}
    </Card>
  );
}

/** A reason code with its fixed colour. Same hue everywhere it appears. */
export function ReasonBadge({ code }: { code: ReasonCode }) {
  return (
    <span className="inline-flex items-center gap-2.5 whitespace-nowrap">
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ background: REASON_COLORS[code] ?? "var(--text-muted)" }}
      />
      <span className="text-sm">{REASON_LABELS[code] ?? code}</span>
    </span>
  );
}

export function StatusPill({
  tone,
  children,
  pulse = false,
}: {
  tone: "success" | "warning" | "danger" | "muted" | "accent";
  children: ReactNode;
  pulse?: boolean;
}) {
  const styles = {
    success: { color: "var(--success-text)", borderColor: "var(--success)" },
    warning: { color: "var(--warning-text)", borderColor: "var(--warning)" },
    danger: { color: "var(--danger-text)", borderColor: "var(--danger)" },
    accent: { color: "var(--accent-text)", borderColor: "var(--accent)" },
    muted: { color: "var(--text-secondary)", borderColor: "var(--border)" },
  }[tone];

  return (
    <span className="pill" style={styles}>
      {pulse && (
        <span
          className="animate-pulse-dot h-1.5 w-1.5 rounded-full"
          style={{ background: "currentColor" }}
        />
      )}
      {children}
    </span>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="max-h-[34rem] overflow-auto rounded-card border border-border">
      <table className="data-table">{children}</table>
    </div>
  );
}

export function Th({
  children,
  align = "left",
}: {
  children: ReactNode;
  align?: "left" | "right";
}) {
  return <th style={{ textAlign: align }}>{children}</th>;
}

export function Td({
  children,
  align = "left",
  className = "",
  style,
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <td className={className} style={{ textAlign: align, ...style }}>
      {children}
    </td>
  );
}

export function Tr({
  children,
  onClick,
  selected = false,
}: {
  children: ReactNode;
  onClick?: () => void;
  selected?: boolean;
}) {
  return (
    <tr
      onClick={onClick}
      className={[
        onClick ? "is-clickable" : "",
        selected ? "is-selected" : "",
      ].join(" ")}
    >
      {children}
    </tr>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2.5 text-sm text-secondary">
      <span
        className="h-3.5 w-3.5 animate-spin rounded-full border-2"
        style={{
          borderColor: "var(--border)",
          borderTopColor: "var(--accent)",
        }}
      />
      {label}
    </span>
  );
}

export function EmptyState({
  title,
  detail,
  tone = "muted",
}: {
  title: string;
  detail?: string;
  tone?: "muted" | "success";
}) {
  return (
    <div className="rounded-card border border-dashed border-border px-8 py-14 text-center">
      <p
        className="font-display text-lg"
        style={{
          color: tone === "success" ? "var(--success-text)" : "var(--text-primary)",
        }}
      >
        {title}
      </p>
      {detail && (
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-secondary">
          {detail}
        </p>
      )}
    </div>
  );
}

/** A failure the operator needs to see, rendered inline rather than thrown. */
export function ErrorNote({ message }: { message: string }) {
  return (
    <div
      className="rounded-control border px-5 py-4"
      style={{
        borderColor: "var(--danger)",
        background: "color-mix(in srgb, var(--danger) 8%, transparent)",
      }}
    >
      <p className="text-sm text-danger-text">{message}</p>
    </div>
  );
}
