"use client";

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

const INDEXING_STATUS_URL = "/api/manage/admin/connector/indexing-status";

function Main() {
  const {
    data: indexAttemptData,
    isLoading: indexAttemptIsLoading,
    isValidating: indexAttemptIsValidating,
    error: indexAttemptError,
    mutate: refetchIndexAttempt,
  } = useSWR<ConnectorIndexingStatus<any, any>[]>(
    INDEXING_STATUS_URL,
    errorHandlingFetcher,
    { refreshInterval: 10000 } // 10 seconds
  );

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

  if (indexAttemptData.length === 0) {
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
      <div className="flex justify-end mb-3">
        <Button
          size="xs"
          color="gray"
          variant="secondary"
          icon={FiRefreshCw}
          loading={indexAttemptIsValidating}
          onClick={() => refetchIndexAttempt()}
        >
          Refresh
        </Button>
      </div>
      <CCPairIndexingStatusTable
        ccPairsIndexingStatuses={indexAttemptData}
        onRefresh={() => refetchIndexAttempt()}
      />
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
      <Main />
    </div>
  );
}
