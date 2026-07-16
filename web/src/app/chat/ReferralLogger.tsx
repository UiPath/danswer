"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";

// Fire-and-forget referral beacon: when the chat page is opened from an external
// link carrying a `utm_source` (e.g. a per-channel Slack "Ask Darwin" workflow),
// POST the UTM + assistant params once so the backend can record the landing in
// the chat_referral table. Renders nothing.
//
// Fires at most once per distinct landing URL (guarded via sessionStorage) so SPA
// re-renders / tab switches within the same page load don't double-count. Failures
// are swallowed — analytics must never disrupt the chat experience.
export function ReferralLogger() {
  const searchParams = useSearchParams();

  useEffect(() => {
    const utmSource = searchParams.get("utm_source");
    if (!utmSource) return;

    const dedupeKey = `referral-logged:${searchParams.toString()}`;
    try {
      if (sessionStorage.getItem(dedupeKey)) return;
      sessionStorage.setItem(dedupeKey, "1");
    } catch {
      // sessionStorage unavailable (private mode etc.) — proceed without dedupe.
    }

    fetch("/api/chat/referral", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      keepalive: true, // survive the navigation/pageload that triggered it
      body: JSON.stringify({
        utm_source: utmSource,
        utm_medium: searchParams.get("utm_medium"),
        utm_campaign: searchParams.get("utm_campaign"),
        utm_channel: searchParams.get("utm_channel"),
        assistant: searchParams.get("assistant"),
      }),
    }).catch(() => {
      // best-effort; ignore
    });
  }, [searchParams]);

  return null;
}
