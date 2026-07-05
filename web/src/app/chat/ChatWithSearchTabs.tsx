"use client";

import { useContext, useState } from "react";
import { ChatPage } from "./ChatPage";
import { AutoSearch, autoSearchVisible } from "../auto-search/AutoSearch";
import { ReferralLogger } from "./ReferralLogger";
import { SettingsContext } from "@/components/settings/SettingsProvider";

// Tabbed shell over the chat page: a "Chat" tab (the existing UX, untouched) and
// a "Search" tab (the auto-routed one-shot box). When the rollout doesn't grant
// the user the Search tab, this renders ChatPage exactly as before — zero change.
//
// ChatPage stays MOUNTED while on the Search tab (hidden via display:none) so an
// in-progress conversation isn't lost when toggling. AutoSearch renders as a
// fixed overlay above the hidden chat. The backend enforces the rollout gate on
// /query/auto-search regardless of this UI.
export function ChatWithSearchTabs({
  documentSidebarInitialWidth,
  defaultSelectedPersonaId,
  userRole,
}: {
  documentSidebarInitialWidth?: number;
  defaultSelectedPersonaId?: number;
  userRole: string | null;
}) {
  const settings = useContext(SettingsContext)?.settings;
  const searchEnabled = autoSearchVisible(
    settings?.auto_search_rollout,
    userRole
  );
  const [tab, setTab] = useState<"chat" | "search">("chat");

  const chatPage = (
    <ChatPage
      documentSidebarInitialWidth={documentSidebarInitialWidth}
      defaultSelectedPersonaId={defaultSelectedPersonaId}
    />
  );

  // No Search tab for this user — render chat exactly as before (plus the
  // invisible referral beacon, which is independent of the tabs).
  if (!searchEnabled) {
    return (
      <>
        <ReferralLogger />
        {chatPage}
      </>
    );
  }

  return (
    <>
      <ReferralLogger />
      {/* Tab pill, floating top-center above both views. */}
      <div className="fixed top-2 left-1/2 -translate-x-1/2 z-50 flex gap-1 rounded-full border border-border-medium bg-background p-1 shadow-lg">
        <TabButton active={tab === "chat"} onClick={() => setTab("chat")}>
          Chat
        </TabButton>
        <TabButton active={tab === "search"} onClick={() => setTab("search")}>
          Search
        </TabButton>
      </div>

      {/* Keep ChatPage mounted so its state survives a tab switch. */}
      <div style={{ display: tab === "chat" ? "contents" : "none" }}>
        {chatPage}
      </div>

      {tab === "search" && (
        <div className="fixed inset-0 z-40 bg-background">
          <AutoSearch userRole={userRole} />
        </div>
      )}
    </>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-4 py-1 text-sm transition-colors ${
        active
          ? "bg-accent text-white"
          : "text-subtle hover:bg-hover"
      }`}
    >
      {children}
    </button>
  );
}
