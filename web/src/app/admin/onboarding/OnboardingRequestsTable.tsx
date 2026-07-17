"use client";

import { useEffect, useState } from "react";
import {
  OnboardingRequestSnapshot,
  OnboardingStatusResponse,
} from "@/lib/onboarding/interfaces";

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

  async function refresh() {
    const r = await fetch("/api/admin/onboarding");
    if (r.ok) setRequests((await r.json()) as OnboardingRequestSnapshot[]);
  }

  useEffect(() => {
    void refresh();
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

  return (
    <div>
      {error && <p className="mb-3 text-sm text-error">{error}</p>}
      {requests.map((req) => (
        <div
          key={req.id}
          className="mb-4 rounded-lg border border-border-medium p-4"
        >
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold text-default">
                {req.payload.team_name} → #{req.payload.channel.channel_name}
              </div>
              <div className="text-xs text-subtle">
                by {req.requester_email} ·{" "}
                {new Date(req.created_at).toLocaleString()}
              </div>
            </div>
            <Badge status={req.status} />
          </div>

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
            <div className="mt-3 flex gap-2">
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
                <div
                  key={s.cc_pair_id}
                  className="mt-1 flex items-center justify-between text-xs"
                >
                  <span className="text-subtle">{s.name}</span>
                  <span>
                    <Badge status={s.status} /> · {s.docs_indexed} docs
                    {s.error_msg ? (
                      <span className="text-error"> · {s.error_msg}</span>
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
