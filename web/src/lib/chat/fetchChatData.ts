import {
  AuthTypeMetadata,
  getAuthTypeMetadataSS,
  getCurrentUserSS,
} from "@/lib/userSS";
import { fetchSS } from "@/lib/utilsSS";
import {
  CCPairBasicInfo,
  DocumentSet,
  Tag,
  User,
  ValidSources,
} from "@/lib/types";
import { ChatSession, CHAT_SESSION_PAGE_SIZE } from "@/app/chat/interfaces";
import { Persona } from "@/app/admin/assistants/interfaces";
import { FullEmbeddingModelResponse } from "@/app/admin/models/embedding/embeddingModels";
import { Settings } from "@/app/admin/settings/interfaces";
import { fetchLLMProvidersSS } from "@/lib/llm/fetchLLMs";
import { LLMProviderDescriptor } from "@/app/admin/models/llm/interfaces";
import { Folder } from "@/app/chat/folders/interfaces";
import { personaComparator } from "@/app/admin/assistants/lib";
import { cookies } from "next/headers";
import { DOCUMENT_SIDEBAR_WIDTH_COOKIE_NAME } from "@/components/resizable/contants";
import { hasCompletedWelcomeFlowSS } from "@/components/initialSetup/welcome/WelcomeModalWrapper";
import { fetchAssistantsSS } from "../assistants/fetchAssistantsSS";

interface FetchChatDataResult {
  user: User | null;
  chatSessions: ChatSession[];
  // True when older sessions exist beyond the first (recent) page, so the
  // sidebar knows to keep lazy-loading on scroll.
  hasMoreChatSessions: boolean;
  ccPairs: CCPairBasicInfo[];
  availableSources: ValidSources[];
  documentSets: DocumentSet[];
  assistants: Persona[];
  tags: Tag[];
  llmProviders: LLMProviderDescriptor[];
  folders: Folder[];
  openedFolders: Record<string, boolean>;
  defaultPersonaId?: number;
  finalDocumentSidebarInitialWidth?: number;
  shouldShowWelcomeModal: boolean;
  shouldDisplaySourcesIncompleteModal: boolean;
}

// Start of the "Today" window (midnight 1 day ago), matching the sidebar's
// Today bucket in groupSessionsByDateRange. Returned as an ISO string for the
// chat-sessions query: only Today loads on first paint; every other bucket
// (Previous 7 Days / 30 Days / Over 30 days ago) is collapsed and lazy-loads
// when expanded. Computed server-side for the initial paint; the client
// recomputes the same cutoffs for the older buckets.
function todayWindowStart(): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return new Date(today.getTime() - 1 * 24 * 3600 * 1000).toISOString();
}

export async function fetchChatData(searchParams: {
  [key: string]: string;
}): Promise<FetchChatDataResult | { redirect: string }> {
  const tasks = [
    getAuthTypeMetadataSS(),
    getCurrentUserSS(),
    fetchSS("/manage/indexing-status"),
    fetchSS("/manage/document-set"),
    fetchAssistantsSS(),
    // Only the Today window. Every older bucket (Previous 7 Days / 30 Days /
    // Over 30 days ago) is collapsed by default and lazy-loads in the sidebar
    // when expanded, so history beyond today never loads on first paint.
    fetchSS(
      `/chat/get-user-chat-sessions?limit=${CHAT_SESSION_PAGE_SIZE}` +
        `&start_time=${encodeURIComponent(todayWindowStart())}`
    ),
    fetchSS("/query/valid-tags"),
    fetchLLMProvidersSS(),
    fetchSS("/folder"),
  ];

  // Use allSettled (not Promise.all) so that ONE failing fetch — e.g. a
  // backend 500 whose body isn't valid JSON — degrades only that piece instead
  // of rejecting the whole batch and nulling everything (which previously
  // crashed the entire /chat server render with "object null is not iterable"
  // when results[4] was destructured).
  const settled = await Promise.allSettled(tasks);
  const results: (
    | User
    | Response
    | AuthTypeMetadata
    | FullEmbeddingModelResponse
    | Settings
    | LLMProviderDescriptor[]
    | [Persona[], string | null]
    | null
  )[] = settled.map((outcome, i) => {
    if (outcome.status === "fulfilled") {
      return outcome.value;
    }
    console.log(
      `Some fetch failed for the main chat page (task ${i}) - ${outcome.reason}`
    );
    return null;
  });

  const authTypeMetadata = results[0] as AuthTypeMetadata | null;
  const user = results[1] as User | null;
  const ccPairsResponse = results[2] as Response | null;
  const documentSetsResponse = results[3] as Response | null;
  // results[4] is fetchAssistantsSS()'s [assistants, error] tuple — but it's
  // null if that fetch failed, so guard the destructure (this exact spot was
  // the crash).
  const assistantsResult = results[4] as [Persona[], string | null] | null;
  const rawAssistantsList: Persona[] = assistantsResult?.[0] ?? [];
  const assistantsFetchError: string | null = assistantsResult?.[1] ?? null;
  const chatSessionsResponse = results[5] as Response | null;
  const tagsResponse = results[6] as Response | null;
  const llmProviders = (results[7] || []) as LLMProviderDescriptor[];
  const foldersResponse = results[8] as Response | null; // Handle folders result

  const authDisabled = authTypeMetadata?.authType === "disabled";
  if (!authDisabled && !user) {
    return { redirect: "/auth/login" };
  }

  if (user && !user.is_verified && authTypeMetadata?.requiresVerification) {
    return { redirect: "/auth/waiting-on-verification" };
  }

  let ccPairs: CCPairBasicInfo[] = [];
  if (ccPairsResponse?.ok) {
    ccPairs = await ccPairsResponse.json();
  } else {
    console.log(`Failed to fetch connectors - ${ccPairsResponse?.status}`);
  }
  const availableSources: ValidSources[] = [];
  ccPairs.forEach((ccPair) => {
    if (!availableSources.includes(ccPair.source)) {
      availableSources.push(ccPair.source);
    }
  });

  let chatSessions: ChatSession[] = [];
  let hasMoreChatSessions = false;
  if (chatSessionsResponse?.ok) {
    const chatSessionsBody = await chatSessionsResponse.json();
    chatSessions = chatSessionsBody.sessions;
    hasMoreChatSessions = chatSessionsBody.has_more ?? false;
  } else {
    console.log(
      `Failed to fetch chat sessions - ${chatSessionsResponse?.text()}`
    );
  }
  // Larger ID -> created later
  chatSessions.sort((a, b) => (a.id > b.id ? -1 : 1));

  // The sidebar only loads the recent page, but the page may be opened on an
  // OLDER chat (deep link / reload of ?chatId=<old>). ChatPage derives the
  // resumed chat's persona + model override from this list (selectedChatSession
  // -> existingChatSessionPersonaId / llmOverrideManager), so if the open chat
  // falls outside the recent page we fetch it explicitly and prepend it.
  // Otherwise resuming an old chat would silently load the wrong assistant.
  const currentChatIdRaw = searchParams["chatId"];
  const currentChatId = currentChatIdRaw ? parseInt(currentChatIdRaw) : null;
  if (
    currentChatId !== null &&
    !Number.isNaN(currentChatId) &&
    !chatSessions.some((session) => session.id === currentChatId)
  ) {
    try {
      const currentSessionResponse = await fetchSS(
        `/chat/get-chat-session/${currentChatId}`
      );
      if (currentSessionResponse.ok) {
        const detail = await currentSessionResponse.json();
        chatSessions.unshift({
          id: detail.chat_session_id,
          name: detail.description,
          persona_id: detail.persona_id,
          time_created: detail.time_created,
          shared_status: detail.shared_status,
          folder_id: null,
          current_alternate_model: detail.current_alternate_model ?? "",
        });
      }
    } catch (e) {
      console.log(`Failed to fetch current chat session ${currentChatId} - ${e}`);
    }
  }

  let documentSets: DocumentSet[] = [];
  if (documentSetsResponse?.ok) {
    documentSets = await documentSetsResponse.json();
  } else {
    console.log(
      `Failed to fetch document sets - ${documentSetsResponse?.status}`
    );
  }

  let assistants = rawAssistantsList;
  if (assistantsFetchError) {
    console.log(`Failed to fetch assistants - ${assistantsFetchError}`);
  }
  // remove those marked as hidden by an admin
  assistants = assistants.filter((assistant) => assistant.is_visible);

  // sort them in priority order
  assistants.sort(personaComparator);

  let tags: Tag[] = [];
  if (tagsResponse?.ok) {
    tags = (await tagsResponse.json()).tags;
  } else {
    console.log(`Failed to fetch tags - ${tagsResponse?.status}`);
  }

  // Preselected assistant: numeric `assistantId` wins; otherwise resolve the
  // human-readable `assistant=<name>` param (used by shareable links like the
  // per-channel Slack "Ask Darwin" workflow) to an id. Resolution is an exact,
  // case-insensitive match against `assistants` — the user's ACL-filtered, visible
  // list — so an unknown or inaccessible name simply yields no preselection
  // (falls back to the default). The backend also re-checks persona access on send.
  const defaultPersonaIdRaw = searchParams["assistantId"];
  let defaultPersonaId = defaultPersonaIdRaw
    ? parseInt(defaultPersonaIdRaw)
    : undefined;
  if (defaultPersonaId === undefined || Number.isNaN(defaultPersonaId)) {
    const assistantNameRaw = searchParams["assistant"];
    if (assistantNameRaw) {
      const target = assistantNameRaw.trim().toLowerCase();
      const match = assistants.find(
        (a) =>
          a.name.trim().toLowerCase() === target ||
          (a.display_name != null &&
            a.display_name.trim().toLowerCase() === target)
      );
      defaultPersonaId = match?.id;
    }
  }

  const documentSidebarCookieInitialWidth = cookies().get(
    DOCUMENT_SIDEBAR_WIDTH_COOKIE_NAME
  );
  const finalDocumentSidebarInitialWidth = documentSidebarCookieInitialWidth
    ? parseInt(documentSidebarCookieInitialWidth.value)
    : undefined;

  const hasAnyConnectors = ccPairs.length > 0;
  const shouldShowWelcomeModal =
    !hasCompletedWelcomeFlowSS() &&
    !hasAnyConnectors &&
    (!user || user.role === "admin");
  const shouldDisplaySourcesIncompleteModal =
    hasAnyConnectors &&
    !shouldShowWelcomeModal &&
    !ccPairs.some(
      (ccPair) => ccPair.has_successful_run && ccPair.docs_indexed > 0
    );

  // if no connectors are setup, only show personas that are pure
  // passthrough and don't do any retrieval
  if (!hasAnyConnectors) {
    assistants = assistants.filter((assistant) => assistant.num_chunks === 0);
  }

  let folders: Folder[] = [];
  if (foldersResponse?.ok) {
    folders = (await foldersResponse.json()).folders as Folder[];
  } else {
    console.log(`Failed to fetch folders - ${foldersResponse?.status}`);
  }

  const openedFoldersCookie = cookies().get("openedFolders");
  const openedFolders = openedFoldersCookie
    ? JSON.parse(openedFoldersCookie.value)
    : {};

  return {
    user,
    chatSessions,
    hasMoreChatSessions,
    ccPairs,
    availableSources,
    documentSets,
    assistants,
    tags,
    llmProviders,
    folders,
    openedFolders,
    defaultPersonaId,
    finalDocumentSidebarInitialWidth,
    shouldShowWelcomeModal,
    shouldDisplaySourcesIncompleteModal,
  };
}
