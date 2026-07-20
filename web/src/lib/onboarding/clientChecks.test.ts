import { describe, it, expect } from "vitest";
import {
  checkChannel,
  checkChannelLink,
  checkJql,
  checkUrl,
  SOURCE_CLIENT_CHECK,
} from "./clientChecks";

describe("checkChannelLink (bot channel must be a link)", () => {
  it("accepts a channel link", () => {
    expect(
      checkChannelLink("https://uipath-product.slack.com/archives/C0ABC123/p1")
    ).toBeNull();
  });
  it("accepts a <#…> mention and a raw id", () => {
    expect(checkChannelLink("<#C0ABC123|help-team>")).toBeNull();
    expect(checkChannelLink("C0ABC123")).toBeNull();
  });
  it("rejects a bare channel name", () => {
    expect(checkChannelLink("help-automation-suite")).toMatch(/link/i);
    expect(checkChannelLink("#help-team")).toMatch(/link/i);
  });
  it("treats empty as no hint", () => {
    expect(checkChannelLink("")).toBeNull();
    expect(checkChannelLink("   ")).toBeNull();
  });
});

describe("checkChannel (additional slack source, name-friendly)", () => {
  it("accepts a lowercase name, id, link", () => {
    expect(checkChannel("help-team")).toBeNull();
    expect(checkChannel("#help-team")).toBeNull();
    expect(checkChannel("C0ABC123")).toBeNull();
  });
  it("flags spaces and uppercase", () => {
    expect(checkChannel("help team")).toMatch(/spaces/i);
    expect(checkChannel("Help-Team")).toMatch(/lowercase/i);
  });
});

describe("checkUrl", () => {
  it("accepts http(s) urls", () => {
    expect(checkUrl("https://docs.uipath.com/x")).toBeNull();
    expect(checkUrl("http://example.com")).toBeNull();
  });
  it("rejects non-urls", () => {
    expect(checkUrl("docs.uipath.com")).toMatch(/https/i);
    expect(checkUrl("ftp://x")).toMatch(/https/i);
  });
});

describe("checkJql", () => {
  it("accepts a plausible filter", () => {
    expect(checkJql("project = ABC")).toBeNull();
  });
  it("rejects an empty/too-short filter", () => {
    expect(checkJql("")).toMatch(/JQL/i);
    expect(checkJql("a")).toMatch(/JQL/i);
  });
});

describe("SOURCE_CLIENT_CHECK maps each source type", () => {
  it("routes web/confluence to url, slack to channel, jira to jql", () => {
    expect(SOURCE_CLIENT_CHECK.web("nope")).toMatch(/https/i);
    expect(
      SOURCE_CLIENT_CHECK.confluence("https://x.atlassian.net")
    ).toBeNull();
    expect(SOURCE_CLIENT_CHECK.slack("Bad Name")).toMatch(/lowercase|spaces/i);
    expect(SOURCE_CLIENT_CHECK.jira("project = ABC")).toBeNull();
  });
});
