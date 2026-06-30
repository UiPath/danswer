import { describe, it, expect } from "vitest";
import {
  getMentionQuery,
  filterAssistantsByMention,
  applyMentionLabel,
  stripMentionLabel,
  hasMentionLabel,
} from "./mentions";

type A = { name: string; display_name?: string | null };
const ASSISTANTS: A[] = [
  { name: "Orchestrator" },
  { name: "AMERBenefits", display_name: "AMER Benefits" },
  { name: "help-ownership", display_name: "Account Ownership" },
];
const names = (l: A[]) => l.map((a) => a.name);

describe("getMentionQuery", () => {
  it("captures the partial after @, start or mid-message", () => {
    expect(getMentionQuery("@")).toBe("");
    expect(getMentionQuery("@Orch")).toBe("Orch");
    expect(getMentionQuery("ask @help-own")).toBe("help-own"); // hyphen allowed
  });
  it("is null when not in a mention or after a completed token", () => {
    expect(getMentionQuery("hello")).toBeNull();
    expect(getMentionQuery("me@foo")).toBeNull(); // embedded in a word
    expect(getMentionQuery("@AMER Benefits ")).toBeNull(); // trailing space ends it
  });
});

describe("filterAssistantsByMention (name OR display name)", () => {
  it("matches display name", () => {
    expect(names(filterAssistantsByMention(ASSISTANTS, "amer"))).toEqual([
      "AMERBenefits",
    ]);
    expect(names(filterAssistantsByMention(ASSISTANTS, "account"))).toEqual([
      "help-ownership",
    ]);
  });
  it("matches raw name and is case-insensitive", () => {
    expect(names(filterAssistantsByMention(ASSISTANTS, "ORCH"))).toEqual([
      "Orchestrator",
    ]);
  });
});

describe("annotation round-trip (skip-routing correctness)", () => {
  it("applyMentionLabel prepends a canonical '@<label> ' annotation to the front", () => {
    expect(applyMentionLabel("@AM", "AMER Benefits")).toBe("@AMER Benefits ");
    // The partial token is removed and the annotation moves to the front
    // ("address this question to X") so strip/has are deterministic.
    expect(applyMentionLabel("what is 401k @AM", "AMER Benefits")).toBe(
      "@AMER Benefits what is 401k"
    );
  });

  it("stripMentionLabel recovers the clean question (multi-word label)", () => {
    expect(
      stripMentionLabel("@AMER Benefits what is the 401k policy", "AMER Benefits")
    ).toBe("what is the 401k policy");
    // No annotation -> unchanged.
    expect(stripMentionLabel("plain question", "AMER Benefits")).toBe(
      "plain question"
    );
  });

  it("hasMentionLabel detects the annotation, caret-agnostic", () => {
    expect(hasMentionLabel("@AMER Benefits how do I", "AMER Benefits")).toBe(true);
    expect(hasMentionLabel("  @AMER Benefits x", "AMER Benefits")).toBe(true); // leading ws
    expect(hasMentionLabel("how do I", "AMER Benefits")).toBe(false);
    // Annotation removed by the user -> false (selection should be dropped).
    expect(hasMentionLabel("how do I @AM", "AMER Benefits")).toBe(false);
  });

  it("full cycle: pick -> annotate -> strip equals the typed question", () => {
    const typed = "what is the 401k policy @AM";
    const annotated = applyMentionLabel(typed, "AMER Benefits");
    expect(hasMentionLabel(annotated, "AMER Benefits")).toBe(true);
    expect(stripMentionLabel(annotated, "AMER Benefits")).toBe(
      "what is the 401k policy"
    );
  });
});
