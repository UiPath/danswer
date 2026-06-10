"use client";

import {
  FiBook,
  FiEdit,
  FiFolderPlus,
  FiLoader,
  FiPlusSquare,
} from "react-icons/fi";
import { useContext, useEffect, useRef, useState, useTransition } from "react";
import Link from "next/link";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { BasicClickable, BasicSelectable } from "@/components/BasicClickable";
import { ChatSession } from "../interfaces";

import {
  NEXT_PUBLIC_DO_NOT_USE_TOGGLE_OFF_DANSWER_POWERED,
  NEXT_PUBLIC_NEW_CHAT_DIRECTS_TO_SAME_PERSONA,
} from "@/lib/constants";

import { ChatTab } from "./ChatTab";
import { ChatThemeToggle } from "../ChatThemeToggle";
import { Folder } from "../folders/interfaces";
import { createFolder } from "../folders/FolderManagement";
import { usePopup } from "@/components/admin/connectors/Popup";
import { SettingsContext } from "@/components/settings/SettingsProvider";

import React from "react";
import { FaBrain } from "react-icons/fa";
import { Logo } from "@/components/Logo";
import { HeaderTitle } from "@/components/header/Header";

export const ChatSidebar = ({
  existingChats,
  currentChatSession,
  folders,
  openedFolders,
}: {
  existingChats: ChatSession[];
  currentChatSession: ChatSession | null | undefined;
  folders: Folder[];
  openedFolders: { [key: number]: boolean };
}) => {
  const router = useRouter();
  const { popup, setPopup } = usePopup();

  // Navigating to "Manage Assistants" awaits the heavy fetchChatData
  // bundle server-side. useTransition keeps the *current* page (with this
  // sidebar) mounted and visible throughout — so it reads as an in-app
  // transition, not a blank reload — while isPending drives an inline
  // spinner on the button so the click clearly registers.
  const [isNavigatingAssistants, startAssistantsNav] = useTransition();

  const currentChatId = currentChatSession?.id;

  // prevent the NextJS Router cache from causing the chat sidebar to not
  // update / show an outdated list of chats
  useEffect(() => {
    router.refresh();
  }, [currentChatId]);

  // Local mirror of the server-provided folders so we can show a newly
  // created folder instantly, without a full `router.refresh()` (which
  // re-runs the entire heavy fetchChatData bundle just to add one empty
  // folder). Re-synced whenever the server prop changes.
  const [localFolders, setLocalFolders] = useState<Folder[]>(folders);
  useEffect(() => {
    setLocalFolders(folders);
  }, [folders]);

  const combinedSettings = useContext(SettingsContext);
  if (!combinedSettings) {
    return null;
  }
  const settings = combinedSettings.settings;
  const enterpriseSettings = combinedSettings.enterpriseSettings;

  return (
    <>
      {popup}
      <div
        className={`
        w-64
        flex
        flex-none
        bg-background-weak
        3xl:w-72
        border-r 
        border-border 
        flex 
        flex-col 
        h-screen
        transition-transform`}
        id="chat-sidebar"
      >
        <div className="pt-6 flex">
          <Link className="ml-4 w-full" href="/chat">
            <div className="flex w-full">
              <Logo height={32} width={30} className="mr-1 my-auto" />

              {enterpriseSettings && enterpriseSettings.application_name ? (
                <div>
                  <HeaderTitle>
                    {enterpriseSettings.application_name}
                  </HeaderTitle>

                  {!NEXT_PUBLIC_DO_NOT_USE_TOGGLE_OFF_DANSWER_POWERED && (
                    <p className="text-xs text-subtle -mt-1.5">
                      Powered by Darwin
                    </p>
                  )}
                </div>
              ) : (
                <HeaderTitle>Darwin</HeaderTitle>
              )}
            </div>
          </Link>
        </div>

        <div className="flex mt-5 items-center">
          <Link
            href={
              "/chat" +
              (NEXT_PUBLIC_NEW_CHAT_DIRECTS_TO_SAME_PERSONA &&
              currentChatSession
                ? `?assistantId=${currentChatSession.persona_id}`
                : "")
            }
            className="ml-3 w-full"
          >
            <BasicClickable fullWidth>
              <div className="flex items-center text-sm">
                <FiEdit className="ml-1 mr-2" /> New Chat
              </div>
            </BasicClickable>
          </Link>

          <div className="ml-1.5 mr-3 h-full">
            <BasicClickable
              onClick={() =>
                createFolder("New Folder")
                  .then((folderId) => {
                    // Append the new (empty) folder to local state instead
                    // of router.refresh() — instant, no full refetch. The
                    // create POST itself is a single fast INSERT.
                    setLocalFolders((prev) => [
                      ...prev,
                      {
                        folder_id: folderId,
                        folder_name: "New Folder",
                        display_priority:
                          prev.reduce(
                            (max, f) => Math.max(max, f.display_priority),
                            -1
                          ) + 1,
                        chat_sessions: [],
                      },
                    ]);
                  })
                  .catch((error) => {
                    console.error("Failed to create folder:", error);
                    setPopup({
                      message: `Failed to create folder: ${error.message}`,
                      type: "error",
                    });
                  })
              }
            >
              <div className="flex items-center text-sm h-full">
                <FiFolderPlus className="mx-1 my-auto" />
              </div>
            </BasicClickable>
          </div>
        </div>

        <div className="mt-3 mb-1 mx-3">
          <BasicClickable
            fullWidth
            onClick={() =>
              startAssistantsNav(() => router.push("/assistants/mine"))
            }
          >
            <div className="flex items-center text-default font-medium">
              {isNavigatingAssistants ? (
                <FiLoader className="ml-1 mr-2 animate-spin" />
              ) : (
                <FaBrain className="ml-1 mr-2" />
              )}
              {isNavigatingAssistants ? "Loading…" : "Manage Assistants"}
            </div>
          </BasicClickable>
        </div>

        <div className="mt-2 mb-1 mx-3">
          <ChatThemeToggle />
        </div>

        <div className="border-b border-border pb-4 mx-3" />

        <ChatTab
          existingChats={existingChats}
          currentChatId={currentChatId}
          folders={localFolders}
          openedFolders={openedFolders}
        />
      </div>
    </>
  );
};
