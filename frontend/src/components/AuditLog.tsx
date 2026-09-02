/**
 * Tab 4 - the append-only audit trail.
 *
 * Deliberately plain: this is the screen a merchant reads to reconstruct what
 * happened to a payout, so it is laid out like a bank statement rather than a
 * dashboard. Nothing here is editable, and there is no API that could make it
 * so - a trail that can be amended is not evidence of anything.
 */

import { useState } from "react";
import { relativeTime, type AuditRow } from "../api";
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

const ENTITY_LABELS: Record<string, string> = {
  order: "Order",
  exception: "Exception",
  settlement_match: "Settlement match",
};

function formatTimestamp(iso: string): string {
  // Same two shapes as elsewhere: naive from SQLite, offset-bearing from the
  // scheduler. Appending "Z" to a string that already carries an offset makes
  // it unparseable, so the zone is added only when one is missing.
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function AuditLog({
  rows,
  loading,
  error,
}: {
  rows: AuditRow[];
  loading: boolean;
  error: string | null;
}) {
  const [entityFilter, setEntityFilter] = useState("");

  const visible = entityFilter
    ? rows.filter((r) => r.entity_type === entityFilter)
    : rows;

  const entities = [...new Set(rows.map((r) => r.entity_type))];
  const automatic = rows.filter((r) => r.is_automatic).length;

  return (
    <Card>
      <SectionTitle hint="Every state change the system has made, newest first. Read-only and append-only.">
        Audit trail
      </SectionTitle>

      <div className="mb-6 flex flex-wrap items-end justify-between gap-6">
        <div>
          <label className="label" htmlFor="entity">
            Entity type
          </label>
          <select
            id="entity"
            className="input min-w-[12rem]"
            value={entityFilter}
            onChange={(e) => setEntityFilter(e.target.value)}
          >
            <option value="">All entities</option>
            {entities.map((entity) => (
              <option key={entity} value={entity}>
                {ENTITY_LABELS[entity] ?? entity}
              </option>
            ))}
          </select>
        </div>
        <div className="flex gap-10 text-right">
          <div>
            <div className="label">Total changes</div>
            <div className="font-display tabular mt-1.5 text-2xl font-semibold text-primary">
              {rows.length}
            </div>
          </div>
          <div>
            <div className="label">Automatic</div>
            <div className="font-display tabular mt-1.5 text-2xl font-semibold text-primary">
              {automatic}
            </div>
          </div>
        </div>
      </div>

      {error ? (
        <ErrorNote message={error} />
      ) : loading ? (
        <Spinner label="Loading audit trail…" />
      ) : visible.length === 0 ? (
        <EmptyState
          title="No changes recorded yet"
          detail="Corrections and resolutions will appear here as they happen."
        />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>When</Th>
              <Th>Entity</Th>
              <Th>Field</Th>
              <Th>Change</Th>
              <Th>By</Th>
              <Th>Reason</Th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <Tr key={row.id}>
                <Td className="whitespace-nowrap">
                  {/* Timestamps in mono: this screen's job is to read like a
                      ledger, and a fixed-width column of times scans far
                      faster than proportional digits. */}
                  <div className="font-mono text-xs text-primary">
                    {formatTimestamp(row.changed_at)}
                  </div>
                  <div className="mt-1 text-xs text-muted">
                    {relativeTime(row.changed_at)}
                  </div>
                </Td>
                <Td>
                  <div className="text-xs text-secondary">
                    {ENTITY_LABELS[row.entity_type] ?? row.entity_type}
                  </div>
                  <div className="id">{row.entity_id}</div>
                </Td>
                <Td className="font-mono text-xs text-primary">{row.field_changed}</Td>
                <Td>
                  <span className="font-mono text-xs text-muted line-through">
                    {row.old_value ?? "—"}
                  </span>
                  <span className="mx-2 text-muted">→</span>
                  <span className="font-mono text-xs font-medium text-success-text">
                    {row.new_value ?? "—"}
                  </span>
                </Td>
                <Td>
                  {/* An automatic correction and a named human action carry
                      very different weight when reconstructing a payout. */}
                  <StatusPill tone={row.is_automatic ? "muted" : "accent"}>
                    {row.is_automatic ? "system" : row.changed_by}
                  </StatusPill>
                </Td>
                <Td className="max-w-md text-xs leading-relaxed text-secondary">
                  {row.reason ?? "—"}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
