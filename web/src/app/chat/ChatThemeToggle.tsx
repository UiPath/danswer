"use client";

import { useEffect, useState } from "react";
import { FiMoon, FiSun } from "react-icons/fi";
import { BasicClickable } from "@/components/BasicClickable";

const STORAGE_KEY = "darwin-theme";

function isDark(): boolean {
  return document.documentElement.classList.contains("dark");
}

function applyDark(on: boolean): void {
  document.documentElement.classList.toggle("dark", on);
}

/**
 * App-wide light/dark toggle. Dark is the default (set pre-paint by the inline
 * script in layout.tsx); this persists an explicit choice to localStorage and
 * flips `.dark` on <html>, which drives the CSS-variable token palette across
 * every page.
 */
export function ChatThemeToggle() {
  const [dark, setDark] = useState(true);

  useEffect(() => {
    setDark(isDark());
  }, []);

  const toggle = () => {
    const next = !dark;
    setDark(next);
    localStorage.setItem(STORAGE_KEY, next ? "dark" : "light");
    applyDark(next);
  };

  return (
    <BasicClickable fullWidth onClick={toggle}>
      <div className="flex items-center text-default font-medium">
        {dark ? (
          <FiSun className="ml-1 mr-2" />
        ) : (
          <FiMoon className="ml-1 mr-2" />
        )}
        {dark ? "Light mode" : "Dark mode"}
      </div>
    </BasicClickable>
  );
}
