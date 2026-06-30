// "@mention" assistant-picker helpers for the one-shot Search box.
//
// An *active* mention token is an "@" that is either the first character of the
// input OR immediately preceded by whitespace, followed by the partial assistant
// name being typed, anchored at the END of the input (where the caret is). This
// lets the typeahead fire mid-message while ignoring "@" inside a word (e.g. an
// email like "me@foo").
import { assistantDisplayName } from "./displayName";

export const MENTION_TOKEN = /(?:\s|^)@([\w-]*)$/;

/**
 * The partial assistant name currently being typed after "@", or `null` when the
 * caret is not inside an active mention token. "" means "@" was just typed.
 */
export function getMentionQuery(text: string): string | null {
  const match = text.match(MENTION_TOKEN);
  return match ? match[1] : null;
}

/**
 * Assistants whose display name OR raw name starts with the (case-insensitive)
 * mention query. Empty query returns all. Generic so callers can pass Personas
 * or trimmed test fixtures.
 */
export function filterAssistantsByMention<
  T extends { name: string; display_name?: string | null },
>(assistants: T[], query: string): T[] {
  const q = query.toLowerCase();
  return assistants.filter(
    (a) =>
      assistantDisplayName(a).toLowerCase().startsWith(q) ||
      a.name.toLowerCase().startsWith(q)
  );
}

/**
 * Drop the partial "@mention" token the user was typing and prepend a canonical
 * "@<label> " annotation (label = the chosen assistant's display name) to the
 * FRONT of the message — i.e. "address this question to X". Keeping it at the
 * start makes strip/has deterministic (vs. an annotation buried mid-text) and
 * needs no pinned chip — the choice reads inline as text.
 */
export function applyMentionLabel(text: string, label: string): string {
  const rest = text.replace(/(?:^|\s)@[\w-]*$/, "").trim();
  return rest ? `@${label} ${rest}` : `@${label} `;
}

/**
 * Strip a leading "@<label>" annotation (the explicit assistant pick) from the
 * message, returning the clean question to search with. Matches the exact label
 * so multi-word assistant names are handled. No-op if the annotation isn't there.
 */
export function stripMentionLabel(text: string, label: string): string {
  const trimmed = text.replace(/^\s+/, "");
  const prefix = `@${label}`;
  if (trimmed.toLowerCase().startsWith(prefix.toLowerCase())) {
    return trimmed.slice(prefix.length).replace(/^\s+/, "");
  }
  return text;
}

/** Whether the message still carries the "@<label>" annotation (caret-agnostic). */
export function hasMentionLabel(text: string, label: string): boolean {
  return text.replace(/^\s+/, "").toLowerCase().startsWith(`@${label}`.toLowerCase());
}
