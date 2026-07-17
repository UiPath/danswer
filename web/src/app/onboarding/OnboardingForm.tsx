"use client";

import { useEffect, useState } from "react";
import {
  OnboardingRequestSnapshot,
  OnboardingSource,
  OnboardingSourceType,
  OnboardingStatusResponse,
  validateOnboardingField,
  ValidateKind,
  ValidationResult,
} from "@/lib/onboarding/interfaces";

// --- inline-validated text field --------------------------------------------

function ValidatedField({
  label,
  placeholder,
  kind,
  value,
  onChange,
  onResolved,
  optional,
}: {
  label: string;
  placeholder?: string;
  kind: ValidateKind;
  value: string;
  onChange: (v: string) => void;
  onResolved?: (r: ValidationResult) => void;
  optional?: boolean;
}) {
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [checking, setChecking] = useState(false);

  async function check() {
    if (!value.trim()) {
      setResult(null);
      return;
    }
    setChecking(true);
    const r = await validateOnboardingField(kind, value);
    setResult(r);
    setChecking(false);
    onResolved?.(r);
  }

  return (
    <div className="mb-3">
      <label className="block text-sm font-medium text-default mb-1">
        {label}
        {optional && (
          <span className="text-subtle font-normal"> (optional)</span>
        )}
      </label>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value);
          setResult(null);
        }}
        onBlur={check}
        className="w-full rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm text-default focus:outline-none focus:ring-1 focus:ring-accent"
      />
      {checking && <p className="mt-1 text-xs text-subtle">Validating…</p>}
      {result && (
        <p
          className={`mt-1 text-xs ${
            result.valid ? "text-link" : "text-error"
          }`}
        >
          {result.valid ? "✓ " : "✗ "}
          {result.message}
        </p>
      )}
    </div>
  );
}

// --- status badge -----------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "complete"
      ? "text-link"
      : status === "failed" || status === "rejected"
        ? "text-error"
        : "text-subtle";
  return <span className={`text-xs font-semibold ${color}`}>{status}</span>;
}

// --- one extra source row ---------------------------------------------------

function SourceRow({
  source,
  onChange,
  onRemove,
  onMove,
}: {
  source: OnboardingSource;
  onChange: (s: OnboardingSource) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const kindFor: Record<OnboardingSourceType, ValidateKind> = {
    web: "docs",
    confluence: "confluence",
    github: "docs", // github URLs aren't live-validated; format-check only
    slack: "slack_channel",
  };
  const [result, setResult] = useState<ValidationResult | null>(null);
  return (
    <div className="mb-2 flex items-start gap-2">
      <select
        value={source.type}
        onChange={(e) =>
          onChange({ ...source, type: e.target.value as OnboardingSourceType })
        }
        className="rounded-md border border-border-medium bg-background-weak px-2 py-2 text-sm"
      >
        <option value="confluence">Confluence</option>
        <option value="github">GitHub repo</option>
        <option value="slack">Slack channel</option>
        <option value="web">Web / docs</option>
      </select>
      <div className="flex-1">
        <input
          type="text"
          value={source.value}
          placeholder="URL, repo, or #channel"
          onChange={(e) => {
            onChange({ ...source, value: e.target.value });
            setResult(null);
          }}
          onBlur={async () => {
            if (!source.value.trim() || source.type === "github") return;
            setResult(
              await validateOnboardingField(kindFor[source.type], source.value)
            );
          }}
          className="w-full rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm"
        />
        {result && (
          <p
            className={`mt-1 text-xs ${result.valid ? "text-link" : "text-error"}`}
          >
            {result.valid ? "✓ " : "✗ "}
            {result.message}
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={() => onMove(-1)}
        title="Move up"
        className="px-1 text-subtle hover:text-default"
      >
        ↑
      </button>
      <button
        type="button"
        onClick={() => onMove(1)}
        title="Move down"
        className="px-1 text-subtle hover:text-default"
      >
        ↓
      </button>
      <button
        type="button"
        onClick={onRemove}
        title="Remove"
        className="px-1 text-subtle hover:text-error"
      >
        ✕
      </button>
    </div>
  );
}

// --- main form --------------------------------------------------------------

export function OnboardingForm() {
  const [teamName, setTeamName] = useState("");
  const [channelInput, setChannelInput] = useState("");
  const [channel, setChannel] = useState<{ id: string; name: string } | null>(
    null
  );
  const [responseType, setResponseType] = useState<"citations" | "quotes">(
    "citations"
  );
  const [respondTagOnly, setRespondTagOnly] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [smeEnabled, setSmeEnabled] = useState(false);
  const [smeGroup, setSmeGroup] = useState("");
  const [oncallEnabled, setOncallEnabled] = useState(false);
  const [oncallSchedule, setOncallSchedule] = useState("");
  const [jiraEnabled, setJiraEnabled] = useState(false);
  const [jiraProject, setJiraProject] = useState("");
  const [jiraIssueType, setJiraIssueType] = useState("");

  const [docsCloud, setDocsCloud] = useState("");
  const [docsOnprem, setDocsOnprem] = useState("");
  const [includeHistory, setIncludeHistory] = useState(true);
  const [extraSources, setExtraSources] = useState<OnboardingSource[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mine, setMine] = useState<OnboardingRequestSnapshot[]>([]);
  const [statuses, setStatuses] = useState<
    Record<number, OnboardingStatusResponse>
  >({});

  useEffect(() => {
    fetch("/api/onboarding/default-prompt")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d?.system_prompt && setSystemPrompt(d.system_prompt))
      .catch(() => {});
    void refreshMine();
  }, []);

  async function refreshMine() {
    try {
      const r = await fetch("/api/onboarding/mine");
      if (r.ok) setMine((await r.json()) as OnboardingRequestSnapshot[]);
    } catch {
      /* ignore */
    }
  }

  async function loadStatus(id: number) {
    const r = await fetch(`/api/onboarding/${id}/status`);
    if (r.ok) {
      const data = (await r.json()) as OnboardingStatusResponse;
      setStatuses((prev) => ({ ...prev, [id]: data }));
    }
  }

  function buildSources(): OnboardingSource[] {
    const s: OnboardingSource[] = [];
    if (docsCloud.trim())
      s.push({ type: "web", value: docsCloud.trim(), label: "Docs (cloud)" });
    if (docsOnprem.trim())
      s.push({
        type: "web",
        value: docsOnprem.trim(),
        label: "Docs (on-prem)",
      });
    if (includeHistory && channel)
      s.push({
        type: "slack",
        value: channel.name,
        label: `#${channel.name} history`,
      });
    extraSources.forEach((x) => x.value.trim() && s.push(x));
    return s;
  }

  async function submit() {
    setError(null);
    if (!teamName.trim()) return setError("Team name is required.");
    if (!channel) return setError("Validate the bot channel first.");
    const sources = buildSources();
    if (sources.length === 0) return setError("Add at least one source.");

    setSubmitting(true);
    try {
      const res = await fetch("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          team_name: teamName.trim(),
          channel: { channel_id: channel.id, channel_name: channel.name },
          response_type: responseType,
          respond_tag_only: respondTagOnly,
          system_prompt: systemPrompt,
          task_prompt: "",
          sme: { enabled: smeEnabled, group_name: smeGroup },
          oncall: { enabled: oncallEnabled, schedule: oncallSchedule },
          jira: {
            enabled: jiraEnabled,
            project_key: jiraProject,
            issue_type: jiraIssueType,
            component: "",
          },
          sources,
        }),
      });
      if (!res.ok) {
        setError(
          (await res.json().catch(() => null))?.detail ||
            `Submit failed (${res.status}).`
        );
        return;
      }
      // reset the source fields; keep it simple.
      setDocsCloud("");
      setDocsOnprem("");
      setExtraSources([]);
      await refreshMine();
    } catch {
      setError("Something went wrong submitting the request.");
    } finally {
      setSubmitting(false);
    }
  }

  const section =
    "mb-6 rounded-lg border border-border-medium bg-background-weak p-4";

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="text-2xl font-semibold text-default mb-1">
        Onboard a team to Darwin
      </h1>
      <p className="text-sm text-subtle mb-6">
        Submit this request; an admin approves it, then Darwin auto-scrapes your
        sources and wires up your assistant. Every field is validated live.
      </p>

      <div className={section}>
        <ValidatedField
          label="Bot channel"
          placeholder="#help-your-team or a channel link"
          kind="slack_channel"
          value={channelInput}
          onChange={setChannelInput}
          onResolved={(r) =>
            setChannel(
              r.valid
                ? {
                    id: String(r.resolved.channel_id ?? ""),
                    name: String(r.resolved.channel_name ?? ""),
                  }
                : null
            )
          }
        />
        <label className="block text-sm font-medium text-default mb-1 mt-2">
          Team name
        </label>
        <input
          value={teamName}
          onChange={(e) => setTeamName(e.target.value)}
          placeholder="e.g. Integration Service"
          className="w-full rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm mb-3"
        />
        <div className="flex items-center gap-4">
          <label className="text-sm text-default">
            Response format:{" "}
            <select
              value={responseType}
              onChange={(e) =>
                setResponseType(e.target.value as "citations" | "quotes")
              }
              className="rounded-md border border-border-medium bg-background-weak px-2 py-1 text-sm"
            >
              <option value="citations">Citations</option>
              <option value="quotes">Quotes</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-default">
            <input
              type="checkbox"
              checked={respondTagOnly}
              onChange={(e) => setRespondTagOnly(e.target.checked)}
            />
            Respond only when tagged
          </label>
        </div>
      </div>

      <div className={section}>
        <h2 className="text-sm font-semibold text-default mb-2">
          Sources (priority order)
        </h2>
        <ValidatedField
          label="Documentation — Cloud (root URL)"
          placeholder="https://docs.uipath.com/<product>/automation-cloud/latest"
          kind="docs"
          value={docsCloud}
          onChange={setDocsCloud}
          optional
        />
        <ValidatedField
          label="Documentation — On-prem / standalone (root URL)"
          placeholder="https://docs.uipath.com/<product>/standalone/latest"
          kind="docs"
          value={docsOnprem}
          onChange={setDocsOnprem}
          optional
        />
        <p className="text-xs text-subtle mb-3">
          Paste the product root URL only — Darwin crawls all versions
          automatically.
        </p>
        <label className="flex items-center gap-2 text-sm text-default mb-3">
          <input
            type="checkbox"
            checked={includeHistory}
            onChange={(e) => setIncludeHistory(e.target.checked)}
          />
          Include this channel&apos;s message history
        </label>
        <div className="text-sm font-medium text-default mb-1">
          Additional sources
        </div>
        {extraSources.map((s, i) => (
          <SourceRow
            key={i}
            source={s}
            onChange={(ns) =>
              setExtraSources((p) => p.map((x, j) => (j === i ? ns : x)))
            }
            onRemove={() => setExtraSources((p) => p.filter((_, j) => j !== i))}
            onMove={(dir) =>
              setExtraSources((p) => {
                const j = i + dir;
                if (j < 0 || j >= p.length) return p;
                const c = [...p];
                [c[i], c[j]] = [c[j], c[i]];
                return c;
              })
            }
          />
        ))}
        <button
          type="button"
          onClick={() =>
            setExtraSources((p) => [...p, { type: "confluence", value: "" }])
          }
          className="mt-1 text-sm text-link hover:underline"
        >
          + Add source
        </button>
      </div>

      <div className={section}>
        <label className="block text-sm font-medium text-default mb-1">
          System prompt
        </label>
        <p className="text-xs text-subtle mb-1">
          Prefilled from the default (Orchestrator) — edit as needed.
        </p>
        <textarea
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          rows={5}
          className="w-full rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm font-mono"
        />
      </div>

      <div className={section}>
        <h2 className="text-sm font-semibold text-default mb-2">Options</h2>
        <label className="flex items-center gap-2 text-sm text-default mb-2">
          <input
            type="checkbox"
            checked={smeEnabled}
            onChange={(e) => setSmeEnabled(e.target.checked)}
          />
          Let SMEs verify answers
        </label>
        {smeEnabled && (
          <ValidatedField
            label="SME Slack user group(s)"
            placeholder="e.g. as-smes (comma-separated)"
            kind="slack_group"
            value={smeGroup}
            onChange={setSmeGroup}
          />
        )}
        <label className="flex items-center gap-2 text-sm text-default mb-2">
          <input
            type="checkbox"
            checked={oncallEnabled}
            onChange={(e) => setOncallEnabled(e.target.checked)}
          />
          On &quot;need more help&quot;, tag the on-call
        </label>
        {oncallEnabled && (
          <input
            value={oncallSchedule}
            onChange={(e) => setOncallSchedule(e.target.value)}
            placeholder="Opsgenie schedule name"
            className="w-full rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm mb-3"
          />
        )}
        <label className="flex items-center gap-2 text-sm text-default mb-2">
          <input
            type="checkbox"
            checked={jiraEnabled}
            onChange={(e) => setJiraEnabled(e.target.checked)}
          />
          Enable Jira ticket creation
        </label>
        {jiraEnabled && (
          <div className="flex gap-2">
            <input
              value={jiraProject}
              onChange={(e) => setJiraProject(e.target.value)}
              placeholder="Project key"
              className="flex-1 rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm"
            />
            <input
              value={jiraIssueType}
              onChange={(e) => setJiraIssueType(e.target.value)}
              placeholder="Issue type"
              className="flex-1 rounded-md border border-border-medium bg-background-weak px-3 py-2 text-sm"
            />
          </div>
        )}
      </div>

      {error && <p className="mb-3 text-sm text-error">{error}</p>}
      <button
        onClick={submit}
        disabled={submitting}
        className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-inverted disabled:opacity-60"
      >
        {submitting ? "Submitting…" : "Submit onboarding request"}
      </button>

      {mine.length > 0 && (
        <div className="mt-10">
          <h2 className="text-lg font-semibold text-default mb-3">
            Your requests
          </h2>
          {mine.map((r) => (
            <div
              key={r.id}
              className="mb-3 rounded-lg border border-border-medium p-3"
            >
              <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-default">
                  {r.payload.team_name} → #{r.payload.channel.channel_name}
                </div>
                <StatusBadge status={r.status} />
              </div>
              {r.error_msg && (
                <p className="mt-1 text-xs text-error">{r.error_msg}</p>
              )}
              {r.decision_reason && (
                <p className="mt-1 text-xs text-subtle">
                  Note: {r.decision_reason}
                </p>
              )}
              {(r.cc_pair_ids?.length ?? 0) > 0 && (
                <div className="mt-2">
                  <button
                    onClick={() => loadStatus(r.id)}
                    className="text-xs text-link hover:underline"
                  >
                    Refresh scrape status
                  </button>
                  {statuses[r.id]?.sources.map((s) => (
                    <div
                      key={s.cc_pair_id}
                      className="mt-1 flex items-center justify-between text-xs"
                    >
                      <span className="text-subtle">{s.name}</span>
                      <span>
                        <StatusBadge status={s.status} /> · {s.docs_indexed}{" "}
                        docs
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
      )}
    </div>
  );
}
