import { countSourceStatuses, SourceStatus } from "@/lib/onboarding/interfaces";

// One-line rollup of a request's per-source scrape status, e.g.
// "4/6 succeeded · 1 indexing · 1 failed". Shown above the per-source rows so
// admins/requesters see overall progress at a glance instead of scanning each
// row (and so an in-flight request doesn't read as simply "failed").
export function SourceStatusSummary({ sources }: { sources: SourceStatus[] }) {
  if (!sources.length) return null;
  const c = countSourceStatuses(sources);
  return (
    <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
      <span className="font-semibold text-default">
        {c.succeeded}/{c.total} succeeded
      </span>
      {c.indexing > 0 && (
        <span className="text-accent">· {c.indexing} indexing</span>
      )}
      {c.pending > 0 && (
        <span className="text-subtle">· {c.pending} pending</span>
      )}
      {c.failed > 0 && <span className="text-error">· {c.failed} failed</span>}
    </div>
  );
}
