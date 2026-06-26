import React, {
  Dispatch,
  SetStateAction,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  FiSend,
  FiFilter,
  FiPlusCircle,
  FiCpu,
  FiX,
  FiPlus,
  FiInfo,
} from "react-icons/fi";
import ChatInputOption from "./ChatInputOption";
import { FaBrain } from "react-icons/fa";
import { Persona } from "@/app/admin/assistants/interfaces";
import { assistantDisplayName } from "@/lib/assistants/displayName";
import {
  getMentionQuery,
  filterAssistantsByMention,
  stripMentionToken,
} from "@/lib/assistants/mentions";
import { FilterManager, LlmOverrideManager } from "@/lib/hooks";
import { SelectedFilterDisplay } from "./SelectedFilterDisplay";
import { useChatContext } from "@/components/context/ChatContext";
import { SettingsContext } from "@/components/settings/SettingsProvider";
import { getFinalLLM } from "@/lib/llm/utils";
import { getModelDisplayName } from "@/lib/llm/models";
import { FileDescriptor } from "../interfaces";
import { InputBarPreview } from "../files/InputBarPreview";
import { RobotIcon } from "@/components/icons/icons";
import { Hoverable } from "@/components/Hoverable";
import { AssistantIcon } from "@/components/assistants/AssistantIcon";
import { Tooltip } from "@/components/tooltip/Tooltip";
const MAX_INPUT_HEIGHT = 200;

export function ChatInputBar({
  personas,
  message,
  setMessage,
  onSubmit,
  isStreaming,
  setIsCancelled,
  retrievalDisabled,
  filterManager,
  llmOverrideManager,
  useReranking,
  setUseReranking,
  useRelevanceFilter,
  setUseRelevanceFilter,
  onSetSelectedAssistant,
  selectedAssistant,
  files,
  setFiles,
  handleFileUpload,
  setConfigModalActiveTab,
  configModalActiveTab,
  textAreaRef,
  alternativeAssistant,
}: {
  onSetSelectedAssistant: (alternativeAssistant: Persona | null) => void;
  personas: Persona[];
  message: string;
  setMessage: (message: string) => void;
  onSubmit: () => void;
  isStreaming: boolean;
  setIsCancelled: (value: boolean) => void;
  retrievalDisabled: boolean;
  filterManager: FilterManager;
  llmOverrideManager: LlmOverrideManager;
  useReranking: boolean;
  setUseReranking: (value: boolean) => void;
  useRelevanceFilter: boolean;
  setUseRelevanceFilter: (value: boolean) => void;
  selectedAssistant: Persona;
  alternativeAssistant: Persona | null;
  files: FileDescriptor[];
  setFiles: (files: FileDescriptor[]) => void;
  handleFileUpload: (files: File[]) => void;
  setConfigModalActiveTab: (tab: string) => void;
  configModalActiveTab: string | null;
  textAreaRef: React.RefObject<HTMLTextAreaElement>;
}) {
  // handle re-sizing of the text area
  useEffect(() => {
    const textarea = textAreaRef.current;
    if (textarea) {
      textarea.style.height = "0px";
      textarea.style.height = `${Math.min(
        textarea.scrollHeight,
        MAX_INPUT_HEIGHT
      )}px`;
    }
  }, [message]);

  // Block sending while any attached file is still uploading — otherwise the
  // message references a file_id whose file_store row doesn't exist yet, and
  // the backend errors ("File by name ... does not exist"). Send re-enables
  // automatically once the upload(s) finish (the per-file spinner clears).
  const anyFilesUploading = files.some((file) => file.isUploading);
  const canSubmit = !!message && !isStreaming && !anyFilesUploading;

  const handlePaste = (event: React.ClipboardEvent) => {
    const items = event.clipboardData?.items;
    if (items) {
      const pastedFiles = [];
      for (let i = 0; i < items.length; i++) {
        if (items[i].kind === "file") {
          const file = items[i].getAsFile();
          if (file) pastedFiles.push(file);
        }
      }
      if (pastedFiles.length > 0) {
        event.preventDefault();
        handleFileUpload(pastedFiles);
      }
    }
  };

  const { llmProviders } = useChatContext();
  // Cluster-level enablement — hide the per-conversation rerank/relevance
  // toggles entirely when the feature is disabled cluster-wide.
  const settings = useContext(SettingsContext)?.settings;
  const [_, llmName] = getFinalLLM(llmProviders, selectedAssistant, null);

  const suggestionsRef = useRef<HTMLDivElement | null>(null);
  const [showSuggestions, setShowSuggestions] = useState(false);

  const interactionsRef = useRef<HTMLDivElement | null>(null);

  const hideSuggestions = () => {
    setShowSuggestions(false);
    setAssistantIconIndex(0);
  };

  // Update selected persona
  const updateCurrentPersona = (persona: Persona) => {
    onSetSelectedAssistant(persona.id == selectedAssistant.id ? null : persona);
    hideSuggestions();
    // Remove only the "@mention" token the user was typing and keep the rest of
    // the message intact (previously this cleared the entire input, discarding
    // any question already typed before/around the mention).
    setMessage(stripMentionToken(message));
  };

  // Click out of assistant suggestions
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        suggestionsRef.current &&
        !suggestionsRef.current.contains(event.target as Node) &&
        (!interactionsRef.current ||
          !interactionsRef.current.contains(event.target as Node))
      ) {
        hideSuggestions();
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, []);

  // The partial assistant name being typed after "@" (anywhere in the message),
  // or null when the caret isn't inside an @mention token. See lib/assistants/mentions.
  const mentionQuery = getMentionQuery(message);

  // Complete user input handling
  const handleInputChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    const text = event.target.value;
    setMessage(text);

    // Show the assistant typeahead whenever the caret is inside an @mention
    // token — anywhere in the message (previously this only fired when "@" was
    // the first character of the input).
    if (getMentionQuery(text) !== null) {
      setShowSuggestions(true);
    } else {
      hideSuggestions();
    }
  };

  // Match on both the display name and the raw name so typing either works.
  const filteredPersonas = filterAssistantsByMention(personas, mentionQuery ?? "");

  const [assistantIconIndex, setAssistantIconIndex] = useState(0);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      showSuggestions &&
      filteredPersonas.length > 0 &&
      (e.key === "Tab" || e.key == "Enter")
    ) {
      e.preventDefault();
      if (assistantIconIndex == filteredPersonas.length) {
        window.open("/assistants/new", "_blank");
        hideSuggestions();
        setMessage("");
      } else {
        const option =
          filteredPersonas[assistantIconIndex >= 0 ? assistantIconIndex : 0];
        updateCurrentPersona(option);
      }
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setAssistantIconIndex((assistantIconIndex) =>
        Math.min(assistantIconIndex + 1, filteredPersonas.length)
      );
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setAssistantIconIndex((assistantIconIndex) =>
        Math.max(assistantIconIndex - 1, 0)
      );
    }
  };

  return (
    <div>
      <div className="flex justify-center pb-2 mx-auto mb-2">
        <div
          className="
            w-full
            shrink
            relative
            px-4
            w-searchbar-xs
            2xl:w-searchbar-sm
            3xl:w-searchbar
            mx-auto
          "
        >
          {showSuggestions && filteredPersonas.length > 0 && (
            <div
              ref={suggestionsRef}
              className="text-sm absolute inset-x-0 top-0 w-full transform -translate-y-full"
            >
              <div className="rounded-lg py-1.5 bg-background-search dark:bg-background-strong border border-border-medium overflow-hidden shadow-lg mx-2 px-1.5 mt-2 rounded z-10">
                {filteredPersonas.map((currentPersona, index) => (
                  <button
                    key={index}
                    className={`px-2 ${
                      assistantIconIndex == index && "bg-hover"
                    } rounded content-start flex gap-x-1 py-1.5 w-full  hover:bg-hover cursor-pointer`}
                    onClick={() => {
                      updateCurrentPersona(currentPersona);
                    }}
                  >
                    <p className="font-bold ">
                      {assistantDisplayName(currentPersona)}
                    </p>
                    <p className="line-clamp-1">
                      {currentPersona.id == selectedAssistant.id &&
                        "(default) "}
                      {currentPersona.description}
                    </p>
                  </button>
                ))}
                <a
                  key={filteredPersonas.length}
                  target="_blank"
                  className={`${
                    assistantIconIndex == filteredPersonas.length && "bg-hover"
                  } px-3 flex gap-x-1 py-2 w-full  items-center  hover:bg-hover-light cursor-pointer"`}
                  href="/assistants/new"
                >
                  <FiPlus size={17} />
                  <p>Create a new assistant</p>
                </a>
              </div>
            </div>
          )}

          <div>
            <SelectedFilterDisplay filterManager={filterManager} />
          </div>

          <div
            className="
              opacity-100
              w-full
              h-fit
              flex
              flex-col
              border
              border-border-medium
              rounded-xl
              overflow-hidden
              bg-background-weak
              shadow-lg
              shadow-black/5
              dark:shadow-black/40
              transition
              focus-within:border-accent
              focus-within:ring-2
              focus-within:ring-accent/30
            "
          >
            {alternativeAssistant && (
              <div className="flex flex-wrap gap-y-1 gap-x-2 px-2 pt-1.5 w-full">
                <div
                  ref={interactionsRef}
                  className="bg-background-subtle p-2 rounded-t-lg  items-center flex w-full"
                >
                  <AssistantIcon assistant={alternativeAssistant} border />
                  <p className="ml-3 text-strong my-auto">
                    {assistantDisplayName(alternativeAssistant)}
                  </p>
                  <div className="flex gap-x-1 ml-auto ">
                    <Tooltip
                      content={
                        <p className="max-w-xs flex flex-wrap">
                          {alternativeAssistant.description}
                        </p>
                      }
                    >
                      <button>
                        <Hoverable icon={FiInfo} />
                      </button>
                    </Tooltip>

                    <Hoverable
                      icon={FiX}
                      onClick={() => onSetSelectedAssistant(null)}
                    />
                  </div>
                </div>
              </div>
            )}

            {files.length > 0 && (
              <div className="flex flex-wrap gap-y-1 gap-x-2 px-2 pt-2">
                {files.map((file) => (
                  <div key={file.id}>
                    <InputBarPreview
                      file={file}
                      onDelete={() => {
                        setFiles(
                          files.filter(
                            (fileInFilter) => fileInFilter.id !== file.id
                          )
                        );
                      }}
                      isUploading={file.isUploading || false}
                    />
                  </div>
                ))}
              </div>
            )}

            <textarea
              onPaste={handlePaste}
              onKeyDownCapture={handleKeyDown}
              onChange={handleInputChange}
              ref={textAreaRef}
              className={`
                m-0
                w-full
                shrink
                resize-none
                border-0
                bg-background-weak
                ${
                  textAreaRef.current &&
                  textAreaRef.current.scrollHeight > MAX_INPUT_HEIGHT
                    ? "overflow-y-auto mt-2"
                    : ""
                }
                overflow-hidden
                whitespace-normal
                break-word
                overscroll-contain
                outline-none
                placeholder-subtle
                overflow-hidden
                resize-none
                pl-4
                pr-12
                py-5
                text-base
                min-h-[88px]
              `}
              autoFocus
              style={{ scrollbarWidth: "thin" }}
              role="textarea"
              aria-multiline
              placeholder="How can I help you today?"
              value={message}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && canSubmit) {
                  onSubmit();
                  event.preventDefault();
                } else if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  anyFilesUploading
                ) {
                  // Swallow the Enter so a half-typed message isn't sent
                  // against a not-yet-uploaded file.
                  event.preventDefault();
                }
              }}
              suppressContentEditableWarning={true}
            />
            <div className="flex items-center space-x-3 mr-12 px-4 pb-2 overflow-hidden">
              <ChatInputOption
                flexPriority="shrink"
                name={
                  selectedAssistant
                    ? assistantDisplayName(selectedAssistant)
                    : "Assistants"
                }
                icon={FaBrain}
                onClick={() => setConfigModalActiveTab("assistants")}
              />

              <ChatInputOption
                flexPriority="second"
                name={
                  getModelDisplayName(
                    llmOverrideManager.llmOverride.provider,
                    llmOverrideManager.llmOverride.modelName
                  ) ||
                  (selectedAssistant
                    ? selectedAssistant.llm_model_version_override || llmName
                    : llmName)
                }
                icon={FiCpu}
                onClick={() => setConfigModalActiveTab("llms")}
              />

              {!retrievalDisabled && (
                <ChatInputOption
                  flexPriority="stiff"
                  name="Filters"
                  icon={FiFilter}
                  onClick={() => setConfigModalActiveTab("filters")}
                />
              )}

              {/* Per-conversation search-quality toggles. Hidden entirely when
                  the feature is disabled cluster-wide (settings.*_enabled). */}
              {!retrievalDisabled && settings?.rerank_enabled && (
                <button
                  type="button"
                  onClick={() => setUseReranking(!useReranking)}
                  title="Rerank retrieved results with a cross-encoder for this conversation (only applies if reranking is enabled by an admin)"
                  className={`flex-none text-sm rounded px-2 py-1 ${
                    useReranking ? "bg-hover text-emphasis" : "text-subtle"
                  }`}
                >
                  Rerank: {useReranking ? "On" : "Off"}
                </button>
              )}

              {!retrievalDisabled && settings?.llm_relevance_filter_enabled && (
                <button
                  type="button"
                  onClick={() => setUseRelevanceFilter(!useRelevanceFilter)}
                  title="Apply an LLM relevance filter to retrieved results for this conversation (only applies if enabled by an admin)"
                  className={`flex-none text-sm rounded px-2 py-1 ${
                    useRelevanceFilter
                      ? "bg-hover text-emphasis"
                      : "text-subtle"
                  }`}
                >
                  Relevance: {useRelevanceFilter ? "On" : "Off"}
                </button>
              )}

              <ChatInputOption
                flexPriority="stiff"
                name="File"
                icon={FiPlusCircle}
                onClick={() => {
                  const input = document.createElement("input");
                  input.type = "file";
                  input.multiple = true; // Allow multiple files
                  input.onchange = (event: any) => {
                    const files = Array.from(
                      event?.target?.files || []
                    ) as File[];
                    if (files.length > 0) {
                      handleFileUpload(files);
                    }
                  };
                  input.click();
                }}
              />
            </div>
            <div
              className={`absolute bottom-2.5 right-10 ${
                configModalActiveTab ? "invisible" : ""
              }`}
            >
              <div
                className={
                  anyFilesUploading && !isStreaming
                    ? "cursor-not-allowed"
                    : "cursor-pointer"
                }
                title={
                  anyFilesUploading
                    ? "Waiting for file upload to finish…"
                    : undefined
                }
                onClick={() => {
                  if (!isStreaming) {
                    if (canSubmit) {
                      onSubmit();
                    }
                  } else {
                    setIsCancelled(true);
                  }
                }}
              >
                <FiSend
                  size={18}
                  className={`w-9 h-9 p-2 rounded-lg transition-colors ${
                    anyFilesUploading && !isStreaming ? "opacity-40 " : ""
                  }${
                    message
                      ? "bg-accent text-white hover:bg-accent-hover"
                      : "text-emphasis"
                  }`}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
