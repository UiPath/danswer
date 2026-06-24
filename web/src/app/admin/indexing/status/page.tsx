"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";

import { LoadingAnimation } from "@/components/Loading";
import { NotebookIcon } from "@/components/icons/icons";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { ConnectorIndexingStatus } from "@/lib/types";
import { CCPairIndexingStatusTable } from "./CCPairIndexingStatusTable";
import { AdminPageTitle } from "@/components/admin/Title";
import Link from "next/link";
import { Button, Text } from "@tremor/react";
import { FiRefreshCw } from "react-icons/fi";

const INDEXING_STATUS_URL_BASE = "/api/manage/admin/connector/indexing-status";

// "Show" filter: drives the server-side `disabled` query param so we
// don't ship paused cc-pairs over the wire by default. Environments
// with hundreds of historical (paused) connectors paid for them in
// every 30s poll before this.
type ShowFilter = "enabled" | "disabled" | "all";

function buildIndexingStatusUrl(show: ShowFilter): string {
  if (show === "enabled") return `${INDEXING_STATUS_URL_BASE}?disabled=false`;
  if (show === "disabled") return `${INDEXING_STATUS_URL_BASE}?disabled=true`;
  return INDEXING_STATUS_URL_BASE;
}

function Main() {
  // Deep-link shortcut: ?status=active (or any status value) seeds the
  // table's status filter so links/bookmarks land straight in that view.
  const searchParams = useSearchParams();
  const initialStatusFilter = searchParams.get("status") ?? "all";
  // Default to "enabled" so the initial load is small. Switching the
  // dropdown changes the SWR key (different URL) so SWR re-fetches
  // and caches each variant separately.
  const [show, setShow] = useState<ShowFilter>("enabled");
  // Tracks whether the user just clicked Refresh, so the button only
  // spins on user-initiated refresh — not on every 30s background
  // poll (which would otherwise leave the button perpetually loading).
  const [isManualRefreshing, setIsManualRefreshing] = useState(false);

  const {
    data: indexAttemptData,
    isLoading: indexAttemptIsLoading,
    error: indexAttemptError,
    mutate: refetchIndexAttempt,
  } = useSWR<ConnectorIndexingStatus<any, any>[]>(
    buildIndexingStatusUrl(show),
    errorHandlingFetcher,
    {
      // Background poll cadence. 10s was unnecessarily aggressive for
      // an admin overview page and kept all open admin tabs hammering
      // the endpoint.
      refreshInterval: 30000,
      // Don't poll while the tab is hidden — admins routinely leave
      // the page open in a background tab.
      refreshWhenHidden: false,
      // Re-fetch when the tab regains focus so a stale view doesn't
      // linger after a long absence.
      revalidateOnFocus: true,
    }
  );

  const handleManualRefresh = async () => {
    setIsManualRefreshing(true);
    try {
      await refetchIndexAttempt();
    } finally {
      setIsManualRefreshing(false);
    }
  };

  if (indexAttemptIsLoading) {
    return <LoadingAnimation text="" />;
  }

  if (indexAttemptError || !indexAttemptData) {
    return (
      <div className="text-error">
        {indexAttemptError?.info?.detail || "Error loading indexing history."}
      </div>
    );
  }

  if (indexAttemptData.length === 0 && show === "all") {
    return (
      <Text>
        It looks like you don&apos;t have any connectors setup yet. Visit the{" "}
        <Link className="text-link" href="/admin/add-connector">
          Add Connector
        </Link>{" "}
        page to get started!
      </Text>
    );
  }

  // sort by source name
  indexAttemptData.sort((a, b) => {
    if (a.connector.source < b.connector.source) {
      return -1;
    } else if (a.connector.source > b.connector.source) {
      return 1;
    } else {
      return 0;
    }
  });

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <label className="text-sm flex items-center gap-2">
          <span className="text-text-500">Show</span>
          <select
            value={show}
            onChange={(e) => setShow(e.target.value as ShowFilter)}
            className="h-8 rounded-md border border-border bg-background px-2 text-sm"
          >
            <option value="enabled">Enabled only</option>
            <option value="disabled">Disabled only</option>
            <option value="all">All</option>
          </select>
        </label>
        <Button
          size="xs"
          color="gray"
          variant="secondary"
          icon={FiRefreshCw}
          loading={isManualRefreshing}
          onClick={handleManualRefresh}
        >
          Refresh
        </Button>
      </div>
      {indexAttemptData.length === 0 ? (
        <Text>
          No {show === "enabled" ? "enabled" : "disabled"} connectors.
        </Text>
      ) : (
        <CCPairIndexingStatusTable
          ccPairsIndexingStatuses={indexAttemptData}
          onRefresh={() => refetchIndexAttempt()}
          initialStatusFilter={initialStatusFilter}
        />
      )}
    </>
  );
}

export default function Status() {
  return (
    <div className="mx-auto container">
      <AdminPageTitle
        icon={<NotebookIcon size={32} />}
        title="Existing Connectors"
        farRightElement={
          <Link href="/admin/add-connector">
            <Button color="green" size="xs">
              Add Connector
            </Button>
          </Link>
        }
      />
      {/* useSearchParams() in Main requires a Suspense boundary, or the
          production build fails ("should be wrapped in a suspense boundary"). */}
      <Suspense fallback={<LoadingAnimation text="" />}>
        <Main />
      </Suspense>
    </div>
  );
}
