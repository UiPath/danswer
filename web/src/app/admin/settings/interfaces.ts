export interface Settings {
  chat_page_enabled: boolean;
  search_page_enabled: boolean;
  default_page: "search" | "chat";
  maximum_chat_retention_days: number | null;
  // Byte cap for chat file uploads (mirrors backend CHAT_FILE_MAX_SIZE_MB).
  chat_file_max_size_mb?: number;
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
