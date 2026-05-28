import { LlmOverrideManager } from "@/lib/hooks";
import React, { useCallback, useState } from "react";
import { debounce } from "lodash";
import { DefaultDropdown } from "@/components/Dropdown";
import { Text } from "@tremor/react";
import { Persona } from "@/app/admin/assistants/interfaces";
import { destructureValue, structureValue } from "@/lib/llm/utils";
import { updateModelOverrideForChatSession } from "../../lib";
import { FLAT_LLM_MODELS } from "@/lib/llm/models";

export function LlmTab({
  llmOverrideManager,
  currentAssistant,
  chatSessionId,
}: {
  llmOverrideManager: LlmOverrideManager;
  currentAssistant: Persona;
  chatSessionId?: number;
}) {
  const { llmOverride, setLlmOverride, temperature, setTemperature } =
    llmOverrideManager;

  const [localTemperature, setLocalTemperature] = useState<number>(
    temperature || 0
  );

  const debouncedSetTemperature = useCallback(
    debounce((value) => {
      setTemperature(value);
    }, 300),
    []
  );

  const handleTemperatureChange = (value: number) => {
    setLocalTemperature(value);
    debouncedSetTemperature(value);
  };

  const llmOptions = FLAT_LLM_MODELS.map((m) => ({
    name: `${m.vendorLabel} — ${m.modelLabel}`,
    value: structureValue(m.vendorLabel, m.vendorKey, m.modelId),
  }));

  return (
    <div className="mb-4">
      <label className="block text-sm font-medium mb-2">Choose Model</label>
      <Text className="mb-3">
        Select the model to use for this chat session. The selection applies
        only to this session.
      </Text>

      <div className="w-96">
        <DefaultDropdown
          options={llmOptions}
          selected={structureValue(
            llmOverride.name,
            llmOverride.provider,
            llmOverride.modelName
          )}
          onSelect={(value) => {
            setLlmOverride(destructureValue(value as string));
            if (chatSessionId) {
              updateModelOverrideForChatSession(chatSessionId, value as string);
            }
          }}
        />
      </div>

      <label className="block text-sm font-medium mb-2 mt-4">Temperature</label>

      <Text className="mb-8">
        Adjust the temperature of the LLM. Higher temperature will make the LLM
        generate more creative and diverse responses, while lower temperature
        will make the LLM generate more conservative and focused responses.
      </Text>

      <div className="relative w-full">
        <input
          type="range"
          onChange={(e) => handleTemperatureChange(parseFloat(e.target.value))}
          className="
            w-full
            p-2
            border
            border-border
            rounded-md
          "
          min="0"
          max="2"
          step="0.01"
          value={localTemperature}
        />
        <div
          className="absolute text-sm"
          style={{
            left: `${(localTemperature || 0) * 50}%`,
            transform: `translateX(-${Math.min(
              Math.max((localTemperature || 0) * 50, 10),
              90
            )}%)`,
            top: "-1.5rem",
          }}
        >
          {localTemperature}
        </div>
      </div>
    </div>
  );
}
