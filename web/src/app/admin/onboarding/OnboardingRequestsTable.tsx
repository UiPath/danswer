"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  OnboardingRequestSnapshot,
  OnboardingStatusResponse,
} from "@/lib/onboarding/interfaces";
import { SourceStatusSummary } from "@/app/onboarding/SourceStatusSummary";
import { OnboardingJourney } from "@/app/onboarding/OnboardingJourney";

type FilterKey =
  "active" | "pending" | "in_progress" | "failed" | "complete" | "all";

const FILTERS: {
  key: FilterKey;
  label: string;
  match: (s: string) => boolean;
}[] = [
  {
    key: "active",
    label: "Active",
    // Anything not finished — needs attention.
    match: (s) => !["complete", "rejected", "cancelled"].includes(s),
  },
  { key: "pending", label: "Pending", match: (s) => s === "pending" },
  {
    key: "in_progress",
    label: "In progress",
    match: (s) => ["provisioning", "indexing"].includes(s),
  },
  { key: "failed", label: "Failed", match: (s) => s === "failed" },
  { key: "complete", label: "Complete", match: (s) => s === "complete" },
  { key: "all", label: "All", match: () => true },
];

const PAGE_SIZE = 10;

function Badge({ status }: { status: string }) {
  const color =
    status === "complete"
      ? "text-link"
      : status === "failed" || status === "rejected"
        ? "text-error"
        : status === "pending"
          ? "text-default"
          : "text-subtle";
  return (
    <span className={`text-xs font-semibold uppercase ${color}`}>{status}</span>
  );
}

export function OnboardingRequestsTable() {
  const [requests, setRequests] = useState<OnboardingRequestSnapshot[]>([]);
  const [statuses, setStatuses] = useState<
    Record<number, OnboardingStatusResponse>
  >({});
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Default view hides finished requests (complete/rejected/cancelled) so the
  // queue stays focused on what needs attention; switchable + paginated so the
  // page stays usable well past 10+ requests.
  const [filter, setFilter] = useState<FilterKey>("active");
  const [page, setPage] = useState(0);

  async function refresh() {
    const r = await fetch("/api/admin/onboarding");
    if (!r.ok) return;
    const data = (await r.json()) as OnboardingRequestSnapshot[];
    setRequests(data);
    // Auto-load per-source scrape status for provisioned requests so the journey
    // + rollup populate without a manual "Refresh scrape status" click.
    await Promise.all(
      data
        .filter((req) => (req.cc_pair_ids?.length ?? 0) > 0)
        .map((req) => loadStatus(req.id))
    );
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function decide(id: number, action: "approve" | "reject") {
    setError(null);
    let reason: string | null = null;
    if (action === "reject") {
      reason = window.prompt("Reason for rejecting (optional)?") || null;
    }
    setBusy(id);
    try {
      const r = await fetch(`/api/admin/onboarding/${id}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "reject" ? { reason } : {}),
      });
      if (!r.ok) {
        setError(
          (await r.json().catch(() => null))?.detail || `Failed (${r.status}).`
        );
        return;
      }
      await refresh();
    } finally {
      setBusy(null);
    }
  }

  async function loadStatus(id: number) {
    const r = await fetch(`/api/onboarding/${id}/status`);
    if (r.ok) {
      const data = (await r.json()) as OnboardingStatusResponse;
      setStatuses((p) => ({ ...p, [id]: data }));
    }
  }

  if (requests.length === 0) {
    return <p className="text-sm text-subtle">No onboarding requests yet.</p>;
  }

  const activeFilter = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
  const filtered = requests.filter((r) => activeFilter.match(r.status));
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages - 1);
  const pageItems = filtered.slice(
    clampedPage * PAGE_SIZE,
    clampedPage * PAGE_SIZE + PAGE_SIZE
  );

  return (
    <div>
      {error && <p className="mb-3 text-sm text-error">{error}</p>}

      {/* Filter bar */}
      <div className="mb-4 flex flex-wrap items-center gap-1">
        {FILTERS.map((f) => {
          const count = requests.filter((r) => f.match(r.status)).length;
          const selected = f.key === filter;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => {
                setFilter(f.key);
                setPage(0);
              }}
              className={
                `rounded-full border px-3 py-1 text-xs font-medium transition-colors ` +
                (selected
                  ? " border-accent bg-accent/10 text-accent"
                  : " border-border-medium text-subtle hover:bg-hover-light")
              }
            >
              {f.label} ({count})
            </button>
          );
        })}
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-subtle">
          No {activeFilter.label.toLowerCase()} onboarding requests.
        </p>
      ) : (
        pageItems.map((req) => (
          <div
            key={req.id}
            className="mb-4 rounded-lg border border-border-medium p-4"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-default">
                  {req.payload.team_name} → #{req.payload.channel.channel_name}
                </div>
                <div className="text-xs text-subtle">
                  by {req.requester_email} ·{" "}
                  {new Date(req.created_at).toLocaleString()}
                </div>
              </div>
              <div className="flex flex-col items-end gap-1">
                <Badge status={req.status} />
                {statuses[req.id] && (
                  <SourceStatusSummary sources={statuses[req.id].sources} />
                )}
              </div>
            </div>

            <OnboardingJourney
              request={req}
              sources={statuses[req.id]?.sources}
              admin
            />

            <div className="mt-2 text-xs text-subtle">
              <span className="font-medium text-default">Sources:</span>{" "}
              {req.payload.sources
                .map((s) => `${s.type}:${s.label || s.value}`)
                .join(", ")}
            </div>
            <div className="mt-1 text-xs text-subtle">
              SME:{" "}
              {req.payload.sme.enabled
                ? req.payload.sme.group_name || "yes"
                : "no"}{" "}
              · On-call:{" "}
              {req.payload.oncall.enabled
                ? req.payload.oncall.schedule || "yes"
                : "no"}{" "}
              · Jira:{" "}
              {req.payload.jira.enabled
                ? req.payload.jira.project_key || "yes"
                : "no"}
            </div>
            {req.error_msg && (
              <p className="mt-1 text-xs text-error">{req.error_msg}</p>
            )}

            {req.status === "pending" && (
              <div className="mt-3 flex items-center gap-2">
                <Link
                  href={`/admin/onboarding/${req.id}`}
                  className="rounded-md border border-border-medium px-3 py-1.5 text-xs font-medium text-default hover:bg-hover"
                >
                  Open / edit
                </Link>
                <button
                  disabled={busy === req.id}
                  onClick={() => decide(req.id, "approve")}
                  className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-inverted disabled:opacity-60"
                >
                  {busy === req.id ? "Provisioning…" : "Approve & provision"}
                </button>
                <button
                  disabled={busy === req.id}
                  onClick={() => decide(req.id, "reject")}
                  className="rounded-md border border-border-medium px-3 py-1.5 text-xs font-medium text-default hover:bg-hover"
                >
                  Reject
                </button>
              </div>
            )}

            {(req.cc_pair_ids?.length ?? 0) > 0 && (
              <div className="mt-3">
                <button
                  onClick={() => loadStatus(req.id)}
                  className="text-xs text-link hover:underline"
                >
                  Refresh scrape status
                </button>
                {statuses[req.id]?.sources.map((s) => (
                  <div key={s.cc_pair_id} className="mt-1 text-xs">
                    <div className="flex items-start gap-2">
                      <Link
                        href={`/admin/connector/${s.cc_pair_id}`}
                        className="min-w-0 flex-1 text-link hover:underline [overflow-wrap:anywhere]"
                        title="Open this connector to see full indexing status"
                      >
                        {s.name}
                      </Link>
                      <span className="w-24 shrink-0">
                        <Badge status={s.status} />
                      </span>
                      <span className="w-16 shrink-0 text-right text-subtle">
                        {s.docs_indexed} docs
                      </span>
                    </div>
                    {s.error_msg && (
                      <p className="mt-0.5 [overflow-wrap:anywhere] text-error">
                        {s.error_msg}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))
      )}

      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between text-xs text-subtle">
          <span>
            Showing {clampedPage * PAGE_SIZE + 1}–
            {Math.min((clampedPage + 1) * PAGE_SIZE, filtered.length)} of{" "}
            {filtered.length}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={clampedPage === 0}
              onClick={() => setPage(clampedPage - 1)}
              className="rounded-md border border-border-medium px-2 py-1 hover:bg-hover-light disabled:opacity-40"
            >
              Prev
            </button>
            <span>
              Page {clampedPage + 1} / {totalPages}
            </span>
            <button
              type="button"
              disabled={clampedPage >= totalPages - 1}
              onClick={() => setPage(clampedPage + 1)}
              className="rounded-md border border-border-medium px-2 py-1 hover:bg-hover-light disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
