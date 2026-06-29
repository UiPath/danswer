export interface Settings {
  chat_page_enabled: boolean;
  search_page_enabled: boolean;
  default_page: "search" | "chat";
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
