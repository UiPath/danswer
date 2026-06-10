import { Persona } from "@/app/admin/assistants/interfaces";
import { Bubble } from "@/components/Bubble";
import { AssistantIcon } from "@/components/assistants/AssistantIcon";
import React, { useState } from "react";
import { FiBookmark, FiImage, FiSearch } from "react-icons/fi";

interface AssistantsTabProps {
  selectedAssistant: Persona;
  availableAssistants: Persona[];
  onSelect: (assistant: Persona) => void;
}

export function AssistantsTab({
  selectedAssistant,
  availableAssistants,
  onSelect,
}: AssistantsTabProps) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const filteredAssistants = availableAssistants.filter(
    (assistant) =>
      !q ||
      assistant.name.toLowerCase().includes(q) ||
      (assistant.description?.toLowerCase().includes(q) ?? false)
  );

  return (
    <>
      <h3 className="text-lg font-semibold">Choose Assistant</h3>

      <div className="mt-2 flex items-center rounded border border-border bg-background px-2.5 py-1.5">
        <FiSearch className="text-subtle mr-2 shrink-0" />
        <input
          autoFocus
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search assistants…"
          className="w-full bg-transparent text-sm text-default placeholder:text-subtle focus:outline-none"
        />
      </div>

      <div className="my-3 grid grid-cols-1 gap-4">
        {filteredAssistants.length === 0 && (
          <div className="text-sm text-subtle">
            No assistants match “{query}”.
          </div>
        )}
        {filteredAssistants.map((assistant) => (
          <div
            key={assistant.id}
            className={`
              cursor-pointer
              p-4
              border
              rounded-lg
              shadow-md
              hover:bg-hover-light
              ${
                selectedAssistant.id === assistant.id
                  ? "border-accent"
                  : "border-border"
              }
            `}
            onClick={() => onSelect(assistant)}
          >
            <div className="flex items-center mb-2">
              <AssistantIcon assistant={assistant} />
              <div className="ml-2 font-bold text-lg text-emphasis">
                {assistant.name}
              </div>
            </div>
            {assistant.tools.length > 0 && (
              <div className="text-xs text-subtle flex flex-wrap gap-2">
                {assistant.tools.map((tool) => {
                  let toolName = tool.name;
                  let toolIcon = null;

                  if (tool.name === "SearchTool") {
                    toolName = "Search";
                    toolIcon = <FiSearch className="mr-1 my-auto" />;
                  } else if (tool.name === "ImageGenerationTool") {
                    toolName = "Image Generation";
                    toolIcon = <FiImage className="mr-1 my-auto" />;
                  }

                  return (
                    <Bubble key={tool.id} isSelected={false}>
                      <div className="flex flex-row gap-1">
                        {toolIcon}
                        {toolName}
                      </div>
                    </Bubble>
                  );
                })}
              </div>
            )}
            <div className="text-sm text-subtle mb-2 mt-2">
              {assistant.description}
            </div>
            {assistant.document_sets.length > 0 && (
              <div className="mt-2 text-xs text-subtle flex flex-wrap gap-2">
                <p className="my-auto font-medium">Document Sets:</p>
                {assistant.document_sets.map((set) => (
                  <Bubble key={set.id} isSelected={false}>
                    <div className="flex flex-row gap-1">
                      <FiBookmark className="mr-1 my-auto" />
                      {set.name}
                    </div>
                  </Bubble>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
