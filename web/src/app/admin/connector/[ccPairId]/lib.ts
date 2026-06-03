export function buildCCPairInfoUrl(ccPairId: string | number) {
  return `/api/manage/admin/cc-pair/${ccPairId}`;
}

export function buildIndexAttemptsUrl(
  ccPairId: string | number,
  page: number,
  pageSize: number
) {
  return `/api/manage/admin/cc-pair/${ccPairId}/index-attempts?page=${page}&page_size=${pageSize}`;
}
