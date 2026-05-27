"use client";

import { User } from "@/lib/types";
import Link from "next/link";
import React, { useContext } from "react";
import { FiMessageSquare } from "react-icons/fi";
import { HeaderWrapper } from "./HeaderWrapper";
import { SettingsContext } from "../settings/SettingsProvider";
import { UserDropdown } from "../UserDropdown";
import { Logo } from "../Logo";
import { NEXT_PUBLIC_DO_NOT_USE_TOGGLE_OFF_DANSWER_POWERED } from "@/lib/constants";

export function HeaderTitle({ children }: { children: JSX.Element | string }) {
  return <h1 className="flex text-2xl text-strong font-bold">{children}</h1>;
}

interface HeaderProps {
  user: User | null;
}

export function Header({ user }: HeaderProps) {
  const combinedSettings = useContext(SettingsContext);
  if (!combinedSettings) {
    return null;
  }
  const settings = combinedSettings.settings;
  const enterpriseSettings = combinedSettings.enterpriseSettings;

  return (
    <HeaderWrapper>
      <div className="flex h-full">
        <Link className="py-3 flex flex-col" href="/chat">
          <div className="flex my-auto">
            <div className="mr-1 my-auto">
              <Logo />
            </div>
            <div className="my-auto">
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
          </div>
        </Link>

        {(!settings || settings.chat_page_enabled) && (
          <Link
            href="/chat"
            className="ml-6 my-auto flex items-center gap-2 px-3 py-2 rounded-md border border-accent bg-accent/10 text-accent hover:bg-accent hover:text-white transition-colors"
          >
            <FiMessageSquare size={18} />
            <span className="text-sm font-semibold">Chat</span>
          </Link>
        )}

        <div className="ml-auto h-full flex flex-col">
          <div className="my-auto">
            <UserDropdown user={user} hideChatAndSearch />
          </div>
        </div>
      </div>
    </HeaderWrapper>
  );
}

/* 

*/
