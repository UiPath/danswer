export interface Settings {
  chat_page_enabled: boolean;
  search_page_enabled: boolean;
  default_page: "search" | "chat";
  // Show the "Create assistant" entry points (Create buttons on My Assistants +
  // Gallery, and the chat @-mention "Create a new assistant" row). Mirrors backend
  // Settings.enable_assistant_creation. Defaults to hidden when absent/false.
  enable_assistant_creation?: boolean;
  maximum_chat_retention_days: number | null;
  // Byte cap for chat file uploads (mirrors backend CHAT_FILE_MAX_SIZE_MB).
  chat_file_max_size_mb?: number;
  // Cluster-level enablement (mirrors backend RERANK_ENABLED /
  // LLM_RELEVANCE_FILTER_ENABLED). When false the chat + assistant UIs hide the
  // corresponding rerank / relevance toggles.
  rerank_enabled?: boolean;
  llm_relevance_filter_enabled?: boolean;
  // Staged rollout of the auto-routed Search tab (mirrors backend
  // AutoSearchRollout). The Search tab + endpoint are gated by this; the
  // backend enforces it independently of the UI.
  auto_search_rollout?: "off" | "admin_only" | "everyone";
  // Enable the semantic intent pre-route (LLM phrase matcher). Default off.
  auto_search_intent_enabled?: boolean;
  // Show two answers side by side (single top-1 vs union of top matches) for
  // AI-router picks on the Search tab. Default on.
  auto_search_compare_enabled?: boolean;
}

export interface EnterpriseSettings {
  application_name: string | null;
  use_custom_logo: boolean;

  // custom Chat components
  custom_header_content: string | null;
  custom_popup_header: string | null;
  custom_popup_content: string | null;
}

export interface CombinedSettings {
  settings: Settings;
  enterpriseSettings: EnterpriseSettings | null;
  customAnalyticsScript: string | null;
}
