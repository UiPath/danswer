"use client";

import { useContext, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FiSend, FiThumbsUp, FiThumbsDown } from "react-icons/fi";
import Link from "next/link";
import { SettingsContext } from "@/components/settings/SettingsProvider";
import { useChatContext } from "@/components/context/ChatContext";
import { Logo } from "@/components/Logo";
import { HeaderTitle } from "@/components/header/Header";
import { UserDropdown } from "@/components/UserDropdown";
import { Persona } from "@/app/admin/assistants/interfaces";
import { assistantDisplayName } from "@/lib/assistants/displayName";
import { orderAssistantsForUser } from "@/lib/assistants/orderAssistants";
import {
  getMentionQuery,
  filterAssistantsByMention,
  applyMentionLabel,
  stripMentionLabel,
  hasMentionLabel,
} from "@/lib/assistants/mentions";

// Matches ChatInputBar's auto-grow cap so the Search box feels like the chat box.
const MAX_INPUT_HEIGHT = 200;
const RECENTS_KEY = "autoSearchRecents";
const RECENTS_LIMIT = 8;
// Playful rotating status words shown while a search runs (Claude-style).
const LOADING_PHRASES = [
  "Searching",
  "Brewing",
  "Routing",
  "Digging",
  "Pondering",
  "Assembling",
  "Connecting the dots",
  "Almost there",
];
const LOADING_PHRASE_INTERVAL_MS = 1800;
// Typewriter timings for the empty search box's example placeholder.
const TYPE_MS = 45; // per character while typing
const HOLD_MS = 1800; // pause once a full example is typed, before the next
// Example prompts that teach the "@assistant" convention, cycled through the empty
// search box's placeholder. Each is bound to a real assistant by a name fragment
// (case/space-insensitive); unmatched ones are dropped so we never show an
// assistant the user doesn't have.
const EXAMPLE_PROMPTS: { match: string; question: string }[] = [
  { match: "integration", question: "why is my Salesforce connection failing?" },
  { match: "ownership", question: "who is the TAM or CSM of the ABC account?" },
  { match: "action center", question: "how do I reassign a task to someone else?" },
  { match: "orchestrator", question: "how do I schedule a process to run hourly?" },
  { match: "automation suite", question: "how do I back up my cluster?" },
];

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
interface SearchedAssistant {
  persona_id: number;
  name: string;
  display_name: string | null;
}
interface AutoSearchResponse {
  answer: string | null;
  docs: { top_documents: AutoSearchDoc[] } | null;
  chat_message_id: number | null;
  answered_by: AnsweredBy;
  // Next-best assistants (router ranks 2..N) shown as clickable "Recommended
  // assistants" chips: click one to chat further with it if #1 wasn't right.
  other_recommended: SearchedAssistant[];
  error_msg: string | null;
  // Compare: when compare_enabled, the UI shows two tabs — the top-1 answer (here)
  // and a second answer over the union of union_assistants' document sets, which is
  // lazy-fetched from /query/auto-search/union so it never blocks this answer.
  compare_enabled: boolean;
  union_assistants: SearchedAssistant[];
  // Third tab (rides along with compare): an answer scoped to HighSpot + the docs
  // sites, lazy-fetched from /query/auto-search/sources.
  source_tab_enabled: boolean;
}
// The lazily-fetched second/third answer (union of doc sets, or the source-scoped
// answer). Same shape for both.
interface AutoSearchUnionResponse {
  answer: string | null;
  docs: { top_documents: AutoSearchDoc[] } | null;
  error_msg: string | null;
}

export function autoSearchVisible(
  rollout: string | undefined,
  userRole: string | null
): boolean {
  // Mirrors the backend gate (_auto_search_allowed); the endpoint enforces it
  // for real. OFF hides for everyone; EVERYONE shows to all; ADMIN_ONLY shows to
  // admins AND the no-auth/superuser context (userRole === null), matching the
  // backend's "user is None => treat as admin" convention so local dev
  // (AUTH_TYPE=disabled) isn't blocked.
  if (rollout === "off") return false;
  if (rollout === "everyone") return true;
  // admin_only
  if (userRole === null) return true;
  return userRole === "admin";
}

export function AutoSearch({ userRole }: { userRole: string | null }) {
  const settings = useContext(SettingsContext)?.settings;
  const rollout = settings?.auto_search_rollout ?? "admin_only";
  const { user, availablePersonas } = useChatContext();
  // Same accessible/ordered assistant set the chat picker uses.
  const assistants = orderAssistantsForUser(availablePersonas, user);

  const [question, setQuestion] = useState("");
  const textAreaRef = useRef<HTMLTextAreaElement>(null);
  // Explicit @mention pick: when set (and its "@<label>" annotation is still in
  // the box), the query is sent straight to this assistant and the LLM router is
  // skipped. No pinned chip — the annotation reads inline as text.
  const [forcedPersona, setForcedPersona] = useState<Persona | null>(null);
  const [showMentions, setShowMentions] = useState(false);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<AutoSearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Compare view: which tab is showing, and the lazily-fetched union answer.
  const [activeTab, setActiveTab] = useState<"top" | "union" | "sources">("top");
  const [unionResult, setUnionResult] = useState<AutoSearchUnionResponse | null>(
    null
  );
  const [unionLoading, setUnionLoading] = useState(false);
  const [unionError, setUnionError] = useState<string | null>(null);
  // Third (source-scoped: HighSpot + docs) compare answer, also lazy-fetched.
  const [sourceResult, setSourceResult] =
    useState<AutoSearchUnionResponse | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  // chat_message_id -> the feedback already submitted for it (one per answer).
  const [feedbackGiven, setFeedbackGiven] = useState<"like" | "dislike" | null>(
    null
  );
  const [feedbackText, setFeedbackText] = useState("");
  const [showFeedbackBox, setShowFeedbackBox] = useState(false);
  // Recent searches — kept client-side (localStorage), point-in-time history.
  const [recents, setRecents] = useState<string[]>([]);
  const [loadingPhraseIdx, setLoadingPhraseIdx] = useState(0);
  // Typewriter state for the rotating "@assistant …" example placeholder.
  const [phraseIdx, setPhraseIdx] = useState(0);
  const [charCount, setCharCount] = useState(0);

  // Build the rotating placeholder examples from real, accessible assistants.
  // Match on a name fragment ignoring case + non-letters, so "action center"
  // resolves the camelCase persona "ActionCenter".
  const examplePlaceholders = EXAMPLE_PROMPTS.map((e) => {
    const target = e.match.toLowerCase().replace(/[^a-z]/g, "");
    const persona = assistants.find(
      (a) =>
        a.name.toLowerCase().replace(/[^a-z]/g, "").includes(target) ||
        assistantDisplayName(a).toLowerCase().replace(/[^a-z]/g, "").includes(target)
    );
    return persona
      ? `@${assistantDisplayName(persona)} ${e.question}`
      : null;
  }).filter((x): x is string => x !== null);

  // Auto-grow the textarea like ChatInputBar.
  useEffect(() => {
    const ta = textAreaRef.current;
    if (ta) {
      ta.style.height = "0px";
      ta.style.height = `${Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT)}px`;
    }
  }, [question]);

  // Load recent searches once on mount.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(RECENTS_KEY);
      if (raw) setRecents(JSON.parse(raw));
    } catch {
      // ignore malformed/unavailable storage
    }
  }, []);

  // Rotate the playful status word while a search is running.
  useEffect(() => {
    if (!isLoading) {
      setLoadingPhraseIdx(0);
      return;
    }
    const id = setInterval(
      () => setLoadingPhraseIdx((i) => (i + 1) % LOADING_PHRASES.length),
      LOADING_PHRASE_INTERVAL_MS
    );
    return () => clearInterval(id);
  }, [isLoading]);

  // Typewriter: type the current example out character-by-character, hold, then
  // advance to the next — so users discover the "@assistant" convention. Runs only
  // while the box is empty; once the user types, the overlay is hidden.
  useEffect(() => {
    if (question.trim() !== "" || examplePlaceholders.length === 0) return;
    const current = examplePlaceholders[phraseIdx % examplePlaceholders.length];
    if (charCount < current.length) {
      const id = setTimeout(() => setCharCount((c) => c + 1), TYPE_MS);
      return () => clearTimeout(id);
    }
    const id = setTimeout(() => {
      setCharCount(0);
      setPhraseIdx((i) => (i + 1) % examplePlaceholders.length);
    }, HOLD_MS);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question, phraseIdx, charCount, examplePlaceholders.length]);

  const typedPlaceholder =
    examplePlaceholders.length > 0
      ? examplePlaceholders[phraseIdx % examplePlaceholders.length].slice(
          0,
          charCount
        )
      : "";

  function pushRecent(q: string) {
    setRecents((prev) => {
      const next = [q, ...prev.filter((x) => x !== q)].slice(0, RECENTS_LIMIT);
      try {
        localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
      } catch {
        // ignore storage write failures
      }
      return next;
    });
  }

  if (!autoSearchVisible(rollout, userRole)) {
    return (
      <div className="flex h-full items-center justify-center text-subtle">
        Search is not enabled for your account.
      </div>
    );
  }

  // Active @mention typeahead state (derived).
  const mentionQuery = getMentionQuery(question);
  const mentionMatches =
    mentionQuery !== null ? filterAssistantsByMention(assistants, mentionQuery) : [];

  function handleQuestionChange(text: string) {
    setQuestion(text);
    setShowMentions(getMentionQuery(text) !== null);
    setMentionIndex(0);
    // Drop the explicit pick if its inline "@<label>" annotation is gone.
    if (
      forcedPersona &&
      !hasMentionLabel(text, assistantDisplayName(forcedPersona))
    ) {
      setForcedPersona(null);
    }
  }

  function selectMention(persona: Persona) {
    const label = assistantDisplayName(persona);
    setQuestion((prev) => applyMentionLabel(prev, label));
    setForcedPersona(persona);
    setShowMentions(false);
    setMentionIndex(0);
    textAreaRef.current?.focus();
  }


  async function runSearch(override?: string, explicitPersonaId?: number) {
    const rawText = override ?? question;
    // Explicit @mention pick (only honored when its annotation is still present
    // and this isn't a recent-search re-run or a "try another assistant" retry)
    // => skip routing, invoke directly.
    let personaId: number | null = explicitPersonaId ?? null;
    let toSearch = rawText;
    if (
      explicitPersonaId === undefined &&
      override === undefined &&
      forcedPersona &&
      hasMentionLabel(rawText, assistantDisplayName(forcedPersona))
    ) {
      personaId = forcedPersona.id;
      toSearch = stripMentionLabel(rawText, assistantDisplayName(forcedPersona));
    }
    const trimmed = toSearch.trim();
    if (!trimmed || isLoading) return;
    if (override !== undefined) setQuestion(override);
    setIsLoading(true);
    setError(null);
    setResult(null);
    setFeedbackGiven(null);
    setShowFeedbackBox(false);
    setFeedbackText("");
    setShowMentions(false);
    // Reset compare state for the new query.
    setActiveTab("top");
    setUnionResult(null);
    setUnionError(null);
    setUnionLoading(false);
    setSourceResult(null);
    setSourceError(null);
    setSourceLoading(false);
    try {
      const response = await fetch("/api/query/auto-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed, persona_id: personaId }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null))?.detail;
        setError(detail || `Search failed (${response.status}).`);
        return;
      }
      const data = (await response.json()) as AutoSearchResponse;
      setResult(data);
      pushRecent(trimmed);
      setForcedPersona(null); // one-shot: don't carry the pick to the next query
      // Lazily fetch the union (compare) answer AFTER the primary answer is shown,
      // so the two never block each other. The top-1 answer is already on screen.
      if (data.compare_enabled && data.union_assistants.length > 0) {
        void fetchUnionAnswer(
          trimmed,
          data.union_assistants.map((a) => a.persona_id)
        );
      }
      // Third tab (HighSpot + docs sites) — also lazy, in parallel with the union.
      if (data.compare_enabled && data.source_tab_enabled) {
        void fetchSourceAnswer(trimmed);
      }
    } catch {
      setError("Something went wrong running the search.");
    } finally {
      setIsLoading(false);
    }
  }

  async function fetchUnionAnswer(message: string, personaIds: number[]) {
    setUnionLoading(true);
    setUnionError(null);
    try {
      const response = await fetch("/api/query/auto-search/union", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, persona_ids: personaIds }),
      });
      if (!response.ok) {
        setUnionError(`Compare answer failed (${response.status}).`);
        return;
      }
      setUnionResult((await response.json()) as AutoSearchUnionResponse);
    } catch {
      setUnionError("Something went wrong generating the compare answer.");
    } finally {
      setUnionLoading(false);
    }
  }

  async function fetchSourceAnswer(message: string) {
    setSourceLoading(true);
    setSourceError(null);
    try {
      const response = await fetch("/api/query/auto-search/sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (!response.ok) {
        setSourceError(`Compare answer failed (${response.status}).`);
        return;
      }
      setSourceResult((await response.json()) as AutoSearchUnionResponse);
    } catch {
      setSourceError("Something went wrong generating the compare answer.");
    } finally {
      setSourceLoading(false);
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
  const otherRecommended = result?.other_recommended ?? [];
  const topDocs = result?.docs?.top_documents ?? [];
  // Compare state. compareOn just means "show tabs" — the union answer may still
  // be loading (it's fetched lazily), so the union tab shows its own progress.
  const compareOn = !!result?.compare_enabled;
  const unionAssistants = result?.union_assistants ?? [];
  const unionAnswer = unionResult?.answer ?? null;
  const unionDocs = unionResult?.docs?.top_documents ?? [];
  const unionDisplayError = unionError || unionResult?.error_msg || null;
  // Third tab: HighSpot + docs sites (source-scoped).
  const sourceTabOn = !!result?.source_tab_enabled;
  const sourceAnswer = sourceResult?.answer ?? null;
  const sourceDocs = sourceResult?.docs?.top_documents ?? [];
  const sourceDisplayError = sourceError || sourceResult?.error_msg || null;
  const assistantLabel = (a: SearchedAssistant) =>
    a.display_name?.trim() ? a.display_name : a.name;
  // "A", "A and B", "A, B and C"
  const formatAssistantList = (names: string[]) =>
    names.length <= 1
      ? names.join("")
      : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

  const sourcesBlock = (docs: AutoSearchDoc[]) =>
    docs.length > 0 ? (
      <div className="mt-7 border-t border-border-medium pt-5">
        <div className="text-xs font-semibold uppercase tracking-wide text-subtle mb-3">
          Sources
        </div>
        <ul className="flex flex-col gap-1.5">
          {docs.slice(0, 8).map((doc, i) => (
            <li key={i} className="flex items-baseline gap-2 text-sm">
              <span className="text-subtle tabular-nums w-4 shrink-0">{i + 1}</span>
              {doc.link ? (
                <a
                  href={doc.link}
                  target="_blank"
                  rel="noreferrer"
                  className="text-link hover:underline line-clamp-1"
                >
                  {doc.semantic_identifier || doc.link}
                </a>
              ) : (
                <span className="line-clamp-1">{doc.semantic_identifier}</span>
              )}
            </li>
          ))}
        </ul>
      </div>
    ) : null;

  const answerBody = (text: string | null) => (
    <div className="prose dark:prose-invert max-w-none text-base leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {text || result?.error_msg || "No answer was generated."}
      </ReactMarkdown>
    </div>
  );

  const hasActivity = isLoading || !!result || !!error;

  // The search composer (mention dropdown + chat-style box). Shared between the
  // centered empty state and the top-anchored active state.
  const composer = (
    <div className="relative">
      {showMentions && mentionMatches.length > 0 && (
        <div className="absolute bottom-full left-0 right-0 mb-2 z-20 max-h-64 overflow-y-auto rounded-xl border border-border-medium bg-background shadow-xl py-1.5 px-1.5">
          {mentionMatches.map((persona, index) => (
            <button
              key={persona.id}
              onClick={() => selectMention(persona)}
              onMouseEnter={() => setMentionIndex(index)}
              className={`w-full text-left flex gap-x-2 items-baseline rounded-lg px-2.5 py-2 ${
                mentionIndex === index ? "bg-hover" : "hover:bg-hover"
              }`}
            >
              <span className="font-semibold shrink-0">
                {assistantDisplayName(persona)}
              </span>
              <span className="line-clamp-1 text-sm text-subtle">
                {persona.description}
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="relative flex flex-col w-full rounded-2xl border border-border-medium bg-background-weak shadow-lg shadow-black/5 dark:shadow-black/30 transition focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/30 overflow-hidden">
        {/* Typewriter example overlay — only while the box is empty. Aligned to the
            textarea's text (same padding/size) and click-through so the box stays
            usable. A trailing caret fades to read as a live cursor. */}
        {question === "" && examplePlaceholders.length > 0 && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute left-0 top-0 pl-5 pr-14 py-4 text-base text-subtle whitespace-pre-wrap break-word"
          >
            {typedPlaceholder}
            <span className="animate-pulse text-accent">▏</span>
          </div>
        )}
        <textarea
          ref={textAreaRef}
          autoFocus
          role="textarea"
          aria-multiline
          aria-label="Ask a question"
          className={`m-0 w-full resize-none border-0 bg-transparent outline-none placeholder-subtle whitespace-normal break-word overscroll-contain pl-5 pr-14 py-4 text-base min-h-[64px] ${
            textAreaRef.current &&
            textAreaRef.current.scrollHeight > MAX_INPUT_HEIGHT
              ? "overflow-y-auto"
              : "overflow-hidden"
          }`}
          style={{ scrollbarWidth: "thin" }}
          // The animated example lives in the overlay below; only fall back to a
          // static native placeholder when there are no examples to type.
          placeholder={examplePlaceholders.length > 0 ? "" : "Ask anything…"}
          value={question}
          onChange={(e) => handleQuestionChange(e.target.value)}
          onKeyDown={(e) => {
            if (showMentions && mentionMatches.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setMentionIndex((i) =>
                  Math.min(i + 1, mentionMatches.length - 1)
                );
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setMentionIndex((i) => Math.max(i - 1, 0));
                return;
              }
              if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                selectMention(mentionMatches[mentionIndex] ?? mentionMatches[0]);
                return;
              }
              if (e.key === "Escape") {
                setShowMentions(false);
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              runSearch();
            }
          }}
        />
        <button
          onClick={() => runSearch()}
          disabled={!question.trim() || isLoading}
          aria-label="Search"
          title={isLoading ? "Searching…" : "Search"}
          className="absolute bottom-2.5 right-2.5"
        >
          <FiSend
            size={18}
            className={`w-9 h-9 p-2 rounded-xl transition-colors ${
              question.trim() && !isLoading
                ? "bg-accent text-white hover:bg-accent-hover"
                : "text-subtle opacity-40"
            }`}
          />
        </button>
      </div>
    </div>
  );

  // Recent searches as quiet chips (only when there are any).
  const recentChips = recents.length > 0 && (
    <div className="mt-4 flex flex-wrap items-center gap-2">
      <span className="text-xs uppercase tracking-wide text-subtle mr-1">
        Recent
      </span>
      {recents.slice(0, 6).map((q, i) => (
        <button
          key={i}
          onClick={() => runSearch(q)}
          title={q}
          className="max-w-[15rem] truncate rounded-full border border-border-medium bg-background-weak px-3 py-1 text-sm text-default hover:bg-hover transition-colors"
        >
          {q}
        </button>
      ))}
    </div>
  );

  return (
    <div className="relative h-full w-full overflow-y-auto bg-background">
      {/* Brand mark, top-left. */}
      <Link
        href="/chat"
        className="absolute top-4 left-5 z-30 flex items-center"
        title="Back to chat"
      >
        <Logo height={28} width={26} className="mr-1.5 my-auto" />
        <HeaderTitle>Darwin</HeaderTitle>
      </Link>

      {/* Logged-in user menu, top-right. */}
      <div className="absolute top-3 right-4 z-30">
        <UserDropdown user={user} />
      </div>

      <div
        className={`relative mx-auto flex min-h-full w-full max-w-2xl flex-col px-5 ${
          hasActivity ? "pt-24 pb-16" : "justify-center pb-28"
        }`}
      >
        {/* Empty state: centered hero with a single restrained accent glow. */}
        {!hasActivity && (
          <>
            <div
              aria-hidden="true"
              className="pointer-events-none absolute left-1/2 top-1/2 -z-10 h-44 w-2/3 -translate-x-1/2 -translate-y-[140%] rounded-full bg-accent opacity-20 blur-[90px]"
            />
            <div className="mb-8 text-center">
              <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight text-default">
                What do you want to know?
              </h1>
              <p className="mt-3 text-subtle">
                Ask a question and the right assistant answers it. Type{" "}
                <span className="font-medium text-default">@</span> to pick one
                yourself.
              </p>
            </div>
          </>
        )}

        {composer}
        {recentChips}

        {isLoading && (
          <div className="mt-10 flex items-center gap-3 text-subtle">
            <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-subtle border-t-transparent" />
            <span className="text-sm">{LOADING_PHRASES[loadingPhraseIdx]}…</span>
          </div>
        )}

        {error && !isLoading && (
          <div className="mt-6 rounded-xl border border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/30 px-4 py-3 text-sm text-red-700 dark:text-red-300">
            {error}
          </div>
        )}

        {result && !error && !isLoading && (
          <div className="mt-8">
            {compareOn ? (
              // Two answers in tabs, each full width. The union tab lazy-loads so
              // it never blocks the top-1 answer already shown here.
              <div>
                <div
                  role="tablist"
                  className="mb-5 flex gap-6 border-b border-border-medium"
                >
                  <button
                    role="tab"
                    aria-selected={activeTab === "top"}
                    onClick={() => setActiveTab("top")}
                    className={`-mb-px border-b-2 pb-2.5 text-sm font-medium transition-colors ${
                      activeTab === "top"
                        ? "border-accent text-default"
                        : "border-transparent text-subtle hover:text-default"
                    }`}
                  >
                    Top match
                  </button>
                  <button
                    role="tab"
                    aria-selected={activeTab === "union"}
                    onClick={() => setActiveTab("union")}
                    className={`-mb-px flex items-center gap-2 border-b-2 pb-2.5 text-sm font-medium transition-colors ${
                      activeTab === "union"
                        ? "border-accent text-default"
                        : "border-transparent text-subtle hover:text-default"
                    }`}
                  >
                    All top matches
                    {unionLoading && (
                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-border-medium border-t-accent" />
                    )}
                  </button>
                  {sourceTabOn && (
                    <button
                      role="tab"
                      aria-selected={activeTab === "sources"}
                      onClick={() => setActiveTab("sources")}
                      className={`-mb-px flex items-center gap-2 border-b-2 pb-2.5 text-sm font-medium transition-colors ${
                        activeTab === "sources"
                          ? "border-accent text-default"
                          : "border-transparent text-subtle hover:text-default"
                      }`}
                    >
                      HighSpot &amp; Docs
                      {sourceLoading && (
                        <span className="h-3 w-3 animate-spin rounded-full border-2 border-border-medium border-t-accent" />
                      )}
                    </button>
                  )}
                </div>

                {activeTab === "top" ? (
                  <div>
                    {answeredByLabel && (
                      <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-border-medium bg-background-weak px-3 py-1 text-xs text-subtle">
                        <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                        Answered by{" "}
                        <span className="font-semibold text-default">
                          {answeredByLabel}
                        </span>
                        {!answeredBy?.routed && <span>· all sources</span>}
                      </div>
                    )}
                    {answerBody(result.answer)}
                    {sourcesBlock(topDocs)}
                  </div>
                ) : activeTab === "union" ? (
                  <div>
                    {unionAssistants.length > 0 && (
                      <div className="mb-3 text-xs text-subtle">
                        Combined from{" "}
                        <span className="text-default">
                          {formatAssistantList(
                            unionAssistants.map(assistantLabel)
                          )}
                        </span>
                      </div>
                    )}
                    {unionLoading ? (
                      <div className="flex items-center gap-3 py-8 text-sm text-subtle">
                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-border-medium border-t-accent" />
                        Generating a combined answer across{" "}
                        {unionAssistants.length} assistants…
                      </div>
                    ) : unionDisplayError ? (
                      <div className="py-4 text-sm text-error">
                        {unionDisplayError}
                      </div>
                    ) : unionAnswer ? (
                      <>
                        {answerBody(unionAnswer)}
                        {sourcesBlock(unionDocs)}
                      </>
                    ) : (
                      <div className="py-4 text-sm text-subtle">
                        No combined answer available.
                      </div>
                    )}
                  </div>
                ) : (
                  <div>
                    <div className="mb-3 text-xs text-subtle">
                      From{" "}
                      <span className="text-default">HighSpot &amp; the docs sites</span>
                    </div>
                    {sourceLoading ? (
                      <div className="flex items-center gap-3 py-8 text-sm text-subtle">
                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-border-medium border-t-accent" />
                        Generating an answer from HighSpot &amp; the docs sites…
                      </div>
                    ) : sourceDisplayError ? (
                      <div className="py-4 text-sm text-error">
                        {sourceDisplayError}
                      </div>
                    ) : sourceAnswer ? (
                      <>
                        {answerBody(sourceAnswer)}
                        {sourcesBlock(sourceDocs)}
                      </>
                    ) : (
                      <div className="py-4 text-sm text-subtle">
                        No answer available from these sources.
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <>
                {answeredByLabel && (
                  <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-border-medium bg-background-weak px-3 py-1 text-xs text-subtle">
                    <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                    Answered by{" "}
                    <span className="font-semibold text-default">
                      {answeredByLabel}
                    </span>
                    {!answeredBy?.routed && <span>· all sources</span>}
                  </div>
                )}
                {answerBody(result.answer)}
                {sourcesBlock(topDocs)}
              </>
            )}

            {otherRecommended.length > 0 && (
              <div className="mt-7 border-t border-border-medium pt-5">
                <div className="text-xs font-semibold uppercase tracking-wide text-subtle mb-3">
                  Recommended assistants
                </div>
                <div className="flex flex-wrap gap-2">
                  {otherRecommended.map((a) => (
                    <button
                      key={a.persona_id}
                      onClick={() => runSearch(question, a.persona_id)}
                      disabled={isLoading}
                      title={`Ask ${
                        a.display_name?.trim() ? a.display_name : a.name
                      } instead`}
                      className="rounded-full border border-border-medium px-3 py-1 text-sm text-default hover:bg-hover transition-colors disabled:opacity-50"
                    >
                      {a.display_name?.trim() ? a.display_name : a.name}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {result.chat_message_id != null && (
              <div className="mt-6 flex items-center gap-2">
                <button
                  title="Helpful"
                  aria-label="Helpful"
                  onClick={() => submitFeedback("like", null)}
                  className={`rounded-lg p-2 hover:bg-hover transition-colors ${
                    feedbackGiven === "like" ? "text-green-600" : "text-subtle"
                  }`}
                >
                  <FiThumbsUp size={16} />
                </button>
                <button
                  title="Not helpful"
                  aria-label="Not helpful"
                  onClick={() => setShowFeedbackBox((v) => !v)}
                  className={`rounded-lg p-2 hover:bg-hover transition-colors ${
                    feedbackGiven === "dislike" ? "text-red-600" : "text-subtle"
                  }`}
                >
                  <FiThumbsDown size={16} />
                </button>
                {feedbackGiven && !showFeedbackBox && (
                  <span className="text-xs text-subtle ml-1">
                    Thanks for the feedback!
                  </span>
                )}
              </div>
            )}

            {showFeedbackBox && (
              <div className="mt-3 flex flex-col gap-2">
                <textarea
                  className="w-full rounded-xl border border-border-medium bg-background-weak p-3 text-sm outline-none focus:border-accent"
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
    </div>
  );
}
