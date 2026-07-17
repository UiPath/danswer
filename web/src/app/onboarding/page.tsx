import { Suspense } from "react";
import { OnboardingForm } from "./OnboardingForm";

// SSO gate + app header live in ./layout.tsx (shared with the edit sub-route).
export default function Page() {
  // Suspense: OnboardingForm reads useSearchParams (?view=requests deep link),
  // which the prod build requires be wrapped.
  return (
    <Suspense fallback={null}>
      <OnboardingForm />
    </Suspense>
  );
}
