"use client";

/**
 * Assistant Gallery — redesigned UX.
 *
 * Why this rewrite: with 50+ assistants and growing, the old flat 2-column
 * grid had no hierarchy, no status signal (added vs not), and no real
 * filtering — every card looked identical regardless of whether it was
 * yours, shared, public, or already in your picker. This page is now
 * structured around three questions: "is this mine?" (sections), "have
 * I added this?" (availability filter + per-card chip), and "what does
 * it do?" (denser layout + tool/source counts + tool filter chips).
 *
 * Changes packed in (numbers map to the design proposal):
 *
 *   1. Per-card "✓ In your picker" status chip + muted card style for
 *      already-added assistants. Eye finds the un-added ones fast.
 *   2. Three implicit sections: Yours / Shared with you / Featured.
 *      Empty sections hide; section headers carry counts.
 *   3. Filter chips above the grid: availability (All / Available / Added)
 *      + auto-generated per-tool chips (only tools that appear in ≥2
 *      assistants, so the chip row doesn't bloat as the dataset grows).
 *   4. Owner display: name-from-email fallback (split on '@'), with a
 *      "Built-in" badge for default_persona assistants — kills the
 *      fork-specific "Author: Darwin" magic string.
 *   6. Responsive grid: 1 col on mobile, 2 / 3 / 4 by breakpoint.
 *   7. Header matches the Manage page: title + subtitle on the left,
 *      "Back to my assistants" as a text link, "Create new" button
 *      top-right. The giant centered button + paragraph are gone.
 *   8. Sort dropdown: Featured (API order) / A → Z / Newly added.
 *   9. Search now includes tool names AND document-set names. Empty-
 *      search-result has a real empty state with a Clear button.
 *  10. Compact chips ({n} tools / {n} sources), tool list on hover.
 *      Add/Remove buttons replace the heavy Tremor color="green/red"
 *      with flat buttons matching the design system.
 *  11. Design tokens fixed — search input uses border-border /
 *      focus-ring-accent like the rest of the app.
 *
 * What is intentionally NOT here:
 *   - #5 (detail drawer / modal) — deferred per the proposal; revisit
 *     after seeing how users use the new gallery.
 *   - Bulk select — adding 5 assistants at once isn't a real use case.
 *
 * All mutations are optimistic + undoable, mirroring the Manage page.
 */

import { useEffect, useMemo, useState } from "react";
import { Persona } from "@/app/admin/assistants/interfaces";
import { User } from "@/lib/types";
import { AssistantIcon } from "@/components/assistants/AssistantIcon";
import { Bubble } from "@/components/Bubble";
import { usePopup } from "@/components/admin/connectors/Popup";
import {
  addAssistantToList,
  reorderAssistantList,
  removeAssistantFromList,
} from "@/lib/assistants/updateAssistantPreferences";
import { checkUserOwnsAssistant } from "@/lib/assistants/checkOwnership";
import { AssistantsPageTitle } from "../AssistantsPageTitle";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FiBookmark, FiPlus, FiSearch, FiX } from "react-icons/fi";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Availability = "all" | "available" | "added";
type SortMode = "featured" | "name-asc" | "recent";

interface SectionDef {
  key: string;
  label: string;
  assistants: Persona[];
}

// ---------------------------------------------------------------------------
// Column-count parameterisation
// ---------------------------------------------------------------------------
//
// Each row in this table is a complete, static Tailwind class string so the
// purge step actually emits the classes. You CAN'T compute these at runtime
// (`md:grid-cols-${n}` won't survive purge). Add a row here to support a new
// column count. Each row scales 1-col on mobile up to N at the widest
// breakpoint, with one breakpoint per added column so cards stay roomy on
// medium screens.
const GRID_CLASSES: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-1 sm:grid-cols-2",
  3: "grid-cols-1 md:grid-cols-2 2xl:grid-cols-3",
  4: "grid-cols-1 md:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4",
  5: "grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5",
};

const DEFAULT_COLUMNS = 3;

// Values exposed in the in-page column picker. The control lets users
// override the prop-derived default at runtime; the persisted choice
// lives in localStorage so it survives reloads.
//
// Below the smallest md breakpoint everything is 1-col regardless of
// this value (see GRID_CLASSES), so we don't bother exposing 1.
const COLUMN_PICKER_OPTIONS = [2, 3, 4];
const COLUMNS_STORAGE_KEY = "danswer:assistants-gallery:columns";

// How many doc-set name chips to render before collapsing the rest into
// a "+N more" pill. Three keeps each card's scope visible without
// blowing the card width at narrower column counts.
const MAX_VISIBLE_DOC_SETS = 3;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** Best-effort author display name. Names aren't on MinimalUserSnapshot;
 * we have email only. Split on '@' so "foo.bar@example.com" → "foo.bar"
 * rather than dumping the full email at the user. */
function ownerDisplayName(persona: Persona): string | null {
  if (persona.default_persona) return null; // Built-in badge shown instead.
  const email = persona.owner?.email;
  if (!email) return null;
  const local = email.split("@")[0];
  // Replace dots/underscores with spaces and trim — usually closer to
  // "First Last" than the raw local-part.
  return local.replace(/[._]/g, " ").trim() || email;
}

// ---------------------------------------------------------------------------
// Single card
// ---------------------------------------------------------------------------

interface CardProps {
  assistant: Persona;
  user: User | null;
  isAdded: boolean;
  onAdd: (a: Persona) => void;
  onRemove: (a: Persona) => void;
}

function GalleryCard({ assistant, user, isAdded, onAdd, onRemove }: CardProps) {
  // Tool-related UI was intentionally removed from this page (filter
  // chips + per-card counts) — the gallery is for browsing assistants,
  // and tool execution isn't reliable enough to advertise.
  const author = ownerDisplayName(assistant);
  const isBuiltIn = assistant.default_persona;

  return (
    <div
      className={`
        bg-background-emphasis rounded-lg p-5
        border transition
        flex flex-col
        ${
          isAdded
            ? "border-border opacity-75 hover:opacity-100"
            : "border-transparent shadow-sm hover:shadow-md"
        }
      `}
    >
      {/* Header: icon + name (+ built-in badge if applicable). The
          prior absolute top-right "In your picker" badge was dropped —
          the muted card style + the Remove button in the footer
          already signal "added"; the badge ate horizontal space and
          crowded the title at narrower widths. */}
      <div className="flex items-start gap-3 mb-2">
        <AssistantIcon assistant={assistant} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-base font-semibold text-strong truncate">
              {assistant.name}
            </h2>
            {isBuiltIn && (
              <span
                className="
                  text-[10px] uppercase tracking-wide
                  px-1.5 py-0.5 rounded
                  bg-border text-default font-medium
                "
                title="Bundled with the app."
              >
                Built-in
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Description — primary signal of "should I pick this?". */}
      {assistant.description && (
        <p className="text-sm text-default leading-relaxed mb-3 line-clamp-3">
          {assistant.description}
        </p>
      )}

      {/* Knowledge-scope chips — name the document sets the
          assistant points at (counts alone don't help a chooser
          decide). Cap at MAX_VISIBLE_DOC_SETS with a "+N more"
          tooltip so a long list doesn't blow the card width. Tools
          were removed entirely (see card-level comment). */}
      {assistant.document_sets && assistant.document_sets.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-3 text-xs">
          {assistant.document_sets.slice(0, MAX_VISIBLE_DOC_SETS).map((ds) => (
            <Bubble key={ds.id} isSelected={false} notSelectable>
              <div className="flex items-center gap-1 max-w-[180px]">
                <FiBookmark size={12} className="flex-shrink-0" />
                <span className="truncate" title={ds.name}>
                  {ds.name}
                </span>
              </div>
            </Bubble>
          ))}
          {assistant.document_sets.length > MAX_VISIBLE_DOC_SETS && (
            <Bubble isSelected={false} notSelectable>
              <span
                title={assistant.document_sets
                  .slice(MAX_VISIBLE_DOC_SETS)
                  .map((d) => d.name)
                  .join(", ")}
              >
                +{assistant.document_sets.length - MAX_VISIBLE_DOC_SETS} more
              </span>
            </Bubble>
          )}
        </div>
      )}

      {/* Footer row: author (or built-in subtle text) + Add/Remove */}
      <div className="mt-auto flex items-center justify-between gap-2 pt-2 border-t border-border/40">
        <div className="text-xs text-subtle truncate min-w-0">
          {isBuiltIn ? (
            <span>Bundled assistant</span>
          ) : author ? (
            <span title={assistant.owner?.email ?? ""}>by {author}</span>
          ) : (
            // Public assistant with no owner record — rare; surface
            // gracefully without the old "Author: Darwin" magic string.
            <span>Public</span>
          )}
        </div>

        {/* Add/Remove — flat, matches design system. Tremor's color="green"
            for "Add to my list" was visually shoutier than the action. */}
        {user &&
          (isAdded ? (
            <button
              type="button"
              onClick={() => onRemove(assistant)}
              className="
                text-xs px-3 py-1.5 rounded
                border border-border
                text-default hover:bg-hover
                flex items-center gap-1
                focus:outline-none focus:ring-2 focus:ring-accent
              "
              title="Remove from your picker"
            >
              <FiX size={12} /> Remove
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onAdd(assistant)}
              className="
                text-xs px-3 py-1.5 rounded
                bg-accent text-inverted font-medium
                hover:opacity-90
                flex items-center gap-1
                focus:outline-none focus:ring-2 focus:ring-accent
              "
              title="Add to your picker"
            >
              <FiPlus size={12} /> Add
            </button>
          ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filter chip — small reusable toggle pill
// ---------------------------------------------------------------------------

function FilterChip({
  active,
  onClick,
  children,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  badge?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`
        text-xs px-3 py-1.5 rounded-full
        border transition-colors
        flex items-center gap-1.5
        focus:outline-none focus:ring-2 focus:ring-accent
        ${
          active
            ? "bg-accent text-inverted border-accent"
            : "bg-background border-border text-default hover:bg-hover"
        }
      `}
    >
      {children}
      {badge !== undefined && (
        <span
          className={`
            text-[10px] px-1.5 rounded-full
            ${active ? "bg-white/20" : "bg-border"}
          `}
        >
          {badge}
        </span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export function AssistantsGallery({
  assistants,
  user,
  columns: initialColumns = DEFAULT_COLUMNS,
}: {
  assistants: Persona[];
  user: User | null;
  /**
   * Initial max columns at the widest breakpoint. Acts as the default
   * if the user has no stored preference yet; once the user picks via
   * the in-page column control that choice (in localStorage) wins.
   * Responsive scaling below the widest breakpoint is fixed
   * (see GRID_CLASSES). Supported values: 1–5; out-of-range silently
   * falls back to DEFAULT_COLUMNS so a bad prop can't break the page.
   */
  columns?: number;
}) {
  const router = useRouter();

  // User-chosen column count. `null` until the localStorage read in
  // the effect below; SSR + first paint use the prop value so we
  // don't get a hydration mismatch. After mount, the stored choice
  // (if any) overrides the prop.
  const [userColumns, setUserColumns] = useState<number | null>(null);
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(COLUMNS_STORAGE_KEY);
      if (raw == null) return;
      const n = Number.parseInt(raw, 10);
      if (Number.isFinite(n) && n in GRID_CLASSES) {
        setUserColumns(n);
      }
    } catch {
      // localStorage can throw in some sandboxed contexts (Safari
      // private mode in the past, certain iframe configs). Fall
      // through to the prop default — the picker still works for
      // the session, just doesn't persist.
    }
  }, []);

  const effectiveColumns = userColumns ?? initialColumns;
  const gridClass =
    GRID_CLASSES[effectiveColumns] ?? GRID_CLASSES[DEFAULT_COLUMNS];

  const changeColumns = (n: number) => {
    setUserColumns(n);
    try {
      window.localStorage.setItem(COLUMNS_STORAGE_KEY, String(n));
    } catch {
      // See above — silently OK to skip persistence.
    }
  };
  const { popup, setPopup } = usePopup();

  // Mirrors the Manage page: no preference = every accessible assistant
  // is "in the picker" by default.
  const initialChosen: number[] =
    user?.preferences?.chosen_assistants ?? assistants.map((a) => a.id);
  const [chosenAssistants, setChosenAssistants] =
    useState<number[]>(initialChosen);
  const chosenSet = useMemo(
    () => new Set(chosenAssistants),
    [chosenAssistants]
  );

  // ---- filter / sort state -------------------------------------------------

  const [search, setSearch] = useState("");
  const [availability, setAvailability] = useState<Availability>("all");
  const [sortMode, setSortMode] = useState<SortMode>("featured");

  // ---- derived: filtered + sorted list -------------------------------------
  // Tool-related filtering was removed from this page — the gallery is
  // about assistants, not their internals. Search now only matches
  // name + description + document-set names.

  const filtered: Persona[] = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out = assistants.filter((a) => {
      if (q) {
        const hay = [
          a.name,
          a.description ?? "",
          ...(a.document_sets ?? []).map((d) => d.name),
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (availability === "added" && !chosenSet.has(a.id)) return false;
      if (availability === "available" && chosenSet.has(a.id)) return false;
      return true;
    });

    if (sortMode === "name-asc") {
      out.sort((a, b) => a.name.localeCompare(b.name));
    } else if (sortMode === "recent") {
      // No created_at on Persona; id desc is a fair proxy ("newer ids
      // were created later").
      out.sort((a, b) => b.id - a.id);
    }
    // "featured" = preserve API order (admins curate via display_priority).
    return out;
  }, [assistants, search, availability, sortMode, chosenSet]);

  // ---- derived: sections ---------------------------------------------------

  const sections: SectionDef[] = useMemo(() => {
    const yours: Persona[] = [];
    const shared: Persona[] = [];
    const featured: Persona[] = [];

    for (const a of filtered) {
      const ownedByUser = checkUserOwnsAssistant(user, a);
      const sharedWithUser =
        user != null &&
        !ownedByUser &&
        !a.is_public &&
        (a.users ?? []).some((u) => u.id === user.id);

      if (ownedByUser && !a.default_persona) {
        yours.push(a);
      } else if (sharedWithUser) {
        shared.push(a);
      } else {
        // Public OR built-in OR (accessible via group permission). All
        // surface here as "Featured & Built-in" — visually equivalent
        // from a chooser's POV.
        featured.push(a);
      }
    }

    const out: SectionDef[] = [];
    if (yours.length > 0)
      out.push({ key: "yours", label: "Yours", assistants: yours });
    if (shared.length > 0)
      out.push({ key: "shared", label: "Shared with you", assistants: shared });
    if (featured.length > 0)
      out.push({
        key: "featured",
        label: "Featured & Built-in",
        assistants: featured,
      });
    return out;
  }, [filtered, user]);

  // ---- counts for filter chips ---------------------------------------------

  const counts = useMemo(() => {
    const q = search.trim().toLowerCase();
    let all = 0;
    let added = 0;
    let available = 0;
    for (const a of assistants) {
      if (q) {
        const hay = [
          a.name,
          a.description ?? "",
          ...(a.document_sets ?? []).map((d) => d.name),
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) continue;
      }
      all++;
      if (chosenSet.has(a.id)) added++;
      else available++;
    }
    return { all, added, available };
  }, [assistants, search, chosenSet]);

  // ---- optimistic add/remove (mirrors Manage page persistOrder) -----------

  const persistChosen = async (
    next: number[],
    {
      successMsg,
      undoToOrder,
    }: { successMsg?: string; undoToOrder?: number[] } = {}
  ): Promise<boolean> => {
    const prev = chosenAssistants;
    setChosenAssistants(next);
    const ok = await reorderAssistantList(next);
    if (!ok) {
      setChosenAssistants(prev);
      setPopup({
        message: "Couldn't update your assistant list — please try again.",
        type: "error",
      });
      return false;
    }
    if (successMsg) {
      setPopup({
        message: successMsg,
        type: "success",
        undo:
          undoToOrder !== undefined
            ? {
                onClick: async () => {
                  await persistChosen(undoToOrder);
                },
              }
            : undefined,
      });
    }
    router.refresh();
    return true;
  };

  const handleAdd = async (a: Persona) => {
    if (!user) return;
    if (chosenSet.has(a.id)) return; // already added — no-op
    const prev = chosenAssistants;
    const next = [...prev, a.id];
    // Use addAssistantToList specifically (idempotent) rather than the
    // generic reorder helper — both PATCH the same endpoint, but this
    // signals intent at the call-site.
    setChosenAssistants(next);
    const ok = await addAssistantToList(a.id, prev);
    if (!ok) {
      setChosenAssistants(prev);
      setPopup({
        message: `Couldn't add "${a.name}". Try again?`,
        type: "error",
      });
      return;
    }
    setPopup({
      message: `"${a.name}" added to your picker.`,
      type: "success",
      undo: {
        onClick: async () => {
          await persistChosen(prev);
        },
      },
    });
    router.refresh();
  };

  const handleRemove = async (a: Persona) => {
    if (!user) return;
    if (chosenAssistants.length === 1 && chosenAssistants[0] === a.id) {
      setPopup({
        message:
          "You need at least one visible assistant — can't remove the last one.",
        type: "error",
      });
      return;
    }
    const prev = chosenAssistants;
    const next = prev.filter((id) => id !== a.id);
    setChosenAssistants(next);
    const ok = await removeAssistantFromList(a.id, prev);
    if (!ok) {
      setChosenAssistants(prev);
      setPopup({
        message: `Couldn't remove "${a.name}". Try again?`,
        type: "error",
      });
      return;
    }
    setPopup({
      message: `"${a.name}" removed from your picker.`,
      type: "success",
      undo: {
        onClick: async () => {
          await persistChosen(prev);
        },
      },
    });
    router.refresh();
  };

  // ---- handlers for filter UI ----------------------------------------------

  const clearAllFilters = () => {
    setSearch("");
    setAvailability("all");
  };

  const hasAnyFilter = search.trim() !== "" || availability !== "all";

  // ---- render -------------------------------------------------------------

  return (
    <>
      {popup}

      <div className="mx-auto w-searchbar-xs 2xl:w-searchbar-sm 3xl:w-searchbar pb-12">
        {/* Header — matches the Manage page rebuild */}
        <div className="flex items-start justify-between gap-4 mb-3">
          <div className="min-w-0">
            <AssistantsPageTitle>Assistant Gallery</AssistantsPageTitle>
            <p className="text-subtle">
              Browse every assistant available to you. Add the ones you want to
              your chat picker.
            </p>
          </div>
          <Link
            href="/assistants/new"
            className="
              flex items-center gap-1.5 flex-shrink-0
              px-4 py-2 rounded-md
              bg-accent text-inverted font-medium
              hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-accent
            "
          >
            <FiPlus size={16} /> Create new
          </Link>
        </div>

        <div className="mb-4">
          <Link
            href="/assistants/mine"
            className="text-sm text-link hover:underline inline-flex items-center gap-1"
          >
            ← Back to my assistants
          </Link>
        </div>

        {/* Search */}
        <div className="relative mb-3">
          <FiSearch
            className="absolute left-3 top-1/2 -translate-y-1/2 text-subtle"
            size={16}
          />
          <input
            type="search"
            placeholder="Search by name, description, tool, or source…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="
              w-full pl-10 pr-3 py-2
              rounded-md border border-border bg-background
              focus:outline-none focus:ring-2 focus:ring-accent
            "
          />
        </div>

        {/* Filter chip rows */}
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <FilterChip
            active={availability === "all"}
            onClick={() => setAvailability("all")}
            badge={counts.all}
          >
            All
          </FilterChip>
          <FilterChip
            active={availability === "available"}
            onClick={() => setAvailability("available")}
            badge={counts.available}
          >
            Available to add
          </FilterChip>
          <FilterChip
            active={availability === "added"}
            onClick={() => setAvailability("added")}
            badge={counts.added}
          >
            Already added
          </FilterChip>

          {/* View controls — columns + sort — live at the right end of
              the filter row so "narrow the list" (left) and "shape
              the view" (right) are visually separated. */}
          <div className="ml-auto flex items-center gap-3 text-xs text-subtle">
            {/* Column picker. Hidden below md since the layout falls
                back to a single column there regardless. Pure
                client-side state + localStorage — no fetch, no
                router.refresh(), no DB hit. */}
            <div className="hidden md:flex items-center gap-2">
              <label htmlFor="columns">Columns</label>
              <select
                id="columns"
                value={effectiveColumns}
                onChange={(e) => changeColumns(Number(e.target.value))}
                className="
                  text-xs px-2 py-1.5 rounded
                  border border-border bg-background
                  focus:outline-none focus:ring-2 focus:ring-accent
                "
              >
                {COLUMN_PICKER_OPTIONS.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </div>
            <label htmlFor="sort">Sort</label>
            <select
              id="sort"
              value={sortMode}
              onChange={(e) => setSortMode(e.target.value as SortMode)}
              className="
                text-xs px-2 py-1.5 rounded
                border border-border bg-background
                focus:outline-none focus:ring-2 focus:ring-accent
              "
            >
              <option value="featured">Featured</option>
              <option value="name-asc">A → Z</option>
              <option value="recent">Newly added</option>
            </select>
          </div>
        </div>

        {/* Empty state when filters exclude everything */}
        {sections.length === 0 && (
          <div className="text-center py-12 text-subtle">
            <p className="font-medium text-default">No assistants match.</p>
            <p className="text-sm mt-1">
              {hasAnyFilter
                ? "Try a different filter, or clear all to see everything."
                : "There are no assistants available to you yet."}
            </p>
            {hasAnyFilter && (
              <button
                type="button"
                onClick={clearAllFilters}
                className="
                  mt-3 text-sm px-3 py-1.5 rounded
                  border border-border hover:bg-hover
                  focus:outline-none focus:ring-2 focus:ring-accent
                "
              >
                Clear all filters
              </button>
            )}
          </div>
        )}

        {/* Sections */}
        {sections.map((section) => (
          <section key={section.key} className="mb-8">
            <div className="flex items-center gap-2 mb-3">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-subtle">
                {section.label}
              </h3>
              <span className="text-xs text-subtle">
                ({section.assistants.length})
              </span>
            </div>
            <div className={`grid gap-3 ${gridClass}`}>
              {section.assistants.map((assistant) => (
                <GalleryCard
                  key={assistant.id}
                  assistant={assistant}
                  user={user}
                  isAdded={chosenSet.has(assistant.id)}
                  onAdd={handleAdd}
                  onRemove={handleRemove}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </>
  );
}
