"use client";

import { useEffect, useRef, useState } from "react";
import { FiCheck, FiEdit2, FiX } from "react-icons/fi";
import { usePopup } from "@/components/admin/connectors/Popup";

export function CCPairNameEdit({
  ccPairId,
  name,
  onUpdated,
}: {
  ccPairId: number;
  name: string;
  onUpdated: () => void;
}) {
  const { popup, setPopup } = usePopup();
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!isEditing) {
      setDraft(name);
    }
  }, [name, isEditing]);

  useEffect(() => {
    if (isEditing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [isEditing]);

  const cancel = () => {
    setDraft(name);
    setIsEditing(false);
  };

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed) {
      setPopup({ message: "Name cannot be empty", type: "error" });
      setTimeout(() => setPopup(null), 4000);
      return;
    }
    if (trimmed === name) {
      setIsEditing(false);
      return;
    }
    setIsSaving(true);
    try {
      const response = await fetch(
        `/api/manage/admin/cc-pair/${ccPairId}/name`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: trimmed }),
        }
      );
      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(errorBody || `HTTP ${response.status}`);
      }
      setIsEditing(false);
      setPopup({ message: "Connector renamed", type: "success" });
      setTimeout(() => setPopup(null), 3000);
      onUpdated();
    } catch (err) {
      setPopup({ message: `Failed to rename: ${err}`, type: "error" });
      setTimeout(() => setPopup(null), 5000);
    } finally {
      setIsSaving(false);
    }
  };

  if (!isEditing) {
    return (
      <>
        {popup}
        <div className="flex items-center gap-2">
          <h1 className="text-3xl text-emphasis font-bold">{name}</h1>
          <button
            type="button"
            className="p-1.5 rounded hover:bg-hover text-subtle"
            title="Rename connector"
            onClick={() => setIsEditing(true)}
          >
            <FiEdit2 size={16} />
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      {popup}
      <div className="flex items-center gap-2">
        <input
          ref={inputRef}
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") cancel();
          }}
          disabled={isSaving}
          className="text-3xl font-bold text-emphasis bg-background border border-border rounded px-2 py-0.5 w-full max-w-[28rem] focus:outline-none focus:ring-2 focus:ring-accent"
        />
        <button
          type="button"
          className="p-1.5 rounded hover:bg-hover text-emphasis disabled:opacity-50"
          title="Save"
          onClick={save}
          disabled={isSaving}
        >
          <FiCheck size={18} />
        </button>
        <button
          type="button"
          className="p-1.5 rounded hover:bg-hover text-subtle disabled:opacity-50"
          title="Cancel"
          onClick={cancel}
          disabled={isSaving}
        >
          <FiX size={18} />
        </button>
      </div>
    </>
  );
}
