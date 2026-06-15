"use client";

import {
  Button,
  Table,
  TableHead,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Text,
} from "@tremor/react";
import { CCPairStatus, IndexAttemptStatus } from "@/components/Status";
import { useEffect, useMemo, useState } from "react";
import { PageSelector } from "@/components/PageSelector";
import { timeAgo } from "@/lib/time";
import { ConnectorIndexingStatus, ValidSources } from "@/lib/types";
import { ConnectorTitle } from "@/components/admin/connectors/ConnectorTitle";
import { getDocsProcessedPerMinute } from "@/lib/indexAttempt";
import { useRouter } from "next/navigation";
import { isCurrentlyDeleting } from "@/lib/documentDeletion";
import {
  FiCheck,
  FiChevronDown,
  FiChevronUp,
  FiEdit2,
  FiSearch,
  FiX,
  FiXCircle,
} from "react-icons/fi";
import { getSourceMetadata } from "@/lib/sources";
import { updateConnector } from "@/lib/connector";
import { usePopup } from "@/components/admin/connectors/Popup";

const NUM_IN_PAGE = 10;

function CCPairIndexingStatusDisplay({
  ccPairsIndexingStatus,
}: {
  ccPairsIndexingStatus: ConnectorIndexingStatus<any, any>;
}) {
  if (ccPairsIndexingStatus.connector.disabled) {
    return (
      <CCPairStatus
        status="not_started"
        disabled={true}
        isDeleting={isCurrentlyDeleting(ccPairsIndexingStatus.deletion_attempt)}
      />
    );
  }

  const docsPerMinute = getDocsProcessedPerMinute(
    ccPairsIndexingStatus.latest_index_attempt
  )?.toFixed(2);
  return (
    <>
      <IndexAttemptStatus
        status={ccPairsIndexingStatus.last_status || "not_started"}
        errorMsg={ccPairsIndexingStatus?.latest_index_attempt?.error_msg}
        size="xs"
      />
      {ccPairsIndexingStatus?.latest_index_attempt?.new_docs_indexed &&
      ccPairsIndexingStatus?.latest_index_attempt?.status === "in_progress" ? (
        <div className="text-xs mt-0.5">
          <div>
            <i>Current Run:</i>{" "}
            {ccPairsIndexingStatus.latest_index_attempt.new_docs_indexed} docs
            indexed
          </div>
          <div>
            <i>Speed:</i>{" "}
            {docsPerMinute ? (
              <>{docsPerMinute} docs / min</>
            ) : (
              "calculating rate..."
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}

function ClickableTableRow({
  url,
  children,
  ...props
}: {
  url: string;
  children: React.ReactNode;
  [key: string]: any; // This allows for any additional props
}) {
  const router = useRouter();

  useEffect(() => {
    router.prefetch(url);
  }, [router]);

  const navigate = () => {
    router.push(url);
  };

  return (
    <TableRow {...props} onClick={navigate}>
      {children}
    </TableRow>
  );
}

export function CCPairIndexingStatusTable({
  ccPairsIndexingStatuses,
  onRefresh,
  initialStatusFilter = "all",
}: {
  ccPairsIndexingStatuses: ConnectorIndexingStatus<any, any>[];
  onRefresh?: () => void;
  // Seeds the status dropdown so a deep-link (e.g. ?status=active) lands
  // straight in the filtered view without the user touching the filters.
  initialStatusFilter?: string;
}) {
  const [page, setPage] = useState(1);
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>(initialStatusFilter);
  const [nameSearch, setNameSearch] = useState<string>("");
  // Sort state for the "Last Indexed" column. `none` falls back to the
  // page's default ordering (source-name ascending, set in page.tsx).
  // Click cycle: none → desc → asc → none.
  const [lastIndexedSort, setLastIndexedSort] = useState<
    "none" | "asc" | "desc"
  >("none");
  const [selectedCcPairIds, setSelectedCcPairIds] = useState<Set<number>>(
    new Set()
  );
  const [isMutating, setIsMutating] = useState(false);
  const { popup, setPopup } = usePopup();

  const uniqueSources = useMemo(() => {
    const set = new Set<ValidSources>();
    for (const s of ccPairsIndexingStatuses) {
      set.add(s.connector.source);
    }
    return Array.from(set).sort();
  }, [ccPairsIndexingStatuses]);

  // The status as the row actually presents in the table: a disabled
  // connector reads as "paused" (we render "not_started" with disabled
  // styling), otherwise it's the latest IndexAttempt status. Keeping
  // this in sync with `CCPairIndexingStatusDisplay` ensures the filter
  // matches what the user sees on screen.
  const effectiveStatus = (s: ConnectorIndexingStatus<any, any>): string => {
    if (s.connector.disabled) return "paused";
    return s.last_status || "not_started";
  };

  const filteredRows = useMemo(() => {
    let rows = ccPairsIndexingStatuses;
    if (sourceFilter !== "all") {
      rows = rows.filter((s) => s.connector.source === sourceFilter);
    }
    if (statusFilter === "active") {
      // Composite lens: currently running + queued waiting for a worker
      // slot — the scheduler's live activity in one view.
      rows = rows.filter((s) =>
        ["in_progress", "not_started"].includes(effectiveStatus(s))
      );
    } else if (statusFilter !== "all") {
      rows = rows.filter((s) => effectiveStatus(s) === statusFilter);
    }
    const q = nameSearch.trim().toLowerCase();
    if (q) {
      rows = rows.filter((s) => s.name?.toLowerCase().includes(q));
    }

    if (lastIndexedSort !== "none") {
      // Sort by `last_success` timestamp. Connectors that have never
      // indexed (null) are pushed to the bottom in either direction so
      // they don't crowd out the meaningful comparisons at the top.
      const NEVER = lastIndexedSort === "asc" ? Infinity : -Infinity;
      rows = [...rows].sort((a, b) => {
        const aT = a.last_success ? new Date(a.last_success).getTime() : NEVER;
        const bT = b.last_success ? new Date(b.last_success).getTime() : NEVER;
        return lastIndexedSort === "asc" ? aT - bT : bT - aT;
      });
    }
    return rows;
  }, [
    ccPairsIndexingStatuses,
    sourceFilter,
    statusFilter,
    nameSearch,
    lastIndexedSort,
  ]);

  // Reset page + selection when any filter changes so we never act on hidden rows.
  useEffect(() => {
    setPage(1);
    setSelectedCcPairIds(new Set());
  }, [sourceFilter, statusFilter, nameSearch]);

  // Reset to page 1 when sort changes — otherwise users on page 4 would
  // jump to "page 4 of the new ordering" which is disorienting.
  useEffect(() => {
    setPage(1);
  }, [lastIndexedSort]);

  const cycleLastIndexedSort = () => {
    setLastIndexedSort((prev) =>
      prev === "none" ? "desc" : prev === "desc" ? "asc" : "none"
    );
  };

  const anyFilterActive =
    sourceFilter !== "all" ||
    statusFilter !== "all" ||
    nameSearch.trim() !== "";

  const clearAllFilters = () => {
    setSourceFilter("all");
    setStatusFilter("all");
    setNameSearch("");
  };

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / NUM_IN_PAGE));
  const safePage = Math.min(page, totalPages);
  const rowsForPage = filteredRows.slice(
    NUM_IN_PAGE * (safePage - 1),
    NUM_IN_PAGE * safePage
  );

  const idsOnPage = rowsForPage.map((r) => r.cc_pair_id);
  const allOnPageSelected =
    idsOnPage.length > 0 && idsOnPage.every((id) => selectedCcPairIds.has(id));
  const anyOnPageSelected = idsOnPage.some((id) => selectedCcPairIds.has(id));

  const togglePageSelection = () => {
    setSelectedCcPairIds((prev) => {
      const next = new Set(prev);
      if (allOnPageSelected) {
        idsOnPage.forEach((id) => next.delete(id));
      } else {
        idsOnPage.forEach((id) => next.add(id));
      }
      return next;
    });
  };

  const toggleRowSelection = (id: number) => {
    setSelectedCcPairIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectedRows = ccPairsIndexingStatuses.filter((s) =>
    selectedCcPairIds.has(s.cc_pair_id)
  );
  const selectedCount = selectedRows.length;
  const allSelectedDisabled =
    selectedCount > 0 && selectedRows.every((s) => s.connector.disabled);
  const allSelectedEnabled =
    selectedCount > 0 && selectedRows.every((s) => !s.connector.disabled);

  const bulkSetDisabled = async (disabled: boolean) => {
    if (selectedRows.length === 0 || isMutating) return;
    setIsMutating(true);

    const results = await Promise.allSettled(
      selectedRows.map((s) => updateConnector({ ...s.connector, disabled }))
    );
    const failures = results.filter((r) => r.status === "rejected").length;
    const successes = results.length - failures;

    setPopup({
      message: failures
        ? `${successes} updated, ${failures} failed`
        : `${disabled ? "Paused" : "Re-enabled"} ${successes} connector(s)`,
      type: failures ? "error" : "success",
    });
    setTimeout(() => setPopup(null), 4000);

    setSelectedCcPairIds(new Set());
    setIsMutating(false);
    onRefresh?.();
  };

  return (
    <div>
      {popup}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <label className="text-sm flex items-center gap-2">
          <span className="text-text-500">Source</span>
          <select
            className="border rounded px-2 py-1 bg-background text-sm"
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
          >
            <option value="all">All connector types</option>
            {uniqueSources.map((src) => {
              const meta = getSourceMetadata(src as ValidSources);
              return (
                <option key={src} value={src}>
                  {meta?.displayName || src}
                </option>
              );
            })}
          </select>
        </label>

        <label className="text-sm flex items-center gap-2">
          <span className="text-text-500">Status</span>
          <select
            className="border rounded px-2 py-1 bg-background text-sm"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="all">All statuses</option>
            <option value="active">Active (running + queued)</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
            <option value="in_progress">In progress</option>
            <option value="not_started">Not started</option>
            <option value="paused">Paused</option>
          </select>
        </label>

        <div className="relative">
          <FiSearch
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-500 pointer-events-none"
            size={14}
          />
          <input
            type="search"
            aria-label="Search by connector name"
            placeholder="Search by connector name…"
            className="border rounded pl-8 pr-2 py-1 bg-background text-sm w-64"
            value={nameSearch}
            onChange={(e) => setNameSearch(e.target.value)}
          />
        </div>

        {anyFilterActive && (
          <Button
            size="xs"
            color="gray"
            variant="secondary"
            icon={FiX}
            onClick={clearAllFilters}
          >
            Clear filters
          </Button>
        )}

        <Text className="ml-auto text-sm">
          <b>{filteredRows.length}</b> of{" "}
          <b>{ccPairsIndexingStatuses.length}</b> connectors
          {selectedCount > 0 && (
            <>
              {" · "}
              <b>{selectedCount}</b> selected
            </>
          )}
        </Text>
      </div>

      {selectedCount > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2 px-3 py-2 rounded bg-background-weak border border-border">
          <Text className="text-sm">
            <b>{selectedCount}</b> selected
          </Text>
          <div className="ml-auto flex gap-2">
            <Button
              size="xs"
              color="green"
              disabled={isMutating || allSelectedEnabled}
              onClick={() => bulkSetDisabled(false)}
              tooltip={
                allSelectedEnabled
                  ? "All selected are already enabled"
                  : "Re-enable selected connectors"
              }
            >
              Re-Enable
            </Button>
            <Button
              size="xs"
              color="red"
              disabled={isMutating || allSelectedDisabled}
              onClick={() => bulkSetDisabled(true)}
              tooltip={
                allSelectedDisabled
                  ? "All selected are already paused"
                  : "Pause selected connectors"
              }
            >
              Pause
            </Button>
            <Button
              size="xs"
              color="gray"
              variant="secondary"
              onClick={() => setSelectedCcPairIds(new Set())}
            >
              Clear selection
            </Button>
          </div>
        </div>
      )}

      <Table className="overflow-visible">
        <TableHead>
          <TableRow>
            <TableHeaderCell className="w-8">
              <input
                type="checkbox"
                aria-label="Select all on this page"
                checked={allOnPageSelected}
                ref={(el) => {
                  if (el) {
                    el.indeterminate = !allOnPageSelected && anyOnPageSelected;
                  }
                }}
                onChange={togglePageSelection}
                onClick={(e) => e.stopPropagation()}
              />
            </TableHeaderCell>
            <TableHeaderCell>Connector</TableHeaderCell>
            <TableHeaderCell>Status</TableHeaderCell>
            <TableHeaderCell>Is Public</TableHeaderCell>
            <TableHeaderCell>
              <button
                type="button"
                onClick={cycleLastIndexedSort}
                className="flex items-center gap-1 hover:text-emphasis cursor-pointer select-none"
                aria-label={
                  lastIndexedSort === "none"
                    ? "Sort by Last Indexed"
                    : lastIndexedSort === "desc"
                      ? "Last Indexed sorted newest first — click to flip"
                      : "Last Indexed sorted oldest first — click to clear"
                }
                title="Click to sort by Last Indexed"
              >
                <span>Last Indexed</span>
                {lastIndexedSort === "desc" ? (
                  <FiChevronDown size={14} />
                ) : lastIndexedSort === "asc" ? (
                  <FiChevronUp size={14} />
                ) : (
                  // Neutral indicator when not sorted — same icon as
                  // the active state but at low opacity so the column
                  // looks "sortable" without faking a direction.
                  <FiChevronDown size={14} className="opacity-30" />
                )}
              </button>
            </TableHeaderCell>
            <TableHeaderCell>Docs Indexed</TableHeaderCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rowsForPage.map((ccPairsIndexingStatus) => {
            const id = ccPairsIndexingStatus.cc_pair_id;
            const isSelected = selectedCcPairIds.has(id);
            return (
              <ClickableTableRow
                url={`/admin/connector/${id}`}
                key={id}
                className={
                  "hover:bg-hover-light bg-background cursor-pointer relative"
                }
              >
                <TableCell
                  className="w-8"
                  onClick={(e: React.MouseEvent) => e.stopPropagation()}
                >
                  <input
                    type="checkbox"
                    aria-label={`Select ${ccPairsIndexingStatus.name}`}
                    checked={isSelected}
                    onChange={() => toggleRowSelection(id)}
                    onClick={(e) => e.stopPropagation()}
                  />
                </TableCell>
                <TableCell>
                  <div className="flex my-auto">
                    <FiEdit2 className="mr-4 my-auto" />
                    <div className="whitespace-normal break-all max-w-3xl">
                      <ConnectorTitle
                        connector={ccPairsIndexingStatus.connector}
                        ccPairId={ccPairsIndexingStatus.cc_pair_id}
                        ccPairName={ccPairsIndexingStatus.name}
                      />
                    </div>
                  </div>
                </TableCell>
                <TableCell>
                  <CCPairIndexingStatusDisplay
                    ccPairsIndexingStatus={ccPairsIndexingStatus}
                  />
                </TableCell>
                <TableCell>
                  {ccPairsIndexingStatus.public_doc ? (
                    <FiCheck className="my-auto text-emerald-600" size="18" />
                  ) : (
                    <FiXCircle className="my-auto text-red-600" />
                  )}
                </TableCell>
                <TableCell>
                  {timeAgo(ccPairsIndexingStatus?.last_success) || "-"}
                </TableCell>
                <TableCell>{ccPairsIndexingStatus.docs_indexed}</TableCell>
              </ClickableTableRow>
            );
          })}
        </TableBody>
      </Table>
      {filteredRows.length > NUM_IN_PAGE && (
        <div className="mt-3 flex">
          <div className="mx-auto">
            <PageSelector
              totalPages={totalPages}
              currentPage={safePage}
              onPageChange={(newPage) => {
                setPage(newPage);
                window.scrollTo({
                  top: 0,
                  left: 0,
                  behavior: "smooth",
                });
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
