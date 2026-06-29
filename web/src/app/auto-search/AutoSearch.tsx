"use client";

import { useContext, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FiSearch, FiThumbsUp, FiThumbsDown } from "react-icons/fi";
import { SettingsContext } from "@/components/settings/SettingsProvider";

// Mirrors the backend AutoSearchResponse shape.
interface AnsweredBy {
  persona_id: number;
  name: string;
  display_name: string | null;
  routed: boolean;
  confidence: number;
}
interface AutoSearchDoc {
  semantic_identifier: string;
  link: string | null;
}
interface AutoSearchResponse {
  answer: string | null;
  docs: { top_documents: AutoSearchDoc[] } | null;
  chat_message_id: number | null;
  answered_by: AnsweredBy;
  error_msg: string | null;
}

function isVisible(rollout: string | undefined, userRole: string | null): boolean {
  // Mirrors the backend gate; the endpoint enforces it for real.
  if (rollout === "everyone") return true;
  if (rollout === "admin_only") return userRole === "admin";
  return false; // "off" or unset-as-off
}

export function AutoSearch({ userRole }: { userRole: string | null }) {
  const settings = useContext(SettingsContext)?.settings;
  const rollout = settings?.auto_search_rollout ?? "admin_only";

  const [question, setQuestion] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<AutoSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // chat_message_id -> the feedback already submitted for it (one per answer).
  const [feedbackGiven, setFeedbackGiven] = useState<"like" | "dislike" | null>(
    null
  );
  const [feedbackText, setFeedbackText] = useState("");
  const [showFeedbackBox, setShowFeedbackBox] = useState(false);

  if (!isVisible(rollout, userRole)) {
    return (
      <div className="flex h-full items-center justify-center text-subtle">
        Search is not enabled for your account.
      </div>
    );
  }

  async function runSearch() {
    const trimmed = question.trim();
    if (!trimmed || isLoading) return;
    setIsLoading(true);
    setError(null);
    setResult(null);
    setFeedbackGiven(null);
    setShowFeedbackBox(false);
    setFeedbackText("");
    try {
      const response = await fetch("/api/query/auto-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null))?.detail;
        setError(detail || `Search failed (${response.status}).`);
        return;
      }
      setResult((await response.json()) as AutoSearchResponse);
    } catch (e) {
      setError("Something went wrong running the search.");
    } finally {
      setIsLoading(false);
    }
  }

  async function submitFeedback(
    vote: "like" | "dislike",
    text: string | null
  ) {
    if (result?.chat_message_id == null) return;
    setFeedbackGiven(vote);
    setShowFeedbackBox(false);
    try {
      await fetch("/api/chat/create-chat-message-feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_message_id: result.chat_message_id,
          is_positive: vote === "like",
          feedback_text: text || null,
        }),
      });
    } catch {
      // Feedback is best-effort; don't disrupt the user if it fails.
    }
  }

  const answeredBy = result?.answered_by;
  const answeredByLabel =
    answeredBy &&
    (answeredBy.display_name?.trim() ? answeredBy.display_name : answeredBy.name);
  const topDocs = result?.docs?.top_documents ?? [];

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold mb-1">Search</h1>
      <p className="text-subtle text-sm mb-6">
        Ask a question — the right assistant is chosen for you automatically.
      </p>

      <div className="flex items-center gap-2 border border-border-medium rounded-xl bg-background-weak px-4 py-3 shadow-sm focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/30">
        <FiSearch className="text-subtle shrink-0" size={18} />
        <input
          autoFocus
          className="w-full bg-transparent outline-none text-base placeholder-subtle"
          placeholder="Ask anything…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              runSearch();
            }
          }}
        />
        <button
          onClick={runSearch}
          disabled={!question.trim() || isLoading}
          className="shrink-0 rounded-lg bg-accent text-white px-3 py-1.5 text-sm disabled:opacity-40 hover:bg-accent-hover"
        >
          {isLoading ? "Searching…" : "Search"}
        </button>
      </div>

      {error && (
        <div className="mt-6 rounded-lg border border-red-300 bg-red-50 dark:bg-red-950/30 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      {result && !error && (
        <div className="mt-8">
          {answeredByLabel && (
            <div className="mb-3 flex items-center gap-2 text-xs text-subtle">
              <span className="rounded-full bg-background-strong px-2.5 py-1">
                Answered by <b>{answeredByLabel}</b>
                {!answeredBy?.routed && " (searched all sources)"}
              </span>
            </div>
          )}

          <div className="prose dark:prose-invert max-w-none text-base">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {result.answer || result.error_msg || "No answer was generated."}
            </ReactMarkdown>
          </div>

          {topDocs.length > 0 && (
            <div className="mt-6">
              <div className="text-xs font-semibold text-subtle mb-2">
                Sources
              </div>
              <ul className="space-y-1">
                {topDocs.slice(0, 8).map((doc, i) => (
                  <li key={i} className="text-sm">
                    {doc.link ? (
                      <a
                        href={doc.link}
                        target="_blank"
                        rel="noreferrer"
                        className="text-link hover:underline"
                      >
                        {doc.semantic_identifier || doc.link}
                      </a>
                    ) : (
                      <span>{doc.semantic_identifier}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.chat_message_id != null && (
            <div className="mt-6 flex items-center gap-3">
              <button
                title="Helpful"
                onClick={() => submitFeedback("like", null)}
                className={`rounded p-1.5 hover:bg-hover ${
                  feedbackGiven === "like" ? "text-green-600" : "text-subtle"
                }`}
              >
                <FiThumbsUp size={16} />
              </button>
              <button
                title="Not helpful"
                onClick={() => setShowFeedbackBox((v) => !v)}
                className={`rounded p-1.5 hover:bg-hover ${
                  feedbackGiven === "dislike" ? "text-red-600" : "text-subtle"
                }`}
              >
                <FiThumbsDown size={16} />
              </button>
              {feedbackGiven && !showFeedbackBox && (
                <span className="text-xs text-subtle">Thanks for the feedback!</span>
              )}
            </div>
          )}

          {showFeedbackBox && (
            <div className="mt-3 flex flex-col gap-2">
              <textarea
                className="w-full rounded-lg border border-border-medium bg-background-weak p-2 text-sm outline-none"
                rows={2}
                placeholder="What was wrong with this answer? (optional)"
                value={feedbackText}
                onChange={(e) => setFeedbackText(e.target.value)}
              />
              <button
                onClick={() => submitFeedback("dislike", feedbackText)}
                className="self-start rounded-lg bg-accent text-white px-3 py-1.5 text-sm hover:bg-accent-hover"
              >
                Submit feedback
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
