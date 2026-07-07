import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createSlackBotConfig, updateSlackBotConfig } from "./lib";

// The Slack bot config request body is assembled by an explicit field whitelist
// (`buildRequestBodyFromCreationRequest`). That whitelist silently drops any field
// not listed — which once shipped a bug where SME-verification settings never
// persisted. These tests pin that new config fields actually reach the request
// body on BOTH create (POST) and update (PATCH).

function makeRequest(overrides: Record<string, unknown> = {}) {
  return {
    document_sets: [],
    persona_id: 1,
    channel_names: ["help-hitl"],
    answer_validity_check_enabled: false,
    questionmark_prefilter_enabled: false,
    respond_tag_only: false,
    respond_to_bots: false,
    respond_team_member_list: [],
    respond_slack_group_list: [],
    usePersona: true,
    response_type: "citations",
    opsgenie_schedule: "as-oncall",
    enable_sme_validation: true,
    sme_group_name: "Automation Suite SMEs",
    ...overrides,
  } as unknown as Parameters<typeof createSlackBotConfig>[0];
}

describe("slack bot config request body", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const sentBody = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

  it("createSlackBotConfig sends the SME fields", async () => {
    await createSlackBotConfig(makeRequest());
    const body = sentBody();
    expect(body.enable_sme_validation).toBe(true);
    expect(body.sme_group_name).toBe("Automation Suite SMEs");
  });

  it("updateSlackBotConfig PATCHes the right url and sends the SME fields", async () => {
    await updateSlackBotConfig(41, makeRequest());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/manage/admin/slack-bot/config/41",
      expect.objectContaining({ method: "PATCH" })
    );
    const body = sentBody();
    expect(body.enable_sme_validation).toBe(true);
    expect(body.sme_group_name).toBe("Automation Suite SMEs");
  });

  it("regression: existing fields (opsgenie_schedule) still pass through", async () => {
    await createSlackBotConfig(makeRequest());
    expect(sentBody().opsgenie_schedule).toBe("as-oncall");
  });

  it("SME off => body carries the falsey values (feature stays disabled)", async () => {
    await createSlackBotConfig(
      makeRequest({ enable_sme_validation: false, sme_group_name: undefined })
    );
    const body = sentBody();
    expect(body.enable_sme_validation).toBe(false);
    expect(body.sme_group_name).toBeUndefined();
  });
});
