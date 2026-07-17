"use client";

import { useEffect, useRef, useState } from "react";
import {
  FiCheck,
  FiChevronDown,
  FiChevronUp,
  FiInfo,
  FiPlus,
  FiTrash2,
  FiX,
} from "react-icons/fi";
import {
  OnboardingRequestSnapshot,
  OnboardingSource,
  OnboardingSourceType,
  OnboardingStatusResponse,
  validateOnboardingField,
  ValidateKind,
  ValidationResult,
} from "@/lib/onboarding/interfaces";
import {
  checkChannelLink,
  checkUrl,
  ClientCheck,
  SOURCE_CLIENT_CHECK,
} from "@/lib/onboarding/clientChecks";
import { useSearchParams } from "next/navigation";

// All colors come from the app's semantic theme tokens (text-default,
// bg-background, border-border, …) so the page follows the global light/dark
// setting automatically — no hardcoded colors anywhere.

const inputClass =
  "w-full rounded-md border border-border-medium bg-background px-3 py-2 " +
  "text-sm text-default placeholder:text-subtle focus:outline-none " +
  "focus:ring-2 focus:ring-accent/40 focus:border-accent";

// How long to wait after the last keystroke before hitting the backend
// validator (so you don't have to blur the field).
const VALIDATE_DEBOUNCE_MS = 600;

// --- small info tooltip -----------------------------------------------------

function InfoHint({ text }: { text: string }) {
  return (
    <span className="group relative ml-1.5 inline-flex align-middle">
      <FiInfo
        className="h-3.5 w-3.5 cursor-help text-subtle"
        aria-hidden
        tabIndex={0}
      />
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-6 z-20 w-60 -translate-x-1/2
          rounded-md border border-border bg-background px-3 py-2 text-xs font-normal
          leading-snug text-default opacity-0 shadow-lg transition-opacity duration-150
          group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

// --- validation result line -------------------------------------------------

function ResultLine({ result }: { result: ValidationResult | null }) {
  if (!result) return null;
  return (
    <p
      className={`mt-1.5 flex items-center gap-1 text-xs ${
        result.valid ? "text-link" : "text-error"
      }`}
    >
      {result.valid ? (
        <FiCheck className="h-3.5 w-3.5 shrink-0" />
      ) : (
        <FiX className="h-3.5 w-3.5 shrink-0" />
      )}
      {result.message}
    </p>
  );
}

// --- inline-validated text field --------------------------------------------

function ValidatedField({
  label,
  placeholder,
  kind,
  value,
  onChange,
  onResolved,
  optional,
  info,
  clientCheck,
  helpText,
}: {
  label: string;
  placeholder?: string;
  kind: ValidateKind;
  value: string;
  onChange: (v: string) => void;
  onResolved?: (r: ValidationResult) => void;
  optional?: boolean;
  info?: string;
  clientCheck?: ClientCheck;
  helpText?: string;
}) {
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [checking, setChecking] = useState(false);
  // Keep onResolved fresh without retriggering the debounce effect.
  const onResolvedRef = useRef(onResolved);
  onResolvedRef.current = onResolved;

  const hint = value.trim() ? (clientCheck?.(value) ?? null) : null;

  // Debounced backend validation: fires VALIDATE_DEBOUNCE_MS after the last
  // keystroke, and is skipped entirely while the local format check fails.
  useEffect(() => {
    if (!value.trim() || hint) {
      setResult(null);
      onResolvedRef.current?.({ valid: false, message: "", resolved: {} });
      return;
    }
    const t = setTimeout(async () => {
      setChecking(true);
      const r = await validateOnboardingField(kind, value);
      setResult(r);
      setChecking(false);
      onResolvedRef.current?.(r);
    }, VALIDATE_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, kind, hint]);

  return (
    <div className="mb-4">
      <label className="mb-1 flex items-center text-sm font-medium text-default">
        {label}
        {optional && (
          <span className="ml-1 font-normal text-subtle">(optional)</span>
        )}
        {info && <InfoHint text={info} />}
      </label>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value);
          setResult(null);
        }}
        className={inputClass}
      />
      {helpText && <p className="mt-1.5 text-xs text-subtle">{helpText}</p>}
      {checking && <p className="mt-1.5 text-xs text-subtle">Validating…</p>}
      {!checking && result && <ResultLine result={result} />}
      {!checking && !result && hint && (
        <p className="mt-1.5 text-xs text-error">{hint}</p>
      )}
    </div>
  );
}

// --- status badge -----------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "complete"
      ? "bg-link/10 text-link"
      : status === "failed" || status === "rejected"
        ? "bg-error/10 text-error"
        : "bg-accent/10 text-accent";
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-semibold capitalize ${tone}`}
    >
      {status}
    </span>
  );
}

// --- one extra source row ---------------------------------------------------

const SOURCE_KIND: Record<OnboardingSourceType, ValidateKind> = {
  web: "docs",
  confluence: "confluence",
  slack: "slack_channel",
  jira: "jira",
};

const SOURCE_PLACEHOLDER: Record<OnboardingSourceType, string> = {
  web: "https://docs.example.com/…",
  confluence: "https://<org>.atlassian.net/wiki/spaces/KEY",
  slack: "#another-channel",
  jira: "project = ABC AND status != Done",
};

function SourceRow({
  source,
  onChange,
  onRemove,
  onMove,
  canMoveUp,
  canMoveDown,
}: {
  source: OnboardingSource;
  onChange: (s: OnboardingSource) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
}) {
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [checking, setChecking] = useState(false);
  const hint = source.value.trim()
    ? SOURCE_CLIENT_CHECK[source.type](source.value)
    : null;

  useEffect(() => {
    if (!source.value.trim() || hint) {
      setResult(null);
      return;
    }
    const t = setTimeout(async () => {
      setChecking(true);
      setResult(
        await validateOnboardingField(SOURCE_KIND[source.type], source.value)
      );
      setChecking(false);
    }, VALIDATE_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.value, source.type, hint]);

  return (
    <div className="mb-2 flex items-start gap-2">
      <select
        value={source.type}
        onChange={(e) =>
          onChange({ ...source, type: e.target.value as OnboardingSourceType })
        }
        className="rounded-md border border-border-medium bg-background px-2 py-2 text-sm text-default focus:outline-none focus:ring-2 focus:ring-accent/40"
      >
        <option value="confluence">Confluence</option>
        <option value="jira">Jira (filter)</option>
        <option value="slack">Slack channel</option>
        <option value="web">Web / docs</option>
      </select>
      <div className="flex-1">
        <input
          type="text"
          value={source.value}
          placeholder={SOURCE_PLACEHOLDER[source.type]}
          onChange={(e) => {
            onChange({ ...source, value: e.target.value });
            setResult(null);
          }}
          className={inputClass}
        />
        {checking && <p className="mt-1.5 text-xs text-subtle">Validating…</p>}
        {!checking && result && <ResultLine result={result} />}
        {!checking && !result && hint && (
          <p className="mt-1.5 text-xs text-subtle">{hint}</p>
        )}
      </div>
      <div className="flex shrink-0 items-center">
        <button
          type="button"
          onClick={() => onMove(-1)}
          disabled={!canMoveUp}
          title="Higher priority"
          className="p-1.5 text-subtle hover:text-default disabled:opacity-30"
        >
          <FiChevronUp className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => onMove(1)}
          disabled={!canMoveDown}
          title="Lower priority"
          className="p-1.5 text-subtle hover:text-default disabled:opacity-30"
        >
          <FiChevronDown className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={onRemove}
          title="Remove source"
          className="p-1.5 text-subtle hover:text-error"
        >
          <FiTrash2 className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

// --- section shell with a numbered step marker ------------------------------

function Section({
  step,
  title,
  hint,
  children,
}: {
  step: number;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-5 rounded-xl border border-border-medium bg-background-weak p-5 shadow-sm">
      <div className="mb-4 flex items-baseline gap-3">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/10 text-xs font-semibold text-accent">
          {step}
        </span>
        <div>
          <h2 className="text-sm font-semibold text-default">{title}</h2>
          {hint && <p className="mt-0.5 text-xs text-subtle">{hint}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

// --- main form --------------------------------------------------------------

export function OnboardingForm() {
  const searchParams = useSearchParams();
  // "Submit a request" vs "My requests" — surfaced as top tabs so status isn't
  // buried at the bottom. Deep-linkable via ?view=requests (e.g. from the nav).
  const [tab, setTab] = useState<"form" | "requests">(
    searchParams?.get("view") === "requests" ? "requests" : "form"
  );
  const [teamName, setTeamName] = useState("");
  const [channelInput, setChannelInput] = useState("");
  const [channel, setChannel] = useState<{ id: string; name: string } | null>(
    null
  );
  const [systemPrompt, setSystemPrompt] = useState("");
  const [smeEnabled, setSmeEnabled] = useState(false);
  const [smeGroup, setSmeGroup] = useState("");
  const [oncallEnabled, setOncallEnabled] = useState(false);
  const [oncallSchedule, setOncallSchedule] = useState("");
  const [oncallHandles, setOncallHandles] = useState("");

  const [docsCloud, setDocsCloud] = useState("");
  const [docsOnprem, setDocsOnprem] = useState("");
  const [extraSources, setExtraSources] = useState<OnboardingSource[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
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

  async function refreshMine(): Promise<OnboardingRequestSnapshot[]> {
    try {
      const r = await fetch("/api/onboarding/mine");
      if (r.ok) {
        const data = (await r.json()) as OnboardingRequestSnapshot[];
        setMine(data);
        return data;
      }
    } catch {
      /* ignore */
    }
    return [];
  }

  async function loadStatus(id: number) {
    const r = await fetch(`/api/onboarding/${id}/status`);
    if (r.ok) {
      const data = (await r.json()) as OnboardingStatusResponse;
      setStatuses((prev) => ({ ...prev, [id]: data }));
    }
  }

  // On the "My requests" tab, auto-load each request's per-source scrape status
  // (and re-poll every 15s) so the requester can see work happening without
  // clicking anything.
  useEffect(() => {
    if (tab !== "requests") return;
    let active = true;
    const load = async () => {
      const list = await refreshMine();
      if (!active) return;
      await Promise.all(
        list
          .filter((r) => (r.cc_pair_ids?.length ?? 0) > 0)
          .map((r) => loadStatus(r.id))
      );
    };
    void load();
    const interval = setInterval(load, 15000);
    return () => {
      active = false;
      clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

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
    // The bot channel's message history is always indexed.
    if (channel)
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
    setSubmitted(false);
    if (!teamName.trim()) return setError("Enter a team name.");
    if (!channel) return setError("Validate the bot channel first.");
    const sources = buildSources();
    if (sources.length === 0)
      return setError("Add at least one source to index.");

    setSubmitting(true);
    try {
      const res = await fetch("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          team_name: teamName.trim(),
          channel: { channel_id: channel.id, channel_name: channel.name },
          // Response format is standardized to citations for every channel.
          response_type: "citations",
          respond_tag_only: false,
          system_prompt: systemPrompt,
          task_prompt: "",
          sme: { enabled: smeEnabled, group_name: smeGroup },
          oncall: {
            enabled: oncallEnabled,
            schedule: oncallSchedule,
            handles: oncallHandles,
          },
          sources,
        }),
      });
      if (!res.ok) {
        setError(
          (await res.json().catch(() => null))?.detail ||
            `Couldn't submit the request (${res.status}).`
        );
        return;
      }
      setDocsCloud("");
      setDocsOnprem("");
      setExtraSources([]);
      setSubmitted(true);
      await refreshMine();
    } catch {
      setError("Something went wrong submitting the request.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <header className="mb-8">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-accent">
          Team onboarding
        </p>
        <h1 className="text-2xl font-semibold text-default">
          Onboarding Darwin to Slack Channel
        </h1>
        <p className="mt-2 text-sm text-subtle">
          Point Darwin at your team&apos;s knowledge and channel. An admin
          approves the request, then Darwin scrapes your sources and wires up
          the assistant. Every field is checked live before you submit.
        </p>
      </header>

      <div className="mb-6 flex gap-1 border-b border-border">
        {(["form", "requests"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              tab === t
                ? "border-accent text-default"
                : "border-transparent text-subtle hover:text-default"
            }`}
          >
            {t === "form"
              ? "Submit a request"
              : `My requests${mine.length ? ` (${mine.length})` : ""}`}
          </button>
        ))}
      </div>

      {tab === "form" && (
        <>
          <Section step={1} title="Channel & assistant">
            <ValidatedField
              label="Slack Channel"
              placeholder="https://your-workspace.slack.com/archives/C0123ABCDE"
              kind="slack_channel"
              value={channelInput}
              onChange={setChannelInput}
              clientCheck={checkChannelLink}
              helpText="Get the link in Slack: open the channel → click ⋯ (More) → Copy → Copy link. A link lets us verify the exact channel; a name can't be checked reliably."
              info="This is the channel where the Darwin Slack bot answers questions. By default this channel is scraped and kept updated, so its knowledge stays current."
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
            <label className="mb-1 block text-sm font-medium text-default">
              Team name
            </label>
            <input
              value={teamName}
              onChange={(e) => setTeamName(e.target.value)}
              placeholder="e.g. Integration Service"
              className={inputClass}
            />
          </Section>

          <Section
            step={2}
            title="Sources"
            hint="Add everything Darwin should read. Order sets retrieval priority — drag the arrows to reorder."
          >
            <ValidatedField
              label="Docs-URL - Cloud"
              placeholder="https://docs.uipath.com/<product>/automation-cloud/latest"
              kind="docs"
              value={docsCloud}
              onChange={setDocsCloud}
              clientCheck={checkUrl}
              optional
            />
            <ValidatedField
              label="Docs-URL - On-prem"
              placeholder="https://docs.uipath.com/<product>/standalone/latest"
              kind="docs"
              value={docsOnprem}
              onChange={setDocsOnprem}
              clientCheck={checkUrl}
              helpText="Also accepts an automation-suite URL, e.g. https://docs.uipath.com/<product>/automation-suite/<latest-version>"
              optional
            />
            <p className="mb-4 text-xs text-subtle">
              Paste the product root URL only. For automation-suite docs the
              version is stripped and Darwin scrapes the latest 3 versions
              automatically — you don&apos;t need to list each version.
            </p>

            <div className="mb-2 text-sm font-medium text-default">
              Additional sources
            </div>
            {extraSources.map((s, i) => (
              <SourceRow
                key={i}
                source={s}
                canMoveUp={i > 0}
                canMoveDown={i < extraSources.length - 1}
                onChange={(ns) =>
                  setExtraSources((p) => p.map((x, j) => (j === i ? ns : x)))
                }
                onRemove={() =>
                  setExtraSources((p) => p.filter((_, j) => j !== i))
                }
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
                setExtraSources((p) => [
                  ...p,
                  { type: "confluence", value: "" },
                ])
              }
              className="mt-1 inline-flex items-center gap-1 text-sm text-link hover:underline"
            >
              <FiPlus className="h-4 w-4" /> Add source
            </button>
          </Section>

          <Section
            step={3}
            title="Assistant prompt"
            hint="Prefilled from the default (Orchestrator) — edit if your team needs different behavior."
          >
            <textarea
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              rows={6}
              className={`${inputClass} font-mono leading-relaxed`}
            />
          </Section>

          <Section step={4} title="Options">
            <label className="mb-3 flex items-center gap-2 text-sm text-default">
              <input
                type="checkbox"
                checked={smeEnabled}
                onChange={(e) => setSmeEnabled(e.target.checked)}
                className="accent-accent"
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
            <label className="flex items-center gap-2 text-sm text-default">
              <input
                type="checkbox"
                checked={oncallEnabled}
                onChange={(e) => setOncallEnabled(e.target.checked)}
                className="accent-accent"
              />
              On &quot;need more help&quot;, tag the on-call
            </label>
            {oncallEnabled && (
              <div className="mt-3">
                <ValidatedField
                  label="DRI Slack handle(s)"
                  placeholder="@as-dri (comma-separated for multiple)"
                  kind="slack_group"
                  value={oncallHandles}
                  onChange={setOncallHandles}
                  helpText="Slack user-group handle(s) to @-mention when someone needs more help, e.g. @as-dri."
                  optional
                />
                <input
                  value={oncallSchedule}
                  onChange={(e) => setOncallSchedule(e.target.value)}
                  placeholder="OpsGenie schedule name (optional)"
                  className={inputClass}
                />
                <p className="mt-1.5 text-xs text-subtle">
                  Optionally pull the current DRI from an OpsGenie schedule
                  instead of (or in addition to) the handles above.
                </p>
              </div>
            )}
          </Section>

          {error && (
            <p className="mb-3 flex items-center gap-1.5 text-sm text-error">
              <FiX className="h-4 w-4 shrink-0" />
              {error}
            </p>
          )}
          {submitted && !error && (
            <p className="mb-3 flex items-center gap-1.5 text-sm text-link">
              <FiCheck className="h-4 w-4 shrink-0" />
              Request submitted — an admin will review it. Track it below.
            </p>
          )}
          <button
            onClick={submit}
            disabled={submitting || !teamName.trim() || !channel}
            title={
              !channel
                ? "Validate the bot channel first"
                : !teamName.trim()
                  ? "Enter a team name"
                  : undefined
            }
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-inverted transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? "Submitting…" : "Submit onboarding request"}
          </button>
        </>
      )}

      {tab === "requests" && (
        <div>
          <h2 className="mb-3 text-lg font-semibold text-default">
            Your requests
          </h2>
          {mine.length === 0 && (
            <p className="text-sm text-subtle">
              You haven&apos;t submitted any onboarding requests yet.
            </p>
          )}
          {mine.map((r) => (
            <div
              key={r.id}
              className="mb-3 rounded-xl border border-border-medium bg-background-weak p-4 shadow-sm"
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
                      <span className="flex items-center gap-2">
                        <StatusBadge status={s.status} /> {s.docs_indexed} docs
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
