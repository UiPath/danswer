import { ReadonlyURLSearchParams } from "next/navigation";

// search params
export const SEARCH_PARAM_NAMES = {
  CHAT_ID: "chatId",
  PERSONA_ID: "assistantId",
  // Human-readable assistant name (e.g. ?assistant=Orchestrator). Resolved to an
  // id server-side against the user's ACL-filtered assistants, so shareable links
  // (e.g. per-channel Slack "Ask Darwin" workflows) can preselect an assistant
  // without knowing its numeric id. `assistantId` (numeric) takes precedence.
  PERSONA_NAME: "assistant",
  // overrides
  TEMPERATURE: "temperature",
  MODEL_VERSION: "model-version",
  SYSTEM_PROMPT: "system-prompt",
  // user message
  USER_MESSAGE: "user-message",
  SUBMIT_ON_LOAD: "submit-on-load",
  // chat title
  TITLE: "title",
  // for seeding chats
  SEEDED: "seeded",
};

export function shouldSubmitOnLoad(searchParams: ReadonlyURLSearchParams) {
  const rawSubmitOnLoad = searchParams.get(SEARCH_PARAM_NAMES.SUBMIT_ON_LOAD);
  if (rawSubmitOnLoad === "true" || rawSubmitOnLoad === "1") {
    return true;
  }
  return false;
}
