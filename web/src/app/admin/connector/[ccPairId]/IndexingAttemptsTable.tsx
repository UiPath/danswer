"use client";

import {
  Table,
  TableHead,
  TableRow,
  TableHeaderCell,
  TableBody,
  TableCell,
  Text,
  Button,
  Divider,
} from "@tremor/react";
import { IndexAttemptStatus } from "@/components/Status";
import { CCPairFullInfo } from "./types";
import { useState } from "react";
import { PageSelector } from "@/components/PageSelector";
import { localizeAndPrettify } from "@/lib/time";
import { getDocsProcessedPerMinute } from "@/lib/indexAttempt";
import { Modal } from "@/components/Modal";
import { CheckmarkIcon, CopyIcon } from "@/components/icons/icons";
import { updateIndexAttemptPriority } from "@/lib/connector";
import { mutate } from "swr";
import { buildCCPairInfoUrl } from "./lib";
import { usePopup } from "@/components/admin/connectors/Popup";

const NUM_IN_PAGE = 8;

export function IndexingAttemptsTable({ ccPair }: { ccPair: CCPairFullInfo }) {
  const [page, setPage] = useState(1);
  const [indexAttemptTracePopupId, setIndexAttemptTracePopupId] = useState<
    number | null
  >(null);
  const indexAttemptToDisplayTraceFor = ccPair.index_attempts.find(
    (indexAttempt) => indexAttempt.id === indexAttemptTracePopupId
  );
  const [copyClicked, setCopyClicked] = useState(false);
  const { popup, setPopup } = usePopup();
  const [updatingPriorityId, setUpdatingPriorityId] = useState<number | null>(
    null
  );

  async function bumpPriority(indexAttemptId: number, nextValue: number) {
    setUpdatingPriorityId(indexAttemptId);
    const errorMsg = await updateIndexAttemptPriority(
      indexAttemptId,
      Math.max(0, Math.min(100, Math.floor(nextValue)))
    );
    setUpdatingPriorityId(null);
    if (errorMsg) {
      setPopup({ message: errorMsg, type: "error" });
    } else {
      setPopup({
        message: `Priority updated to ${nextValue}`,
        type: "success",
      });
    }
    setTimeout(() => setPopup(null), 3000);
    mutate(buildCCPairInfoUrl(ccPair.id));
  }

  return (
    <>
      {indexAttemptToDisplayTraceFor &&
        indexAttemptToDisplayTraceFor.full_exception_trace && (
          <Modal
            width="w-4/6"
            className="h-5/6 overflow-y-hidden flex flex-col"
            title="Full Exception Trace"
            onOutsideClick={() => setIndexAttemptTracePopupId(null)}
          >
            <div className="overflow-y-auto mb-6">
              <div className="mb-6">
                {!copyClicked ? (
                  <div
                    onClick={() => {
                      navigator.clipboard.writeText(
                        indexAttemptToDisplayTraceFor.full_exception_trace!
                      );
                      setCopyClicked(true);
                      setTimeout(() => setCopyClicked(false), 2000);
                    }}
                    className="flex w-fit cursor-pointer hover:bg-hover-light p-2 border-border border rounded"
                  >
                    Copy full trace
                    <CopyIcon className="ml-2 my-auto" />
                  </div>
                ) : (
                  <div className="flex w-fit hover:bg-hover-light p-2 border-border border rounded cursor-default">
                    Copied to clipboard
                    <CheckmarkIcon
                      className="my-auto ml-2 flex flex-shrink-0 text-success"
                      size={16}
                    />
                  </div>
                )}
              </div>
              <div className="whitespace-pre-wrap">
                {indexAttemptToDisplayTraceFor.full_exception_trace}
              </div>
            </div>
          </Modal>
        )}
      {popup}
      <Table>
        <TableHead>
          <TableRow>
            <TableHeaderCell>Time Started</TableHeaderCell>
            <TableHeaderCell>Status</TableHeaderCell>
            <TableHeaderCell>Priority</TableHeaderCell>
            <TableHeaderCell>New Doc Cnt</TableHeaderCell>
            <TableHeaderCell>Total Doc Cnt</TableHeaderCell>
            <TableHeaderCell>Error Msg</TableHeaderCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {ccPair.index_attempts
            .slice(NUM_IN_PAGE * (page - 1), NUM_IN_PAGE * page)
            .map((indexAttempt) => {
              const docsPerMinute =
                getDocsProcessedPerMinute(indexAttempt)?.toFixed(2);
              const priority = indexAttempt.indexing_priority ?? 0;
              const isNotStarted = indexAttempt.status === "not_started";
              const isUpdating = updatingPriorityId === indexAttempt.id;
              return (
                <TableRow key={indexAttempt.id}>
                  <TableCell>
                    {indexAttempt.time_started
                      ? localizeAndPrettify(indexAttempt.time_started)
                      : "-"}
                  </TableCell>
                  <TableCell>
                    <IndexAttemptStatus
                      status={indexAttempt.status || "not_started"}
                      size="xs"
                    />
                    {docsPerMinute && (
                      <div className="text-xs mt-1">
                        {docsPerMinute} docs / min
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {isNotStarted ? (
                      <div className="flex items-center gap-1">
                        <button
                          className="px-1.5 py-0.5 border rounded text-xs hover:bg-hover-light disabled:opacity-50"
                          disabled={isUpdating || priority <= 0}
                          onClick={() =>
                            bumpPriority(indexAttempt.id, priority - 10)
                          }
                          title="Decrease priority by 10"
                        >
                          −10
                        </button>
                        <span
                          className={
                            priority > 0
                              ? "text-xs font-semibold px-2 py-0.5 rounded bg-emerald-100 text-emerald-800"
                              : "text-xs px-2 py-0.5 text-subtle"
                          }
                        >
                          {priority}
                        </span>
                        <button
                          className="px-1.5 py-0.5 border rounded text-xs hover:bg-hover-light disabled:opacity-50"
                          disabled={isUpdating || priority >= 100}
                          onClick={() =>
                            bumpPriority(indexAttempt.id, priority + 10)
                          }
                          title="Increase priority by 10"
                        >
                          +10
                        </button>
                      </div>
                    ) : priority > 0 ? (
                      <span className="text-xs font-semibold px-2 py-0.5 rounded bg-emerald-100 text-emerald-800">
                        {priority}
                      </span>
                    ) : (
                      <span className="text-xs text-subtle">-</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex">
                      <div className="text-right">
                        <div>{indexAttempt.new_docs_indexed}</div>
                        {indexAttempt.docs_removed_from_index > 0 && (
                          <div className="text-xs w-52 text-wrap flex italic overflow-hidden whitespace-normal px-1">
                            (also removed {indexAttempt.docs_removed_from_index}{" "}
                            docs that were detected as deleted in the source)
                          </div>
                        )}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>{indexAttempt.total_docs_indexed}</TableCell>
                  <TableCell>
                    <div>
                      <Text className="flex flex-wrap whitespace-normal">
                        {indexAttempt.error_msg || "-"}
                      </Text>
                      {indexAttempt.full_exception_trace && (
                        <div
                          onClick={() => {
                            setIndexAttemptTracePopupId(indexAttempt.id);
                          }}
                          className="mt-2 text-link cursor-pointer select-none"
                        >
                          View Full Trace
                        </div>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
        </TableBody>
      </Table>
      {ccPair.index_attempts.length > NUM_IN_PAGE && (
        <div className="mt-3 flex">
          <div className="mx-auto">
            <PageSelector
              totalPages={Math.ceil(ccPair.index_attempts.length / NUM_IN_PAGE)}
              currentPage={page}
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
    </>
  );
}
