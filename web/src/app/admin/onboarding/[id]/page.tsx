"use client";

import { Suspense, useEffect, useState } from "react";
import { OnboardingForm } from "@/app/onboarding/OnboardingForm";
import { OnboardingRequestSnapshot } from "@/lib/onboarding/interfaces";

// Admin opens a pending request in the shared onboarding form (prefilled) to fix
// gaps and approve. Gated by the /admin layout (admin-only).
export default function Page({ params }: { params: { id: string } }) {
  const [request, setRequest] = useState<OnboardingRequestSnapshot | null>(
    null
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/admin/onboarding/${params.id}`)
      .then(async (r) => {
        if (!r.ok) {
          setError(`Couldn't load request (${r.status}).`);
          return;
        }
        setRequest((await r.json()) as OnboardingRequestSnapshot);
      })
      .catch(() => setError("Couldn't load request."));
  }, [params.id]);

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 text-sm text-error">
        {error}
      </div>
    );
  }
  if (!request) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10 text-sm text-subtle">
        Loading…
      </div>
    );
  }
  return (
    // Suspense: OnboardingForm reads useSearchParams; the admin layout doesn't
    // wrap children in one, so provide it here for the prod build.
    <Suspense fallback={null}>
      <OnboardingForm initialRequest={request} />
    </Suspense>
  );
}
