import { redirect } from "next/navigation";
import { getAuthTypeMetadataSS, getCurrentUserSS } from "@/lib/userSS";
import { OnboardingForm } from "./OnboardingForm";

// Require an authenticated (SSO) session, mirroring the rest of the app: send
// signed-out users to the login flow rather than rendering a form whose every
// action would 401. The backend endpoints enforce auth regardless — this is the
// matching frontend gate + defense-in-depth.
export default async function Page() {
  const [authTypeMetadata, user] = await Promise.all([
    getAuthTypeMetadataSS(),
    getCurrentUserSS(),
  ]);

  const authDisabled = authTypeMetadata?.authType === "disabled";
  if (!authDisabled && !user) {
    return redirect("/auth/login?next=/onboarding");
  }
  if (user && !user.is_verified && authTypeMetadata?.requiresVerification) {
    return redirect("/auth/waiting-on-verification");
  }

  return <OnboardingForm />;
}
