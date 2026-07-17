"use client";

import { Suspense, useEffect, useState } from "react";
import { OnboardingForm } from "@/app/onboarding/OnboardingForm";
import { OnboardingRequestSnapshot } from "@/lib/onboarding/interfaces";

// The requester opens their own still-pending request in the shared onboarding
// form (prefilled) to fix details before an admin reviews it. The backend PATCH
// enforces owner-or-admin + pending-only; SSO is enforced by the /onboarding
// page above and the API. Ownership scoping is server-side, so a non-owner just
// gets a 403 on load here.
export default function Page({ params }: { params: { id: string } }) {
  const [request, setRequest] = useState<OnboardingRequestSnapshot | null>(
    null
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/onboarding/${params.id}`)
      .then(async (r) => {
        if (!r.ok) {
          setError(
            r.status === 403 || r.status === 404
              ? "This request can't be edited (it may no longer be pending)."
              : `Couldn't load request (${r.status}).`
          );
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
    // Suspense: OnboardingForm reads useSearchParams; provide the boundary the
    // prod build requires.
    <Suspense fallback={null}>
      <OnboardingForm initialRequest={request} editMode="requester" />
    </Suspense>
  );
}
