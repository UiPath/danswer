// Lightweight, synchronous client-side format checks for the onboarding form.
// They catch obvious mistakes instantly (before any network round-trip) and
// gate the debounced backend validation. Each returns a short hint string, or
// null when the value looks fine to send to the server.

import { OnboardingSourceType } from "./interfaces";

export type ClientCheck = (value: string) => string | null;

export const checkChannel: ClientCheck = (v) => {
  const raw = v.trim().replace(/^#/, "");
  if (!raw) return null;
  // A channel id or a pasted link/mention — let the server resolve it.
  if (
    /^C[A-Z0-9]{6,}$/.test(raw) ||
    raw.includes("slack.com/") ||
    raw.includes("<#")
  )
    return null;
  if (/\s/.test(raw))
    return "Channel names have no spaces — paste a link to be sure.";
  if (/[A-Z]/.test(raw)) return "Channel names are lowercase.";
  return null;
};

// The bot channel must be a link (a name can't be verified reliably). Accept a
// channel link / <#…> mention / raw id; anything else prompts for the link.
export const checkChannelLink: ClientCheck = (v) => {
  const raw = v.trim();
  if (!raw) return null;
  if (
    /\/archives\/C[A-Z0-9]+/.test(raw) ||
    /^<#C[A-Z0-9]+/.test(raw) ||
    /^C[A-Z0-9]{6,}$/.test(raw)
  )
    return null;
  return "Paste the channel link, not the name (steps below).";
};

export const checkUrl: ClientCheck = (v) =>
  /^https?:\/\//i.test(v.trim()) ? null : "Start with https://";

export const checkJql: ClientCheck = (v) =>
  v.trim().length < 3 ? "Enter a JQL filter, e.g. project = ABC" : null;

export const SOURCE_CLIENT_CHECK: Record<OnboardingSourceType, ClientCheck> = {
  web: checkUrl,
  confluence: checkUrl,
  slack: checkChannel,
  jira: checkJql,
};
