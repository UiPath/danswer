// Minimal chat landing: a centered greeting that sits directly above the input
// on the empty state (the logo and the Assistant/Filters/Model, Knowledge Sets,
// and Connected Sources tiles were intentionally removed for a cleaner, wider,
// reference-style landing).
export function ChatIntro() {
  return (
    <div className="w-full px-4 mb-6 text-center da-fade-up">
      <h1 className="font-sans text-lg sm:text-xl font-semibold tracking-tight text-strong">
        Search your company&apos;s internal knowledge base
      </h1>
    </div>
  );
}
