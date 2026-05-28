"use client";

/**
 * Manage Assistants — redesigned UX.
 *
 * What changed vs the prior "move up / move down inside a 3-dot popover":
 *
 *   1. Drag-and-drop reorder via @dnd-kit (already in package.json), with
 *      a grab handle on each visible row. Up/down arrows removed.
 *   2. Explicit "set as default" pin icon on each row. Filled = current
 *      default; the default row also gets an accent border. Ordering and
 *      default are now orthogonal.
 *   3. Visibility is a row-level toggle, not a popover item. The page
 *      shows ONE list with a divider; hidden rows render under the
 *      "Hidden" divider at reduced opacity.
 *   4. Client-side search filters by name + description + tool name.
 *   5. Description font-weight bumped; tool chips moved behind a hover
 *      reveal so the visual hierarchy answers "should I pick this?".
 *      A "{n} sources" chip surfaces document-set count, which used to
 *      be hidden in expanded mode only.
 *   6. Bulk select column + action bar (Show / Hide / Remove) appears
 *      only when something is selected.
 *   7. Header: single title + 1-line subtitle + Create button top-right,
 *      "Browse all available" as a text link. Cut the giant tile pair.
 *   8. Undo toast on reorder / default-change / visibility-toggle.
 *      Reuses the extended Popup component (`undo` field on PopupSpec).
 *
 * Everything is optimistic: local `chosenOrder` state mutates first, the
 * PATCH runs after, and a failure rolls back + shows an error toast.
 *
 * NOTE: this file replaces the old up/down arrow flow entirely; the
 * `moveAssistantUp` / `moveAssistantDown` helpers in
 * `lib/assistants/updateAssistantPreferences.ts` are kept for any other
 * callers but no longer used here.
 */

import { useMemo, useState } from "react";
import { MinimalUserSnapshot, User } from "@/lib/types";
import { Persona } from "@/app/admin/assistants/interfaces";
import { Text } from "@tremor/react";
import {
  FiBookmark,
  FiEdit2,
  FiEye,
  FiEyeOff,
  FiPlus,
  FiSearch,
  FiShare2,
  FiStar,
  FiTool,
  FiTrash2,
} from "react-icons/fi";
import { MdDragIndicator } from "react-icons/md";
import {
  DndContext,
  DragEndEvent,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { restrictToVerticalAxis } from "@dnd-kit/modifiers";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { AssistantIcon } from "@/components/assistants/AssistantIcon";
import { Bubble } from "@/components/Bubble";
import { PopupSpec, usePopup } from "@/components/admin/connectors/Popup";
import { checkUserOwnsAssistant } from "@/lib/assistants/checkOwnership";
import {
  bulkAddToList,
  bulkRemoveFromList,
  reorderAssistantList,
  setDefaultAssistant,
} from "@/lib/assistants/updateAssistantPreferences";
import { AssistantSharingModal } from "./AssistantSharingModal";
import { AssistantSharedStatusDisplay } from "../AssistantSharedStatus";
import { AssistantsPageTitle } from "../AssistantsPageTitle";

// ---------------------------------------------------------------------------
// Small inline switch — avoids pulling in a new component library for one
// toggle. role="switch" gives screen readers the right semantics.
// ---------------------------------------------------------------------------

function Toggle({
  checked,
  onChange,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  ariaLabel: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={(e) => {
        e.stopPropagation();
        onChange(!checked);
      }}
      className={`
        relative inline-flex h-5 w-9 items-center rounded-full
        transition-colors flex-shrink-0
        focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-1
        ${checked ? "bg-accent" : "bg-border"}
      `}
    >
      <span
        className={`
          inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform
          ${checked ? "translate-x-[18px]" : "translate-x-[3px]"}
        `}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Single row — used both inside the sortable visible section AND in the
// hidden section. `isSortable` toggles drag affordances; the rest of the
// row is identical so the visual stays consistent across the divider.
// ---------------------------------------------------------------------------

interface RowProps {
  assistant: Persona;
  user: User | null;
  isDefault: boolean;
  isVisible: boolean;
  isSelected: boolean;
  onToggleSelect: (id: number) => void;
  onSetDefault: (id: number) => void;
  onToggleVisibility: (id: number, makeVisible: boolean) => void;
  onShareClick: (id: number) => void;
}

function RowContent({
  assistant,
  user,
  isDefault,
  isVisible,
  isSelected,
  onToggleSelect,
  onSetDefault,
  onToggleVisibility,
  onShareClick,
  // From useSortable when in sortable context; null otherwise.
  dragHandleProps,
}: RowProps & {
  dragHandleProps:
    | (React.HTMLAttributes<HTMLButtonElement> & { ref?: any })
    | null;
}) {
  const isOwnedByUser = checkUserOwnsAssistant(user, assistant);
  const canEdit = isOwnedByUser;
  const canShare = isOwnedByUser && !assistant.is_public;

  // Tool / doc-set counts — surfaced as small chips. Description should
  // be the primary affordance; chips give the at-a-glance "scope" signal.
  const toolCount = assistant.tools?.length ?? 0;
  const docSetCount = assistant.document_sets?.length ?? 0;

  return (
    <div
      className={`
        group bg-background-emphasis rounded-lg p-4 mb-3
        flex items-center gap-3
        border transition
        ${isDefault ? "border-accent shadow-md" : "border-transparent shadow-sm"}
        ${isVisible ? "" : "opacity-50"}
        ${isSelected ? "ring-2 ring-accent" : ""}
      `}
    >
      {/* Bulk-select checkbox. Hidden until hover or when something is
          already selected on the page (the parent shows the action bar
          based on that). Keyboard users always have it via focus. */}
      <input
        type="checkbox"
        aria-label={`Select ${assistant.name}`}
        checked={isSelected}
        onChange={() => onToggleSelect(assistant.id)}
        className="
          h-4 w-4 cursor-pointer
          opacity-30 group-hover:opacity-100 focus:opacity-100
          checked:opacity-100
          transition-opacity
        "
      />

      {/* Drag handle — only meaningful for visible rows. Hidden rows
          have no position to drag to. */}
      {dragHandleProps ? (
        <button
          type="button"
          aria-label={`Drag to reorder ${assistant.name}`}
          className="
            cursor-grab active:cursor-grabbing
            text-subtle hover:text-default
            opacity-40 group-hover:opacity-100 transition-opacity
            focus:outline-none focus:ring-2 focus:ring-accent rounded
            touch-none
          "
          {...dragHandleProps}
        >
          <MdDragIndicator size={22} />
        </button>
      ) : (
        // Reserve the slot so visible/hidden rows line up vertically.
        <div className="w-[22px] flex-shrink-0" />
      )}

      <AssistantIcon assistant={assistant} />

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <h2 className="text-base font-semibold truncate">{assistant.name}</h2>
          {isDefault && (
            <span
              className="
                text-xs px-2 py-0.5 rounded-full
                bg-accent/15 text-accent font-medium
              "
            >
              Default
            </span>
          )}
        </div>

        {/* Description bumped — used to be text-sm with no weight; now
            it's the primary signal of what the assistant is for. */}
        {assistant.description && (
          <div className="text-sm text-default leading-snug">
            {assistant.description}
          </div>
        )}

        {/* Sharing status, e.g. "Shared with 3 people". */}
        <div className="mt-1">
          <AssistantSharedStatusDisplay assistant={assistant} user={user} />
        </div>

        {/* Scope chips — tool/source counts. Compact summary always;
            full tool list reveals on hover so the row stays scannable. */}
        {(toolCount > 0 || docSetCount > 0) && (
          <div className="flex flex-wrap gap-2 mt-2 text-xs text-subtle">
            {docSetCount > 0 && (
              <Bubble isSelected={false}>
                <div className="flex items-center gap-1">
                  <FiBookmark size={12} />
                  {docSetCount} source{docSetCount === 1 ? "" : "s"}
                </div>
              </Bubble>
            )}
            {toolCount > 0 && (
              <Bubble isSelected={false}>
                <div
                  className="flex items-center gap-1"
                  title={assistant.tools.map((t) => t.name).join(", ")}
                >
                  <FiTool size={12} />
                  {toolCount} tool{toolCount === 1 ? "" : "s"}
                </div>
              </Bubble>
            )}
          </div>
        )}
      </div>

      {/* Right-side actions. Order matters for scannability: default
          pin first (most-used), visibility toggle, then ownership
          actions (edit/share). */}
      <div className="flex items-center gap-2 flex-shrink-0">
        {/* Pin / default. Only meaningful for visible rows — pinning a
            hidden one would have to unhide it too; we surface that via
            the visibility toggle instead. */}
        {isVisible && (
          <button
            type="button"
            aria-label={
              isDefault
                ? `${assistant.name} is your default`
                : `Set ${assistant.name} as default`
            }
            disabled={isDefault}
            onClick={() => onSetDefault(assistant.id)}
            className={`
              p-2 rounded
              ${
                isDefault
                  ? "text-accent cursor-default"
                  : "text-subtle hover:text-default hover:bg-hover cursor-pointer"
              }
              focus:outline-none focus:ring-2 focus:ring-accent
            `}
            title={isDefault ? "Default assistant" : "Set as default"}
          >
            <FiStar
              size={16}
              className={isDefault ? "fill-current" : ""}
            />
          </button>
        )}

        {/* Visibility — switch instead of a buried popover item. */}
        <Toggle
          checked={isVisible}
          onChange={(next) => onToggleVisibility(assistant.id, next)}
          ariaLabel={
            isVisible
              ? `Hide ${assistant.name} from the picker`
              : `Show ${assistant.name} in the picker`
          }
        />

        {canShare && (
          <button
            type="button"
            aria-label="Share assistant"
            onClick={() => onShareClick(assistant.id)}
            className="p-2 rounded hover:bg-hover text-subtle hover:text-default"
            title="Share"
          >
            <FiShare2 size={16} />
          </button>
        )}
        {canEdit && (
          <Link
            href={`/assistants/edit/${assistant.id}`}
            aria-label="Edit assistant"
            className="p-2 rounded hover:bg-hover text-subtle hover:text-default"
            title="Edit"
          >
            <FiEdit2 size={16} />
          </Link>
        )}
      </div>
    </div>
  );
}

// Sortable row — wraps RowContent and wires up @dnd-kit's transform/listeners.
function SortableAssistantRow(props: RowProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: props.assistant.id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  return (
    <div ref={setNodeRef} style={style}>
      <RowContent
        {...props}
        dragHandleProps={{
          ref: setActivatorNodeRef,
          ...attributes,
          ...listeners,
        }}
      />
    </div>
  );
}

// Static row — used for the hidden section (no DnD).
function StaticAssistantRow(props: RowProps) {
  return <RowContent {...props} dragHandleProps={null} />;
}

// ---------------------------------------------------------------------------
// Bulk action bar — appears only when something is selected. The hide/show
// split mirrors the per-row visibility toggle; "Remove" matches the prior
// "Hide / Remove" semantic (removes from chosen_assistants regardless of
// ownership).
// ---------------------------------------------------------------------------

function BulkActionsBar({
  selectedCount,
  onClearSelection,
  onShow,
  onHide,
  onRemove,
}: {
  selectedCount: number;
  onClearSelection: () => void;
  onShow: () => void;
  onHide: () => void;
  onRemove: () => void;
}) {
  return (
    <div
      className="
        sticky top-2 z-10 mb-4
        bg-background-emphasis border border-accent/30 shadow-md rounded-lg
        flex items-center gap-3 p-3
      "
    >
      <span className="text-sm font-medium">
        {selectedCount} selected
      </span>
      <button
        type="button"
        onClick={onShow}
        className="
          text-sm px-3 py-1.5 rounded
          hover:bg-hover flex items-center gap-1.5
        "
      >
        <FiEye size={14} /> Show
      </button>
      <button
        type="button"
        onClick={onHide}
        className="
          text-sm px-3 py-1.5 rounded
          hover:bg-hover flex items-center gap-1.5
        "
      >
        <FiEyeOff size={14} /> Hide
      </button>
      <button
        type="button"
        onClick={onRemove}
        className="
          text-sm px-3 py-1.5 rounded
          hover:bg-hover text-error flex items-center gap-1.5
        "
      >
        <FiTrash2 size={14} /> Remove
      </button>
      <button
        type="button"
        onClick={onClearSelection}
        className="ml-auto text-sm text-subtle hover:text-default"
      >
        Clear
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main list. State model:
//   - `chosenOrder`: the user's chosen_assistants array (ordered, visible)
//   - hidden = every assistant the user has access to that's NOT in chosenOrder
//   - selected: bulk-action set; orthogonal to visible/hidden
//   - search: pure client-side filter applied to both groups before render
//
// All mutations are optimistic — update local state, fire PATCH; on error
// roll back and surface a toast. router.refresh() runs on success so the
// rest of the app (chat picker etc.) sees the new order.
// ---------------------------------------------------------------------------

interface AssistantsListProps {
  user: User | null;
  assistants: Persona[];
}

export function AssistantsList({ user, assistants }: AssistantsListProps) {
  const router = useRouter();
  const { popup, setPopup } = usePopup();

  // When the user has no preference yet, treat every accessible
  // assistant as "visible by default" — matches the previous behavior.
  const initialChosen: number[] =
    user?.preferences?.chosen_assistants ?? assistants.map((a) => a.id);

  const [chosenOrder, setChosenOrder] = useState<number[]>(initialChosen);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [sharingAssistantId, setSharingAssistantId] = useState<number | null>(
    null
  );

  // Pulled from /api/users; used by the share modal. Same pattern as the
  // pre-rewrite component.
  const { data: allUsers } = useSWR<MinimalUserSnapshot[]>(
    "/api/users",
    errorHandlingFetcher
  );

  // Derived: id-keyed lookup, visible/hidden splits, search-filtered.
  const assistantsById = useMemo(
    () => new Map(assistants.map((a) => [a.id, a])),
    [assistants]
  );
  const chosenSet = useMemo(() => new Set(chosenOrder), [chosenOrder]);

  const visibleAssistants: Persona[] = useMemo(() => {
    const out: Persona[] = [];
    for (const id of chosenOrder) {
      const a = assistantsById.get(id);
      if (a) out.push(a);
    }
    return out;
  }, [chosenOrder, assistantsById]);

  const hiddenAssistants: Persona[] = useMemo(
    () => assistants.filter((a) => !chosenSet.has(a.id)),
    [assistants, chosenSet]
  );

  const matchesSearch = (a: Persona) => {
    if (!search.trim()) return true;
    const q = search.trim().toLowerCase();
    if (a.name.toLowerCase().includes(q)) return true;
    if (a.description?.toLowerCase().includes(q)) return true;
    return (a.tools ?? []).some((t) => t.name.toLowerCase().includes(q));
  };

  const filteredVisible = visibleAssistants.filter(matchesSearch);
  const filteredHidden = hiddenAssistants.filter(matchesSearch);

  // The default is just position 0 of chosen_assistants. If the user has
  // no preference at all, there's no notion of "default yet" — leave it
  // unset so no row shows the accent until the user picks.
  const defaultId =
    user?.preferences?.chosen_assistants && chosenOrder.length > 0
      ? chosenOrder[0]
      : null;

  // ---- persistence with optimistic + undo --------------------------------

  const persistOrder = async (
    nextOrder: number[],
    {
      successMsg,
      undoToOrder,
    }: { successMsg?: string; undoToOrder?: number[] } = {}
  ): Promise<boolean> => {
    const prev = chosenOrder;
    setChosenOrder(nextOrder);
    const ok = await reorderAssistantList(nextOrder);
    if (!ok) {
      setChosenOrder(prev);
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
                  await persistOrder(undoToOrder);
                },
              }
            : undefined,
      });
    }
    // Refresh the SSR-fetched data so other parts of the app see the
    // new order (chat picker, sidebar, etc.).
    router.refresh();
    return true;
  };

  // ---- handlers ----------------------------------------------------------

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = chosenOrder.indexOf(Number(active.id));
    const newIndex = chosenOrder.indexOf(Number(over.id));
    if (oldIndex < 0 || newIndex < 0) return;
    const next = arrayMove(chosenOrder, oldIndex, newIndex);
    void persistOrder(next, {
      successMsg: "Order updated.",
      undoToOrder: chosenOrder,
    });
  };

  const handleSetDefault = async (id: number) => {
    if (chosenOrder[0] === id) return;
    const prev = chosenOrder;
    const ok = await persistOrder(
      [id, ...chosenOrder.filter((x) => x !== id)],
      {
        successMsg: `Default assistant updated.`,
        undoToOrder: prev,
      }
    );
    if (!ok) {
      // persistOrder already showed the error toast.
    } else {
      // setDefaultAssistant also handles the case where id wasn't in
      // chosen_assistants; persistOrder above already prepended it.
      void setDefaultAssistant(id, prev); // best-effort idempotent confirmation
    }
  };

  const handleToggleVisibility = async (id: number, makeVisible: boolean) => {
    const prev = chosenOrder;
    if (makeVisible) {
      // Add to end so reorder isn't surprising.
      const next = [...chosenOrder, id];
      const assistant = assistantsById.get(id);
      await persistOrder(next, {
        successMsg: assistant
          ? `"${assistant.name}" added to your picker.`
          : "Added to your picker.",
        undoToOrder: prev,
      });
    } else {
      if (chosenOrder.length === 1 && chosenOrder[0] === id) {
        setPopup({
          message:
            "You need at least one visible assistant — can't hide the last one.",
          type: "error",
        });
        return;
      }
      const next = chosenOrder.filter((x) => x !== id);
      const assistant = assistantsById.get(id);
      await persistOrder(next, {
        successMsg: assistant
          ? `"${assistant.name}" hidden from your picker.`
          : "Hidden from your picker.",
        undoToOrder: prev,
      });
    }
  };

  const handleToggleSelect = (id: number) => {
    setSelected((curr) => {
      const next = new Set(curr);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const handleBulkShow = async () => {
    const ids = Array.from(selected);
    const prev = chosenOrder;
    const ok = await bulkAddToList(ids, chosenOrder);
    if (!ok) {
      setPopup({ message: "Couldn't show selected.", type: "error" });
      return;
    }
    // Mirror the optimistic update locally — the helper PATCHed the
    // server; we just need to align local state.
    const existing = new Set(chosenOrder);
    const toAppend = ids.filter((id) => !existing.has(id));
    setChosenOrder([...chosenOrder, ...toAppend]);
    setPopup({
      message: `${ids.length} assistant${ids.length === 1 ? "" : "s"} shown.`,
      type: "success",
      undo: {
        onClick: async () => {
          await persistOrder(prev);
        },
      },
    });
    clearSelection();
    router.refresh();
  };

  const handleBulkHide = async () => {
    const ids = Array.from(selected);
    // Don't let the user hide every visible row at once.
    const remaining = chosenOrder.filter((id) => !ids.includes(id));
    if (remaining.length === 0 && chosenOrder.length > 0) {
      setPopup({
        message: "Can't hide every visible assistant — keep at least one.",
        type: "error",
      });
      return;
    }
    const prev = chosenOrder;
    const ok = await bulkRemoveFromList(ids, chosenOrder);
    if (!ok) {
      setPopup({ message: "Couldn't hide selected.", type: "error" });
      return;
    }
    setChosenOrder(remaining);
    setPopup({
      message: `${ids.length} assistant${ids.length === 1 ? "" : "s"} hidden.`,
      type: "success",
      undo: {
        onClick: async () => {
          await persistOrder(prev);
        },
      },
    });
    clearSelection();
    router.refresh();
  };

  // "Remove" is the same backend op as Hide today — both just remove the
  // ids from chosen_assistants. The label distinction is a UX hint: Hide
  // is reversible by toggling the switch back on (or Undo); Remove
  // implies "I don't want to see this any more." Functionally identical
  // until we have a true "remove access" path.
  const handleBulkRemove = handleBulkHide;

  // ---- DnD plumbing -------------------------------------------------------

  // 6px activation distance: a click on the handle shouldn't immediately
  // start a drag. Helps especially for the click-and-then-undo flow.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } })
  );

  // ---- render -------------------------------------------------------------

  const sharingAssistant =
    sharingAssistantId != null
      ? assistantsById.get(sharingAssistantId) ?? null
      : null;

  return (
    <>
      {popup}

      {sharingAssistant && (
        <AssistantSharingModal
          assistant={sharingAssistant}
          user={user}
          allUsers={allUsers ?? []}
          onClose={() => {
            setSharingAssistantId(null);
            router.refresh();
          }}
          show
        />
      )}

      <div className="mx-auto w-searchbar-xs 2xl:w-searchbar-sm 3xl:w-searchbar pb-12">
        {/* Header: title + 1-line subtitle + create button + browse link.
            Cut the two-tile nav block and the explanatory paragraph. */}
        <div className="flex items-start justify-between gap-4 mb-3">
          <div className="min-w-0">
            <AssistantsPageTitle>My Assistants</AssistantsPageTitle>
            <Text className="text-subtle">
              Choose which assistants appear in the chat picker, set your
              default, and reorder by dragging.
            </Text>
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
            <FiPlus size={16} /> Create
          </Link>
        </div>

        <div className="mb-4">
          <Link
            href="/assistants/gallery"
            className="text-sm text-link hover:underline inline-flex items-center gap-1"
          >
            <FiSearch size={14} /> Browse all available assistants
          </Link>
        </div>

        {/* Search */}
        <div className="relative mb-4">
          <FiSearch
            className="absolute left-3 top-1/2 -translate-y-1/2 text-subtle"
            size={16}
          />
          <input
            type="search"
            placeholder="Filter by name, description, or tool…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="
              w-full pl-10 pr-3 py-2
              rounded-md border border-border bg-background
              focus:outline-none focus:ring-2 focus:ring-accent
            "
          />
        </div>

        {/* Bulk actions — only when something selected */}
        {selected.size > 0 && (
          <BulkActionsBar
            selectedCount={selected.size}
            onClearSelection={clearSelection}
            onShow={handleBulkShow}
            onHide={handleBulkHide}
            onRemove={handleBulkRemove}
          />
        )}

        {/* Visible section — draggable */}
        {filteredVisible.length > 0 ? (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragEnd={handleDragEnd}
            modifiers={[restrictToVerticalAxis]}
          >
            <SortableContext
              items={filteredVisible.map((a) => a.id)}
              strategy={verticalListSortingStrategy}
            >
              {filteredVisible.map((assistant) => (
                <SortableAssistantRow
                  key={assistant.id}
                  assistant={assistant}
                  user={user}
                  isDefault={defaultId === assistant.id}
                  isVisible
                  isSelected={selected.has(assistant.id)}
                  onToggleSelect={handleToggleSelect}
                  onSetDefault={handleSetDefault}
                  onToggleVisibility={handleToggleVisibility}
                  onShareClick={setSharingAssistantId}
                />
              ))}
            </SortableContext>
          </DndContext>
        ) : (
          <EmptyState
            title="No visible assistants"
            body={
              search
                ? `Nothing matches "${search}" in your visible list.`
                : "Toggle one on below to show it in the chat picker."
            }
          />
        )}

        {/* Hidden section — only show divider/header if there's anything */}
        {filteredHidden.length > 0 && (
          <>
            <div className="flex items-center gap-3 my-6">
              <div className="flex-1 h-px bg-border" />
              <span className="text-xs uppercase tracking-wide text-subtle font-medium">
                Hidden ({filteredHidden.length})
              </span>
              <div className="flex-1 h-px bg-border" />
            </div>
            {filteredHidden.map((assistant) => (
              <StaticAssistantRow
                key={assistant.id}
                assistant={assistant}
                user={user}
                isDefault={false}
                isVisible={false}
                isSelected={selected.has(assistant.id)}
                onToggleSelect={handleToggleSelect}
                onSetDefault={handleSetDefault}
                onToggleVisibility={handleToggleVisibility}
                onShareClick={setSharingAssistantId}
              />
            ))}
          </>
        )}

        {/* Search produced nothing at all */}
        {filteredVisible.length === 0 && filteredHidden.length === 0 && (
          <EmptyState
            title="No assistants match"
            body={`Try a different filter — "${search}" matched nothing.`}
          />
        )}
      </div>
    </>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="text-center py-8 text-subtle">
      <p className="font-medium text-default">{title}</p>
      <p className="text-sm mt-1">{body}</p>
    </div>
  );
}
