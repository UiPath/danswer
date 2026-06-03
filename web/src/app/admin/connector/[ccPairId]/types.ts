import {
  Connector,
  Credential,
  DeletionAttemptSnapshot,
  IndexAttemptSnapshot,
} from "@/lib/types";

export interface CCPairFullInfo {
  id: number;
  name: string;
  num_docs_indexed: number;
  connector: Connector<any>;
  credential: Credential<any>;
  // Full attempt history is fetched (paginated) separately; the detail page
  // only needs the most-recent attempt + a total count.
  latest_index_attempt: IndexAttemptSnapshot | null;
  num_index_attempts: number;
  latest_deletion_attempt: DeletionAttemptSnapshot | null;
}

export interface PaginatedIndexAttempts {
  index_attempts: IndexAttemptSnapshot[];
  page: number;
  total_pages: number;
  total_count: number;
}
