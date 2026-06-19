import { describe, it, expect } from "vitest";
import { assistantDisplayName } from "./displayName";

describe("assistantDisplayName", () => {
  it("returns display_name when it is set", () => {
    expect(
      assistantDisplayName({ display_name: "Knowledge Bot", name: "kb_internal" })
    ).toBe("Knowledge Bot");
  });

  it("falls back to name when display_name is null or undefined", () => {
    expect(assistantDisplayName({ display_name: null, name: "Darwin" })).toBe(
      "Darwin"
    );
    expect(assistantDisplayName({ name: "Darwin" })).toBe("Darwin");
  });

  it("falls back to name when display_name is blank/whitespace", () => {
    expect(assistantDisplayName({ display_name: "   ", name: "Darwin" })).toBe(
      "Darwin"
    );
  });

  it("trims surrounding whitespace on display_name", () => {
    expect(
      assistantDisplayName({ display_name: "  Knowledge Bot  ", name: "kb" })
    ).toBe("Knowledge Bot");
  });
});
