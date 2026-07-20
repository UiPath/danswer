// Types for the self-serve Darwin onboarding flow. Mirrors the backend models in
// danswer/server/features/onboarding/.

export type OnboardingSourceType = "web" | "confluence" | "slack" | "jira";

export interface OnboardingSource {
  type: OnboardingSourceType;
  value: string;
  label?: string | null;
}

export interface ChannelRef {
  channel_id: string;
  channel_name: string;
}

export interface OnboardingSubmitRequest {
  team_name: string;
  channel: ChannelRef;
  response_type: "citations" | "quotes";
  respond_tag_only: boolean;
  system_prompt: string;
  task_prompt: string;
  sme: { enabled: boolean; group_name: string };
  oncall: { enabled: boolean; schedule: string; handles: string };
  jira: {
    enabled: boolean;
    project_key: string;
    issue_type: string;
    component: string;
  };
  sources: OnboardingSource[];
}

export interface ValidationResult {
  valid: boolean;
  message: string;
  resolved: Record<string, unknown>;
}

export type OnboardingRequestStatus =
  | "pending"
  | "rejected"
  | "cancelled"
  | "provisioning"
  | "indexing"
  | "complete"
  | "failed";

export interface OnboardingRequestSnapshot {
  id: number;
  requester_email: string;
  status: OnboardingRequestStatus;
  payload: OnboardingSubmitRequest;
  decision_reason: string | null;
  error_msg: string | null;
  persona_id: number | null;
  document_set_id: number | null;
  slack_bot_config_id: number | null;
  cc_pair_ids: number[] | null;
  created_at: string;
  updated_at: string;
}

export interface SourceStatus {
  cc_pair_id: number;
  name: string;
  status: string;
  docs_indexed: number;
  error_msg: string | null;
}

export interface OnboardingStatusResponse {
  request_id: number;
  status: OnboardingRequestStatus;
  sources: SourceStatus[];
}

export interface SourceStatusCounts {
  total: number;
  succeeded: number;
  indexing: number;
  pending: number;
  failed: number;
}

// Roll the per-source scrape statuses up into counts for an at-a-glance summary
// (e.g. "4/6 succeeded · 1 indexing · 1 failed"). Anything that isn't
// success/in_progress/failed (i.e. not_started) counts as pending.
export function countSourceStatuses(
  sources: SourceStatus[]
): SourceStatusCounts {
  const counts: SourceStatusCounts = {
    total: sources.length,
    succeeded: 0,
    indexing: 0,
    pending: 0,
    failed: 0,
  };
  for (const s of sources) {
    if (s.status === "success") counts.succeeded += 1;
    else if (s.status === "in_progress") counts.indexing += 1;
    else if (s.status === "failed") counts.failed += 1;
    else counts.pending += 1;
  }
  return counts;
}

export type ValidateKind =
  "slack_channel" | "slack_group" | "confluence" | "github" | "jira" | "docs";

export async function validateOnboardingField(
  kind: ValidateKind,
  value: string
): Promise<ValidationResult> {
  const res = await fetch("/api/onboarding/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, value }),
  });
  if (!res.ok) {
    return {
      valid: false,
      message: `Couldn't validate (${res.status})`,
      resolved: {},
    };
  }
  return (await res.json()) as ValidationResult;
}
