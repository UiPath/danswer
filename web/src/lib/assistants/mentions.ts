// "@mention" assistant-picker helpers for the chat composer.
//
// An *active* mention token is an "@" that is either the first character of the
// input OR immediately preceded by whitespace, followed by the partial assistant
// name being typed, anchored at the END of the input (i.e. where the caret is).
// This lets the typeahead fire anywhere in the message — not just at the start —
// while ignoring "@" inside a word (e.g. an email address like "me@foo").
//
// These are extracted as pure functions so the chat-input behavior is unit
// testable; ChatInputBar wires them to its textarea state.
import { assistantDisplayName } from "./displayName";

export const MENTION_TOKEN = /(?:\s|^)@(\w*)$/;

/**
 * The partial assistant name currently being typed after "@", or `null` when the
 * caret is not inside an active mention token. An empty string ("") means "@"
 * was just typed with no name yet — show the full list.
 */
export function getMentionQuery(text: string): string | null {
  const match = text.match(MENTION_TOKEN);
  return match ? match[1] : null;
}

/**
 * Assistants whose display name OR raw name starts with the (case-insensitive)
 * mention query. An empty query returns all assistants (the just-typed-"@" case).
 * Generic so callers can pass full `Persona`s (or trimmed test fixtures).
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
 * Remove only the trailing "@mention" token (and a single preceding space) the
 * user was typing, preserving the rest of the message. Used when an assistant is
 * picked so a question already typed around the mention isn't discarded.
 */
export function stripMentionToken(text: string): string {
  return text.replace(/(?:\s)?@\w*$/, "");
}
