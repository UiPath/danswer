import { ChatSession, CHAT_SESSION_PAGE_SIZE } from "../interfaces";
import { getChatHistoryBoundaries } from "../lib";
import { ChatSessionDisplay } from "./ChatSessionDisplay";
import { removeChatFromFolder } from "../folders/FolderManagement";
import { FolderList } from "../folders/FolderList";
import { Folder } from "../folders/interfaces";
import { CHAT_SESSION_ID_KEY, FOLDER_ID_KEY } from "@/lib/drag/constants";
import { usePopup } from "@/components/admin/connectors/Popup";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { FiChevronDown, FiChevronRight, FiLoader } from "react-icons/fi";

type BucketKey = "today" | "prev7" | "prev30" | "older";

interface BucketData {
  sessions: ChatSession[];
  pagesLoaded: number;
  hasMore: boolean;
  loading: boolean;
  loaded: boolean;
}

// "today" is loaded by default (server-seeded) and always shown; the rest are
// collapsed and lazy-load when expanded. Order = newest to oldest.
const BUCKETS: { key: BucketKey; title: string; collapsible: boolean }[] = [
  { key: "today", title: "Today", collapsible: false },
  { key: "prev7", title: "Previous 7 Days", collapsible: true },
  { key: "prev30", title: "Previous 30 Days", collapsible: true },
  { key: "older", title: "Over 30 days ago", collapsible: true },
];

const EMPTY_BUCKET: BucketData = {
  sessions: [],
  pagesLoaded: 0,
  hasMore: false,
  loading: false,
  loaded: false,
};

// Append a freshly-fetched page, skipping ids already present (pages can overlap
// at boundaries, and the server-merged current chat may already be in state).
function dedupeAppend(
  prev: ChatSession[],
  next: ChatSession[]
): ChatSession[] {
  const seen = new Set(prev.map((s) => s.id));
  const merged = [...prev];
  for (const s of next) {
    if (!seen.has(s.id)) {
      merged.push(s);
    }
  }
  return merged;
}

export function ChatTab({
  existingChats,
  hasMoreChats,
  currentChatId,
  folders,
  openedFolders,
}: {
  existingChats: ChatSession[];
  hasMoreChats: boolean;
  currentChatId?: number;
  folders: Folder[];
  openedFolders: { [key: number]: boolean };
}) {
  const { setPopup } = usePopup();
  const router = useRouter();
  const [isDragOver, setIsDragOver] = useState<boolean>(false);

  // Date cutoffs that mirror groupSessionsByDateRange. Computed once so the
  // window doesn't shift mid-session.
  const [boundaries] = useState(() => getChatHistoryBoundaries());
  const oneDayMs = new Date(boundaries.oneDayAgo).getTime();

  // Keep only genuinely-today sessions from the server seed (drops the
  // current-chat merge fetchChatData prepends, which may be an older session).
  const todaySeed = useCallback(
    (chats: ChatSession[]) =>
      chats.filter((c) => new Date(c.time_created).getTime() >= oneDayMs),
    [oneDayMs]
  );

  const [buckets, setBuckets] = useState<Record<BucketKey, BucketData>>(() => ({
    today: {
      sessions: todaySeed(existingChats),
      pagesLoaded: 1,
      hasMore: hasMoreChats,
      loading: false,
      loaded: true,
    },
    prev7: { ...EMPTY_BUCKET },
    prev30: { ...EMPTY_BUCKET },
    older: { ...EMPTY_BUCKET },
  }));
  const [expanded, setExpanded] = useState<Record<BucketKey, boolean>>({
    today: true,
    prev7: false,
    prev30: false,
    older: false,
  });

  // Mirror buckets to a ref so click handlers read the freshest offset/loaded
  // without being re-created (and re-bound) on every render.
  const bucketsRef = useRef(buckets);
  useEffect(() => {
    bucketsRef.current = buckets;
  }, [buckets]);

  // Re-seed Today when the server prop changes (router.refresh on rename / new
  // chat / chat switch). Older buckets are left alone so an expanded section
  // isn't collapsed (and re-fetched) by an unrelated refresh.
  useEffect(() => {
    setBuckets((prev) => ({
      ...prev,
      today: {
        sessions: todaySeed(existingChats),
        pagesLoaded: 1,
        hasMore: hasMoreChats,
        loading: false,
        loaded: true,
      },
    }));
  }, [existingChats, hasMoreChats, todaySeed]);

  const queryFor = useCallback(
    (key: BucketKey, offset: number): string => {
      const params = new URLSearchParams({
        limit: String(CHAT_SESSION_PAGE_SIZE),
        offset: String(offset),
      });
      if (key === "today") {
        params.set("start_time", boundaries.oneDayAgo);
      } else if (key === "prev7") {
        params.set("start_time", boundaries.sevenDaysAgo);
        params.set("end_time", boundaries.oneDayAgo);
      } else if (key === "prev30") {
        params.set("start_time", boundaries.thirtyDaysAgo);
        params.set("end_time", boundaries.sevenDaysAgo);
      } else {
        params.set("end_time", boundaries.thirtyDaysAgo);
      }
      return params.toString();
    },
    [boundaries]
  );

  const loadBucket = useCallback(
    async (key: BucketKey) => {
      const current = bucketsRef.current[key];
      if (current.loading) {
        return;
      }
      const offset = current.pagesLoaded * CHAT_SESSION_PAGE_SIZE;
      setBuckets((p) => ({ ...p, [key]: { ...p[key], loading: true } }));
      try {
        const res = await fetch(
          `/api/chat/get-user-chat-sessions?${queryFor(key, offset)}`
        );
        if (!res.ok) {
          throw new Error(`status ${res.status}`);
        }
        const body = await res.json();
        setBuckets((p) => ({
          ...p,
          [key]: {
            sessions: dedupeAppend(p[key].sessions, body.sessions ?? []),
            pagesLoaded: p[key].pagesLoaded + 1,
            hasMore: body.has_more ?? false,
            loading: false,
            loaded: true,
          },
        }));
      } catch (error) {
        setBuckets((p) => ({
          ...p,
          [key]: { ...p[key], loading: false, hasMore: false, loaded: true },
        }));
        setPopup({ message: "Failed to load chats", type: "error" });
      }
    },
    [queryFor, setPopup]
  );

  const toggleBucket = (key: BucketKey) => {
    const willExpand = !expanded[key];
    setExpanded((p) => ({ ...p, [key]: willExpand }));
    if (willExpand && !bucketsRef.current[key].loaded) {
      loadBucket(key);
    }
  };

  const handleDropToRemoveFromFolder = async (
    event: React.DragEvent<HTMLDivElement>
  ) => {
    event.preventDefault();
    setIsDragOver(false); // Reset drag over state on drop
    const chatSessionId = parseInt(
      event.dataTransfer.getData(CHAT_SESSION_ID_KEY),
      10
    );
    const folderId = event.dataTransfer.getData(FOLDER_ID_KEY);

    if (folderId) {
      try {
        await removeChatFromFolder(parseInt(folderId, 10), chatSessionId);
        router.refresh(); // Refresh the page to reflect the changes
      } catch (error) {
        setPopup({
          message: "Failed to remove chat from folder",
          type: "error",
        });
      }
    }
  };

  const renderSessions = (sessions: ChatSession[]) =>
    sessions
      .filter((chat) => chat.folder_id === null)
      .map((chat) => (
        <div key={`${chat.id}-${chat.name}`}>
          <ChatSessionDisplay
            chatSession={chat}
            isSelected={currentChatId === chat.id}
            skipGradient={isDragOver}
          />
        </div>
      ));

  return (
    <div className="mb-1 ml-3 overflow-y-auto flex-1 min-h-0 no-scrollbar">
      {folders.length > 0 && (
        <div className="py-2 mr-3 border-b border-border">
          <div className="text-xs text-subtle flex pb-0.5 mb-1.5 mt-2 font-medium">
            Folders
          </div>
          <FolderList
            folders={folders}
            currentChatId={currentChatId}
            openedFolders={openedFolders}
          />
        </div>
      )}

      <div
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragOver(true);
        }}
        onDragLeave={() => setIsDragOver(false)}
        onDrop={handleDropToRemoveFromFolder}
        className={`pt-1 transition duration-300 ease-in-out mr-3 ${
          isDragOver ? "bg-hover" : ""
        } rounded-md`}
      >
        {BUCKETS.map(({ key, title, collapsible }) => {
          const bucket = buckets[key];
          const isOpen = expanded[key];
          const loose = bucket.sessions.filter((c) => c.folder_id === null);
          return (
            <div key={key}>
              {collapsible ? (
                <button
                  type="button"
                  onClick={() => toggleBucket(key)}
                  className="w-full text-xs text-subtle flex items-center pb-0.5 mb-1.5 mt-5 font-medium hover:text-default"
                >
                  {isOpen ? (
                    <FiChevronDown className="mr-1" />
                  ) : (
                    <FiChevronRight className="mr-1" />
                  )}
                  {title}
                </button>
              ) : (
                loose.length > 0 && (
                  <div className="text-xs text-subtle flex pb-0.5 mb-1.5 mt-5 font-medium">
                    {title}
                  </div>
                )
              )}

              {isOpen && (
                <>
                  {renderSessions(bucket.sessions)}

                  {bucket.loading && (
                    <div className="flex justify-center py-3 text-subtle">
                      <FiLoader className="animate-spin" />
                    </div>
                  )}

                  {!bucket.loading && bucket.hasMore && (
                    <button
                      type="button"
                      onClick={() => loadBucket(key)}
                      className="w-full text-xs text-link hover:text-link-hover py-2 pl-1 text-left font-medium"
                    >
                      Show more
                    </button>
                  )}

                  {collapsible &&
                    bucket.loaded &&
                    !bucket.loading &&
                    loose.length === 0 && (
                      <div className="text-xs text-subtle italic pl-1 pb-1">
                        No chats in this range
                      </div>
                    )}
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
