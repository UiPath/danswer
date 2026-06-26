import { describe, it, expect } from "vitest";
import {
  getMentionQuery,
  filterAssistantsByMention,
  stripMentionToken,
} from "./mentions";

// Minimal assistant fixtures — the helpers only read `name` / `display_name`.
type A = { name: string; display_name?: string | null };
const ASSISTANTS: A[] = [
  { name: "AMER Benefits" },
  { name: "amer-payroll" },
  { name: "internal-hr-bot", display_name: "HR Helper" },
  { name: "kb-search", display_name: "Knowledge" },
  { name: "Orchestrator" },
];
const names = (list: A[]) => list.map((a) => a.name);

describe("getMentionQuery (when does the @ typeahead fire, and on what query)", () => {
  it("returns '' for a bare '@' (just typed, show full list)", () => {
    expect(getMentionQuery("@")).toBe("");
  });

  it("captures the partial name after '@'", () => {
    expect(getMentionQuery("@AM")).toBe("AM");
    expect(getMentionQuery("@AMER")).toBe("AMER");
    expect(getMentionQuery("@123")).toBe("123"); // \w includes digits
  });

  it("fires mid-message (after whitespace), not just at the start", () => {
    expect(getMentionQuery("what is @AM")).toBe("AM");
    expect(getMentionQuery("what is the 401k policy @AMER")).toBe("AMER");
    expect(getMentionQuery("line one\n@bob")).toBe("bob"); // newline counts as whitespace
  });

  it("returns null when there is no active mention token", () => {
    expect(getMentionQuery("")).toBeNull();
    expect(getMentionQuery("hello world")).toBeNull();
  });

  it("ignores '@' embedded in a word (e.g. an email address)", () => {
    expect(getMentionQuery("contact me@foo")).toBeNull();
  });

  it("closes (null) once the mention isn't anchored at the caret/end", () => {
    expect(getMentionQuery("@AMER what")).toBeNull(); // text typed after the mention
    expect(getMentionQuery("@AMER ")).toBeNull(); // a trailing space ends the token
  });
});

describe("filterAssistantsByMention (case-insensitive, name OR display name)", () => {
  it("returns all assistants for an empty query (the just-typed-'@' case)", () => {
    expect(filterAssistantsByMention(ASSISTANTS, "")).toHaveLength(
      ASSISTANTS.length
    );
  });

  it("matches the raw name, case-insensitively", () => {
    expect(names(filterAssistantsByMention(ASSISTANTS, "am"))).toEqual([
      "AMER Benefits",
      "amer-payroll",
    ]);
    expect(names(filterAssistantsByMention(ASSISTANTS, "ORCH"))).toEqual([
      "Orchestrator",
    ]);
  });

  it("matches the display name even when it differs from the raw name", () => {
    // "HR Helper" matches "hr"; its raw name "internal-hr-bot" does not start with "hr".
    expect(names(filterAssistantsByMention(ASSISTANTS, "hr"))).toEqual([
      "internal-hr-bot",
    ]);
  });

  it("still matches the raw name when a display name is also set", () => {
    // query "kb" matches raw name "kb-search" (display "Knowledge" does not).
    expect(names(filterAssistantsByMention(ASSISTANTS, "kb"))).toEqual([
      "kb-search",
    ]);
  });

  it("returns [] when nothing matches", () => {
    expect(filterAssistantsByMention(ASSISTANTS, "zzz")).toEqual([]);
  });
});

describe("stripMentionToken (preserve typed text when an assistant is picked)", () => {
  it("clears a start-of-message mention", () => {
    expect(stripMentionToken("@AMER")).toBe("");
    expect(stripMentionToken("@a")).toBe("");
    expect(stripMentionToken("@")).toBe("");
    expect(stripMentionToken("hi @")).toBe("hi");
  });

  it("removes only the trailing mention token, keeping the question", () => {
    expect(stripMentionToken("what is the 401k @AMER")).toBe(
      "what is the 401k"
    );
    expect(stripMentionToken("line one\n@bob")).toBe("line one");
  });

  it("leaves text without a trailing mention untouched", () => {
    expect(stripMentionToken("plain question with no mention")).toBe(
      "plain question with no mention"
    );
  });
});

// ---------------------------------------------------------------------------
// Integration: compose the helpers exactly as ChatInputBar's handlers do, to
// exercise the end-to-end typeahead flow without a DOM harness.
//   - handleInputChange: show suggestions iff getMentionQuery(text) !== null
//   - filteredPersonas:  filterAssistantsByMention(personas, query ?? "")
//   - updateCurrentPersona (on select): message becomes stripMentionToken(message)
// ---------------------------------------------------------------------------
function typeahead(input: string, assistants: A[]) {
  const query = getMentionQuery(input);
  const showSuggestions = query !== null;
  const suggestions = showSuggestions
    ? filterAssistantsByMention(assistants, query ?? "")
    : [];
  return { showSuggestions, suggestions };
}

describe("integration: @mention typeahead flow", () => {
  it("opens and narrows the list as the user types the mention", () => {
    expect(typeahead("@", ASSISTANTS).suggestions).toHaveLength(
      ASSISTANTS.length
    );
    expect(names(typeahead("@am", ASSISTANTS).suggestions)).toEqual([
      "AMER Benefits",
      "amer-payroll",
    ]);
    expect(names(typeahead("@orch", ASSISTANTS).suggestions)).toEqual([
      "Orchestrator",
    ]);
  });

  it("ends the mention at a space (multi-word names aren't typeable past word 1)", () => {
    // Known limitation: \w stops at the space, so "@amer b" is treated as a
    // finished mention "@amer" + literal " b" — the typeahead closes. Users
    // narrow with the first word ("@amer") then pick from the list.
    expect(typeahead("@amer b", ASSISTANTS).showSuggestions).toBe(false);
  });

  it("opens for a mid-message mention after a typed question", () => {
    const { showSuggestions, suggestions } = typeahead(
      "what is the 401k policy @hr",
      ASSISTANTS
    );
    expect(showSuggestions).toBe(true);
    expect(names(suggestions)).toEqual(["internal-hr-bot"]); // matched via display name
  });

  it("stays closed for ordinary text and email addresses", () => {
    expect(typeahead("what is the 401k policy", ASSISTANTS).showSuggestions).toBe(
      false
    );
    expect(typeahead("email me@uipath", ASSISTANTS).showSuggestions).toBe(false);
  });

  it("preserves the typed question when an assistant is selected mid-message", () => {
    const input = "what is the 401k policy @amer";
    // user picks an assistant -> ChatInputBar sets message = stripMentionToken(message)
    expect(stripMentionToken(input)).toBe("what is the 401k policy");
  });

  it("leaves an empty composer when the mention was the whole message", () => {
    expect(stripMentionToken("@amer")).toBe("");
  });
});
