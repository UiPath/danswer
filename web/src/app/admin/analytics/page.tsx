"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  AreaChart,
  BarList,
  Card,
  DateRangePicker,
  DateRangePickerValue,
  Grid,
  Metric,
  Text,
  Title,
} from "@tremor/react";
import { FiBarChart2 } from "react-icons/fi";

import { AdminPageTitle } from "@/components/admin/Title";
import { LoadingAnimation } from "@/components/Loading";
import { errorHandlingFetcher } from "@/lib/fetcher";

// Backend response shapes — keep in sync with
// `backend/danswer/server/analytics/api.py` Pydantic models. `date` is a
// Python `datetime.date` which Pydantic serializes as a YYYY-MM-DD string.
interface QueryAnalyticsRow {
  total_queries: number;
  total_likes: number;
  total_dislikes: number;
  // Resolved-button presses on Slackbot answers — counted as a positive
  // signal alongside likes for "strict NPS". Backed by
  // chat_feedback.predefined_feedback = 'resolved'.
  total_resolved: number;
  // "I need more help" button presses — counted as a negative signal
  // alongside dislikes. Backed by chat_feedback.required_followup = TRUE.
  total_needs_help: number;
  date: string;
}

interface UserAnalyticsRow {
  total_active_users: number;
  date: string;
}

interface DanswerbotAnalyticsRow {
  total_queries: number;
  auto_resolved: number;
  date: string;
}

interface TotalDocsResponse {
  total_docs_indexed: number;
  unique_docs: number;
}

interface DocsPerSourceRow {
  source: string;
  docs_indexed: number;
}

interface SlackChannelsResponse {
  total_configs: number;
  enabled_channels: number;
}

type Granularity = "day" | "month";

const DEFAULT_LOOKBACK_DAYS = 30;

function defaultRange(): DateRangePickerValue {
  const now = new Date();
  const from = new Date(now);
  from.setDate(from.getDate() - DEFAULT_LOOKBACK_DAYS);
  return { from, to: now };
}

function buildURL(path: string, range?: DateRangePickerValue): string {
  // Skip the URL params if the picker is empty — the backend defaults
  // to a 30-day window when start/end are absent.
  if (!range) return `/api${path}`;
  const params = new URLSearchParams();
  if (range.from) params.set("start", range.from.toISOString());
  if (range.to) params.set("end", range.to.toISOString());
  const qs = params.toString();
  return qs ? `/api${path}?${qs}` : `/api${path}`;
}

function monthKey(isoDate: string): string {
  // "2026-05-14" → "2026-05". Pydantic always serializes dates this way,
  // so a slice is sufficient.
  return isoDate.slice(0, 7);
}

// Bucket daily rows into monthly. Numeric fields are summed unless their
// name is in `peakFields`, in which case we take the max within the
// month — needed for distinct-counts (e.g. active users) where summing
// would double-count people who logged in on multiple days.
function bucketToMonth<R extends Record<string, unknown> & { date: string }>(
  rows: R[],
  peakFields: ReadonlySet<string> = new Set()
): R[] {
  const buckets = new Map<string, R>();
  for (const row of rows) {
    const key = monthKey(row.date);
    const existing = buckets.get(key);
    if (!existing) {
      buckets.set(key, { ...row, date: key });
      continue;
    }
    const merged: Record<string, unknown> = { ...existing };
    for (const [k, v] of Object.entries(row)) {
      if (k === "date") continue;
      if (typeof v !== "number") continue;
      const prev = (existing as Record<string, unknown>)[k];
      if (typeof prev !== "number") {
        merged[k] = v;
        continue;
      }
      merged[k] = peakFields.has(k) ? Math.max(prev, v) : prev + v;
    }
    buckets.set(key, merged as R);
  }
  return Array.from(buckets.values()).sort((a, b) =>
    a.date < b.date ? -1 : a.date > b.date ? 1 : 0
  );
}

export default function AnalyticsPage() {
  const [range, setRange] = useState<DateRangePickerValue>(defaultRange());
  const [granularity, setGranularity] = useState<Granularity>("day");

  const swrOpts = { keepPreviousData: true };

  // Time-series endpoints — driven by the date range picker.
  const {
    data: queryData,
    isLoading: queryLoading,
    error: queryErr,
  } = useSWR<QueryAnalyticsRow[]>(
    buildURL("/analytics/admin/query", range),
    errorHandlingFetcher,
    swrOpts
  );

  const {
    data: userData,
    isLoading: userLoading,
    error: userErr,
  } = useSWR<UserAnalyticsRow[]>(
    buildURL("/analytics/admin/user", range),
    errorHandlingFetcher,
    swrOpts
  );

  const {
    data: botData,
    isLoading: botLoading,
    error: botErr,
  } = useSWR<DanswerbotAnalyticsRow[]>(
    buildURL("/analytics/admin/danswerbot", range),
    errorHandlingFetcher,
    swrOpts
  );

  // Snapshot endpoints — independent of date range, refresh on mount only.
  const { data: totalDocs, error: totalDocsErr } = useSWR<TotalDocsResponse>(
    buildURL("/analytics/admin/total-docs"),
    errorHandlingFetcher,
    swrOpts
  );
  const { data: docsBySource, error: docsBySourceErr } = useSWR<
    DocsPerSourceRow[]
  >(
    buildURL("/analytics/admin/docs-per-source"),
    errorHandlingFetcher,
    swrOpts
  );
  const { data: slackChannels, error: slackChannelsErr } =
    useSWR<SlackChannelsResponse>(
      buildURL("/analytics/admin/slack-channels"),
      errorHandlingFetcher,
      swrOpts
    );

  const isInitialLoading =
    (queryLoading && !queryData) ||
    (userLoading && !userData) ||
    (botLoading && !botData);
  const hasError =
    queryErr ||
    userErr ||
    botErr ||
    totalDocsErr ||
    docsBySourceErr ||
    slackChannelsErr;

  // KPI aggregates — collapse the time-series data down to single
  // numbers for the cards above the charts.
  const kpis = useMemo(() => {
    const totalQueries = (queryData ?? []).reduce(
      (s, r) => s + r.total_queries,
      0
    );
    const totalLikes = (queryData ?? []).reduce(
      (s, r) => s + r.total_likes,
      0
    );
    const totalDislikes = (queryData ?? []).reduce(
      (s, r) => s + r.total_dislikes,
      0
    );
    const totalResolved = (queryData ?? []).reduce(
      (s, r) => s + (r.total_resolved ?? 0),
      0
    );
    const totalNeedsHelp = (queryData ?? []).reduce(
      (s, r) => s + (r.total_needs_help ?? 0),
      0
    );

    const positivity =
      totalLikes + totalDislikes > 0
        ? Math.round((totalLikes / (totalLikes + totalDislikes)) * 100)
        : null;

    // Strict NPS — explicit signals only. Promoters = likes + resolved
    // (the Slackbot's "I'm all set!" / "Mark Resolved" buttons).
    // Detractors = dislikes + needs_help (the "I need more help" button).
    // Excludes messages with no feedback row at all (silent users).
    // Range -100..+100. NPS is well-defined only when at least one
    // explicit signal exists in the range.
    const strictPromoters = totalLikes + totalResolved;
    const strictDetractors = totalDislikes + totalNeedsHelp;
    const strictDenominator = strictPromoters + strictDetractors;
    const npsStrict =
      strictDenominator > 0
        ? Math.round(
            ((strictPromoters - strictDetractors) / strictDenominator) * 100
          )
        : null;


    // "Peak daily" instead of sum-of-distinct because the per-day
    // distinct counts can't be added across days without double-counting
    // the same user. Peak gives a meaningful "biggest day" number.
    const peakActiveUsers = (userData ?? []).reduce(
      (peak, r) => Math.max(peak, r.total_active_users),
      0
    );

    const totalBotQueries = (botData ?? []).reduce(
      (s, r) => s + r.total_queries,
      0
    );
    const totalAutoResolved = (botData ?? []).reduce(
      (s, r) => s + r.auto_resolved,
      0
    );
    const autoResolvePct =
      totalBotQueries > 0
        ? Math.round((totalAutoResolved / totalBotQueries) * 100)
        : null;

    return {
      totalQueries,
      peakActiveUsers,
      autoResolvePct,
      positivity,
      npsStrict,
      strictDenominator,
    };
  }, [queryData, userData, botData]);

  // Combined query-performance series: queries (from /query) overlaid
  // with active users (from /user). Date join is on ISO date string.
  const queryPerformanceDaily = useMemo(() => {
    const userByDate = new Map<string, number>();
    (userData ?? []).forEach((r) =>
      userByDate.set(r.date, r.total_active_users)
    );
    return (queryData ?? []).map((r) => ({
      date: r.date,
      Queries: r.total_queries,
      "Active Users": userByDate.get(r.date) ?? 0,
    }));
  }, [queryData, userData]);

  const feedbackDaily = useMemo(
    () =>
      (queryData ?? []).map((r) => ({
        date: r.date,
        Likes: r.total_likes,
        Dislikes: r.total_dislikes,
      })),
    [queryData]
  );

  // Apply granularity. Active Users is summed-by-distinct so monthly
  // requires PEAK (sum would double-count). Other fields are simple sums.
  const queryPerformanceData = useMemo(
    () =>
      granularity === "day"
        ? queryPerformanceDaily
        : bucketToMonth(queryPerformanceDaily, new Set(["Active Users"])),
    [queryPerformanceDaily, granularity]
  );
  const feedbackData = useMemo(
    () => (granularity === "day" ? feedbackDaily : bucketToMonth(feedbackDaily)),
    [feedbackDaily, granularity]
  );

  const docsBySourceBars = useMemo(
    () =>
      (docsBySource ?? [])
        .filter((r) => r.docs_indexed > 0)
        .map((r) => ({ name: r.source, value: r.docs_indexed })),
    [docsBySource]
  );

  return (
    <div className="mx-auto container">
      <AdminPageTitle icon={<FiBarChart2 size={32} />} title="Analytics" />

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <Text>Date range:</Text>
        <DateRangePicker
          className="max-w-md"
          value={range}
          onValueChange={setRange}
          enableSelect={true}
          enableClear={false}
        />

        <Text className="ml-2">Granularity:</Text>
        <select
          className="border rounded px-2 py-1 bg-background text-sm"
          value={granularity}
          onChange={(e) => setGranularity(e.target.value as Granularity)}
        >
          <option value="day">Daily</option>
          <option value="month">Monthly</option>
        </select>
      </div>

      {hasError && (
        <Card className="mb-6">
          <Text className="text-error">
            Error loading analytics data. Make sure you&apos;re logged in as an
            admin and the backend is reachable.
          </Text>
        </Card>
      )}

      {isInitialLoading ? (
        <LoadingAnimation text="Loading analytics" />
      ) : (
        <>
          {/* Top row: range-scoped KPIs */}
          <Grid
            numItems={1}
            numItemsSm={2}
            numItemsLg={4}
            className="gap-4 mb-4"
          >
            <Card>
              <Text>Total Queries (range)</Text>
              <Metric>{kpis.totalQueries.toLocaleString()}</Metric>
            </Card>
            <Card>
              <Text>Peak Daily Active Users</Text>
              <Metric>{kpis.peakActiveUsers.toLocaleString()}</Metric>
            </Card>
            <Card>
              <Text>Auto-Resolution Rate (Slack)</Text>
              <Metric>
                {kpis.autoResolvePct !== null
                  ? `${kpis.autoResolvePct}%`
                  : "—"}
              </Metric>
            </Card>
            <Card>
              <Text>NPS — strict</Text>
              <Metric>
                {kpis.npsStrict !== null
                  ? `${kpis.npsStrict > 0 ? "+" : ""}${kpis.npsStrict}`
                  : "—"}
              </Metric>
              <Text className="mt-1 text-xs">
                (likes + resolved) vs (dislikes + needs-help). N=
                {kpis.strictDenominator}
              </Text>
            </Card>
          </Grid>

          {/* Snapshot KPIs — current state, independent of date range */}
          <Grid
            numItems={1}
            numItemsSm={2}
            numItemsLg={4}
            className="gap-4 mb-6"
          >
            <Card>
              <Text>Total Docs Indexed</Text>
              <Metric>
                {totalDocs
                  ? totalDocs.total_docs_indexed.toLocaleString()
                  : "—"}
              </Metric>
              <Text className="mt-1 text-xs">
                {totalDocs
                  ? `${totalDocs.unique_docs.toLocaleString()} unique`
                  : ""}
              </Text>
            </Card>
            <Card>
              <Text>Slack Channels Enabled</Text>
              <Metric>
                {slackChannels
                  ? slackChannels.enabled_channels.toLocaleString()
                  : "—"}
              </Metric>
              <Text className="mt-1 text-xs">
                {slackChannels
                  ? `across ${slackChannels.total_configs} config(s)`
                  : ""}
              </Text>
            </Card>
            <Card>
              <Text>Positive Feedback %</Text>
              <Metric>
                {kpis.positivity !== null ? `${kpis.positivity}%` : "—"}
              </Metric>
              <Text className="mt-1 text-xs">over selected date range</Text>
            </Card>
            <Card>
              <Text>Sources Active</Text>
              <Metric>
                {docsBySource ? docsBySourceBars.length.toLocaleString() : "—"}
              </Metric>
              <Text className="mt-1 text-xs">
                {docsBySource
                  ? `of ${docsBySource.length} configured`
                  : ""}
              </Text>
            </Card>
          </Grid>

          <Grid numItems={1} numItemsLg={2} className="gap-4 mb-6">
            <Card>
              <Title>Users and Query Trend</Title>
              <Text>
                {granularity === "day"
                  ? "Daily"
                  : "Monthly (Active Users = peak day)"}{" "}
                assistant replies overlaid with active users
              </Text>
              <AreaChart
                className="mt-4 h-72"
                data={queryPerformanceData}
                index="date"
                categories={["Queries", "Active Users"]}
                colors={["blue", "green"]}
                showLegend={true}
                noDataText="No data in this date range"
              />
            </Card>

            <Card>
              <Title>Feedback Trend</Title>
              <Text>
                {granularity === "day" ? "Daily" : "Monthly"} thumbs up vs
                thumbs down
              </Text>
              <AreaChart
                className="mt-4 h-72"
                data={feedbackData}
                index="date"
                categories={["Likes", "Dislikes"]}
                colors={["emerald", "rose"]}
                showLegend={true}
                noDataText="No feedback in this date range"
              />
            </Card>
          </Grid>

          <Card className="mb-6">
            <Title>Docs Indexed by Source</Title>
            <Text>Snapshot — sum across all cc-pairs per source type</Text>
            {docsBySourceBars.length > 0 ? (
              <BarList
                className="mt-4"
                data={docsBySourceBars}
                valueFormatter={(n: number) => n.toLocaleString()}
              />
            ) : (
              <Text className="mt-4">No documents indexed yet.</Text>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
