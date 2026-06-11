"use client";

import { useState, useRef, useContext, useEffect } from "react";
import { FiMessageSquare, FiTool, FiLogOut, FiSun, FiMoon } from "react-icons/fi";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { User } from "@/lib/types";
import { checkUserIsNoAuthUser, logout } from "@/lib/user";
import { BasicSelectable } from "@/components/BasicClickable";
import { Popover } from "./popover/Popover";
import { FaBrain } from "react-icons/fa";
import { LOGOUT_DISABLED } from "@/lib/constants";
import { Settings } from "@/app/admin/settings/interfaces";
import { SettingsContext } from "./settings/SettingsProvider";

export function UserDropdown({
  user,
  hideChatAndSearch,
}: {
  user: User | null;
  hideChatAndSearch?: boolean;
}) {
  const [userInfoVisible, setUserInfoVisible] = useState(false);
  const userInfoRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  const combinedSettings = useContext(SettingsContext);
  if (!combinedSettings) {
    return null;
  }
  const settings = combinedSettings.settings;

  // App-wide light/dark toggle (dark is the default; set pre-paint by the inline
  // script in layout.tsx). Available here so every page with the avatar menu can
  // switch, not just the chat sidebar. Flips `.dark` on <html> + persists.
  const [darkMode, setDarkMode] = useState(true);
  useEffect(() => {
    setDarkMode(document.documentElement.classList.contains("dark"));
  }, []);
  const toggleTheme = () => {
    const next = !darkMode;
    setDarkMode(next);
    localStorage.setItem("darwin-theme", next ? "dark" : "light");
    document.documentElement.classList.toggle("dark", next);
  };

  const handleLogout = () => {
    logout().then((isSuccess) => {
      if (!isSuccess) {
        alert("Failed to logout");
      }
      router.push("/auth/login");
    });
  };

  const showAdminPanel = !user || user.role === "admin";
  const showLogout =
    user && !checkUserIsNoAuthUser(user.id) && !LOGOUT_DISABLED;

  return (
    <div className="relative" ref={userInfoRef}>
      <Popover
        open={userInfoVisible}
        onOpenChange={setUserInfoVisible}
        content={
          <BasicSelectable padding={false} selected={false}>
            <div
              onClick={() => setUserInfoVisible(!userInfoVisible)}
              className="flex cursor-pointer"
            >
              <div className="my-auto bg-user hover:bg-user-hover rounded-lg px-2 text-base font-normal">
                {user && user.email ? user.email[0].toUpperCase() : "A"}
              </div>
            </div>
          </BasicSelectable>
        }
        popover={
          <div
            className={`
                text-strong 
                text-sm
                border 
                border-border 
                bg-background
                rounded-lg
                shadow-lg 
                flex 
                flex-col 
                w-full 
                max-h-96 
                overflow-y-auto 
                p-1
                overscroll-contain
              `}
          >
            {!hideChatAndSearch && (
              <>
                {settings.chat_page_enabled && (
                  <>
                    <Link
                      href="/chat"
                      className="flex py-3 px-4 rounded cursor-pointer hover:bg-hover-light"
                    >
                      <FiMessageSquare className="my-auto mr-2 text-lg" />
                      Darwin Chat
                    </Link>
                    <Link
                      href="/assistants/mine"
                      className="flex py-3 px-4 rounded cursor-pointer hover:bg-hover-light"
                    >
                      <FaBrain className="my-auto mr-2 text-lg" />
                      My Assistants
                    </Link>
                  </>
                )}
              </>
            )}
            {showAdminPanel && (
              <>
                {!hideChatAndSearch && (
                  <div className="border-t border-border my-1" />
                )}
                <Link
                  href="/admin/indexing/status"
                  className="flex py-3 px-4 cursor-pointer rounded hover:bg-hover-light"
                >
                  <FiTool className="my-auto mr-2 text-lg" />
                  Admin Panel
                </Link>
              </>
            )}
            <div className="border-t border-border my-1" />
            <div
              onClick={toggleTheme}
              className="flex py-3 px-4 cursor-pointer rounded hover:bg-hover-light"
            >
              {darkMode ? (
                <FiSun className="my-auto mr-2 text-lg" />
              ) : (
                <FiMoon className="my-auto mr-2 text-lg" />
              )}
              {darkMode ? "Light mode" : "Dark mode"}
            </div>
            {showLogout && (
              <>
                <div className="border-t border-border my-1" />
                <div
                  onClick={handleLogout}
                  className="mt-1 flex py-3 px-4 cursor-pointer hover:bg-hover-light"
                >
                  <FiLogOut className="my-auto mr-2 text-lg" />
                  Log out
                </div>
              </>
            )}
          </div>
        }
        side="bottom"
        align="end"
        sideOffset={5}
        alignOffset={-10}
      />
    </div>
  );
}
