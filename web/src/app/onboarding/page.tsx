import { Suspense } from "react";
import { redirect } from "next/navigation";
import { getAuthTypeMetadataSS, getCurrentUserSS } from "@/lib/userSS";
import { Header } from "@/components/header/Header";
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

  // Render the standard app header so users get the logo (back to chat) and the
  // user menu — which, for admins, includes the Admin Panel link + theme toggle.
  return (
    <div className="h-screen overflow-y-auto bg-background">
      <Header user={user} />
      {/* Suspense: OnboardingForm reads useSearchParams (?view=requests deep
          link), which the prod build requires be wrapped. */}
      <Suspense fallback={null}>
        <OnboardingForm />
      </Suspense>
    </div>
  );
}
