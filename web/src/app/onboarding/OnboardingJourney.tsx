import Link from "next/link";
import { FiCheck, FiX } from "react-icons/fi";
import {
  countSourceStatuses,
  OnboardingRequestSnapshot,
  SourceStatus,
} from "@/lib/onboarding/interfaces";

// Visual "light-up" journey for an onboarding request: the milestones from
// submission through the assistant going live in Slack, each derived from real
// request state (status + the FK ids finalize sets), so an admin can see at a
// glance how far along a request is and where it stalled if it did.
//
// Milestones after indexing (document set / assistant / Slack bot) are written
// together by the finalizer, so they normally flip done in one step — showing
// them separately still communicates the pipeline and pinpoints a partial
// finalize failure.

type StepState = "done" | "current" | "failed" | "pending";

function Node({ state }: { state: StepState }) {
  const base =
    "flex h-6 w-6 items-center justify-center rounded-full text-xs shrink-0";
  if (state === "done") {
    return (
      <span className={`${base} bg-link text-inverted`}>
        <FiCheck className="h-3.5 w-3.5" />
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className={`${base} bg-error text-inverted`}>
        <FiX className="h-3.5 w-3.5" />
      </span>
    );
  }
  if (state === "current") {
    return (
      <span className={`${base} border-2 border-accent`}>
        <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
      </span>
    );
  }
  return <span className={`${base} border border-border-medium`} />;
}

function labelTone(state: StepState): string {
  if (state === "done") return "text-default";
  if (state === "current") return "text-accent font-medium";
  if (state === "failed") return "text-error font-medium";
  return "text-subtle";
}

export function OnboardingJourney({
  request,
  sources,
  admin = false,
}: {
  request: OnboardingRequestSnapshot;
  sources?: SourceStatus[];
  // Admin view: linkify the created artifacts (document set / assistant / Slack
  // bot) to their admin pages. Those routes are admin-only, so the requester
  // view leaves them as plain labels (the "Live" → chat link works for both).
  admin?: boolean;
}) {
  // Rejected / cancelled requests never enter the light-up pipeline.
  if (request.status === "rejected" || request.status === "cancelled") {
    return null;
  }

  const counts =
    sources && sources.length ? countSourceStatuses(sources) : null;
  const s = request.status;
  const indexedDone =
    request.document_set_id != null ||
    s === "complete" ||
    (!!counts && counts.total > 0 && counts.succeeded === counts.total);

  // Each artifact gets a link only once its id exists ("as and when available").
  const docId = request.document_set_id;
  const personaId = request.persona_id;
  const cfgId = request.slack_bot_config_id;

  const raw: {
    label: string;
    done: boolean;
    detail?: string;
    href?: string;
  }[] = [
    { label: "Submitted", done: true },
    { label: "Approved", done: s !== "pending" },
    {
      label: "Sources indexed",
      done: indexedDone,
      detail: counts ? `${counts.succeeded}/${counts.total}` : undefined,
    },
    {
      label: "Document set",
      done: docId != null,
      href:
        admin && docId != null ? `/admin/documents/sets/${docId}` : undefined,
    },
    {
      label: "Assistant",
      done: personaId != null,
      href:
        admin && personaId != null
          ? `/admin/assistants/${personaId}`
          : undefined,
    },
    {
      label: "Slack bot",
      done: cfgId != null,
      href: admin && cfgId != null ? `/admin/bot/${cfgId}` : undefined,
    },
    {
      label: "Live",
      done: s === "complete",
      // Open the finished assistant in chat — useful to admin and requester.
      href:
        s === "complete" && personaId != null
          ? `/chat?assistantId=${personaId}`
          : undefined,
    },
  ];

  // The first not-yet-done step is where we are now — "current" while work is
  // ongoing, "failed" if the request has failed at this point.
  const firstPending = raw.findIndex((r) => !r.done);
  const steps = raw.map((r, i) => {
    let state: StepState;
    if (r.done) state = "done";
    else if (i === firstPending) state = s === "failed" ? "failed" : "current";
    else state = "pending";
    return { ...r, state };
  });

  return (
    <ol className="mt-3 flex flex-wrap items-start gap-y-3">
      {steps.map((step, i) => (
        <li key={step.label} className="flex items-start">
          <div className="flex w-16 flex-col items-center text-center">
            <Node state={step.state} />
            {step.href ? (
              <Link
                href={step.href}
                className={`mt-1 text-[10px] leading-tight underline-offset-2 hover:underline ${labelTone(
                  step.state
                )}`}
              >
                {step.label}
              </Link>
            ) : (
              <span
                className={`mt-1 text-[10px] leading-tight ${labelTone(
                  step.state
                )}`}
              >
                {step.label}
              </span>
            )}
            {step.detail && (
              <span className="text-[10px] text-subtle">{step.detail}</span>
            )}
          </div>
          {i < steps.length - 1 && (
            <span
              className={`mt-3 h-px w-4 shrink-0 ${
                step.state === "done" ? "bg-link" : "bg-border-medium"
              }`}
            />
          )}
        </li>
      ))}
    </ol>
  );
}
