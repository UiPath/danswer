import { unstable_noStore as noStore } from "next/cache";
import { getCurrentUserSS } from "@/lib/userSS";
import { User } from "@/lib/types";
import { AutoSearch } from "./AutoSearch";

// Auto-routed Search tab. Auth is enforced by the app layout / middleware; here
// we just resolve the current user's role so the client can gate visibility
// (the /query/auto-search endpoint enforces the rollout independently). Visibility
// is read from the auto_search_rollout setting via SettingsContext on the client.
export default async function Page() {
  noStore();
  let user: User | null = null;
  try {
    user = await getCurrentUserSS();
  } catch {
    user = null;
  }
  return <AutoSearch userRole={user?.role ?? null} />;
}
