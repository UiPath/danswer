import { EnterpriseSettings, Settings } from "@/app/admin/settings/interfaces";
import {
  CUSTOM_ANALYTICS_ENABLED,
  SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED,
} from "@/lib/constants";
import { fetchSS } from "@/lib/utilsSS";

export async function fetchSettingsSS() {
  const tasks = [fetchSS("/settings")];
  if (SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED) {
    tasks.push(fetchSS("/enterprise-settings"));
    if (CUSTOM_ANALYTICS_ENABLED) {
      tasks.push(fetchSS("/enterprise-settings/custom-analytics-script"));
    }
  }

  const results = await Promise.all(tasks);

  const settings = (await results[0].json()) as Settings;
  const enterpriseSettings =
    tasks.length > 1 ? ((await results[1].json()) as EnterpriseSettings) : null;
  const customAnalyticsScript = (
    tasks.length > 2 ? await results[2].json() : null
  ) as string | null;

  const combinedSettings: CombinedSettings = {
    settings,
    enterpriseSettings,
    customAnalyticsScript,
  };

  return combinedSettings;
}

export interface CombinedSettings {
  settings: Settings;
  enterpriseSettings: EnterpriseSettings | null;
  customAnalyticsScript: string | null;
}

export async function getCombinedSettings(_opts?: {
  forceRetrieval?: boolean;
}): Promise<CombinedSettings> {
  // Always fetch fresh. A module-level cache here is shared across ALL requests
  // in the Next server process, so it serves STALE settings to every user until
  // the pod restarts — it hid a freshly-set routing rule and made settings
  // toggles look like they reverted. fetchSettingsSS is `no-store`, so the
  // per-request cost is one cheap internal call. See AGENTS.md "### 13".
  return await fetchSettingsSS();
}
