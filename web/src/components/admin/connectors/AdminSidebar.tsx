// Sidebar.tsx
"use client";

import React from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

interface Item {
  name: string | JSX.Element;
  link: string;
}

interface Collection {
  name: string | JSX.Element;
  items: Item[];
}

export function AdminSidebar({ collections }: { collections: Collection[] }) {
  // Highlight the current item. Compare path + query (not just pathname) so the
  // tabs that share a route but differ only by query — Existing Connectors
  // (/admin/indexing/status), Indexing Activity (?status=active), Failed
  // Indexing (?status=failed) — each light up correctly. Without an active
  // state every tab looked identical, so a click gave no feedback.
  const pathname = usePathname();
  const search = useSearchParams().toString();
  const current = `${pathname}${search ? `?${search}` : ""}`;

  return (
    <aside className="pl-4">
      <nav className="space-y-2 pl-4">
        {collections.map((collection, collectionInd) => (
          <div key={collectionInd}>
            <h2 className="text-xs text-strong font-bold pb-2 ">
              <div>{collection.name}</div>
            </h2>
            {collection.items.map((item) => {
              const isActive = current === item.link;
              return (
                <Link key={item.link} href={item.link}>
                  <button
                    aria-current={isActive ? "page" : undefined}
                    className={
                      "text-sm block w-48 py-2 px-2 text-left rounded " +
                      (isActive
                        ? "bg-hover font-semibold text-strong"
                        : "hover:bg-hover")
                    }
                  >
                    <div className="">{item.name}</div>
                  </button>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
