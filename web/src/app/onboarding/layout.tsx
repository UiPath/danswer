import { redirect } from "next/navigation";
import { getAuthTypeMetadataSS, getCurrentUserSS } from "@/lib/userSS";
import { Header } from "@/components/header/Header";

// Shared chrome + SSO gate for every /onboarding route (the submit/requests
// form and the requester edit sub-route). Mirrors the rest of the app: signed-
// out users go to login rather than seeing a form whose every action 401s. The
// backend endpoints enforce auth regardless — this is the matching frontend
// gate + defense-in-depth. Rendering the standard Header here gives users the
// logo (back to chat) and user menu (Admin Panel + theme toggle for admins).
export default async function OnboardingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
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

  return (
    <div className="h-screen overflow-y-auto bg-background">
      <Header user={user} />
      {children}
    </div>
  );
}
