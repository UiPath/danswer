import { describe, it, expect } from "vitest";
import { getSafeNextPath } from "./safeRedirect";

describe("getSafeNextPath", () => {
  describe("accepts safe same-origin relative paths under the allowlist", () => {
    it("plain /chat", () => {
      expect(getSafeNextPath("/chat")).toBe("/chat");
    });
    it("/chat with an assistant query param", () => {
      expect(getSafeNextPath("/chat?assistant=AutomationSuite")).toBe(
        "/chat?assistant=AutomationSuite"
      );
    });
    it("/chat with encoded space + utm params", () => {
      const v = "/chat?assistant=Automation%20Suite&utm_source=slack";
      expect(getSafeNextPath(v)).toBe(v);
    });
    it("a subpath of /chat", () => {
      expect(getSafeNextPath("/chat/foo")).toBe("/chat/foo");
    });
    it("drops any fragment", () => {
      expect(getSafeNextPath("/chat?x=1#frag")).toBe("/chat?x=1");
    });
  });

  describe("rejects open-redirect / off-origin attempts", () => {
    const bad = [
      null,
      undefined,
      "",
      "//evil.com",
      "///evil.com",
      "https://evil.com",
      "https://evil.com/chat",
      "http://evil.com",
      "/\\evil.com", // backslash treated as slash by browsers
      "/\\/evil.com",
      "/%2f%2fevil.com", // encoded double slash
      "/%2F%2Fevil.com",
      "/%5cevil.com", // encoded backslash
      "https://darwin.invalid@evil.com/chat", // userinfo smuggling
      "javascript:alert(1)",
      "data:text/html,<script>1</script>",
      "mailto:x@y.com",
    ];
    bad.forEach((v) => {
      it(`rejects ${JSON.stringify(v)}`, () => {
        expect(getSafeNextPath(v)).toBeNull();
      });
    });
  });

  describe("rejects non-allowlisted destinations", () => {
    const notAllowed = [
      "/", // root not allowlisted
      "/admin",
      "/admin/api-key",
      "/auth/login",
      "/search", // only /chat is allowlisted
      "/chatx", // prefix must be exact segment, not substring
      "chat", // no leading slash
      " /chat", // leading space
    ];
    notAllowed.forEach((v) => {
      it(`rejects ${JSON.stringify(v)}`, () => {
        expect(getSafeNextPath(v)).toBeNull();
      });
    });
  });

  describe("rejects injection / abuse", () => {
    it("rejects CRLF (header/cookie injection)", () => {
      expect(getSafeNextPath("/chat\r\nSet-Cookie: x=1")).toBeNull();
    });
    it("rejects a newline", () => {
      expect(getSafeNextPath("/chat\nfoo")).toBeNull();
    });
    it("rejects a null byte", () => {
      expect(getSafeNextPath("/chat\x00")).toBeNull();
    });
    it("rejects an over-long value", () => {
      expect(getSafeNextPath("/chat?q=" + "a".repeat(600))).toBeNull();
    });
    it("allows a query param whose VALUE looks like a url (it's not a redirect target)", () => {
      expect(getSafeNextPath("/chat?ref=https://x.com")).toBe(
        "/chat?ref=https://x.com"
      );
    });
  });
});
