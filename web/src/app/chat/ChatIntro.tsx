import { Logo } from "@/components/Logo";

// Minimal chat landing: a centered logo + greeting that sits directly above the
// input on the empty state (the Assistant/Filters/Model, Knowledge Sets, and
// Connected Sources tiles were intentionally removed for a cleaner, wider,
// reference-style landing).
export function ChatIntro() {
  return (
    <div className="w-full px-4 mb-6 text-center da-fade-up">
      <Logo height={60} width={60} className="mx-auto mb-4" />
      <h1 className="font-display text-3xl sm:text-4xl font-semibold tracking-tight text-strong">
        Start your day with Darwin
      </h1>
    </div>
  );
}
