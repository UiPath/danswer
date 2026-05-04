"use client";

import { useMemo, useState } from "react";
import { Button, Card, Title, Text } from "@tremor/react";
import { Connector } from "@/lib/types";
import { updateConnector } from "@/lib/connector";
import { usePopup } from "@/components/admin/connectors/Popup";
import { REFRESH_FREQ_OPTIONS } from "@/components/admin/connectors/ConnectorForm";

// Connectors that don't poll (one-shot loads / event-driven). The
// detail page hides the frequency control for these — there's nothing
// to schedule.
const NON_POLLING_INPUT_TYPES = new Set(["load_state", "event"]);

function labelFor(refreshFreq: number | null | undefined): string {
  if (refreshFreq === null || refreshFreq === undefined) return "—";
  const match = REFRESH_FREQ_OPTIONS.find((o) => o.value === refreshFreq);
  if (match) return match.label;
  // Connector was created before the preset list existed (or via API)
  // with an arbitrary value. Show seconds so the user has *some*
  // referent before they pick a preset.
  return `Custom (${refreshFreq}s)`;
}

export function RefreshFrequencyEdit<T>({
  connector,
  onUpdated,
}: {
  connector: Connector<T>;
  onUpdated: () => void;
}) {
  const { popup, setPopup } = usePopup();

  const initialValue = useMemo<number>(() => {
    // If the stored value matches a preset, start there. Otherwise
    // default to "Daily" (the new project default).
    const match = REFRESH_FREQ_OPTIONS.find(
      (o) => o.value === connector.refresh_freq
    );
    return match ? match.value : 60 * 60 * 24;
  }, [connector.refresh_freq]);

  const [selected, setSelected] = useState<number>(initialValue);
  const [isSaving, setIsSaving] = useState(false);

  if (NON_POLLING_INPUT_TYPES.has(connector.input_type)) {
    return null;
  }

  const dirty = selected !== connector.refresh_freq;

  const handleSave = async () => {
    if (!dirty || isSaving) return;
    setIsSaving(true);
    try {
      await updateConnector({
        ...connector,
        refresh_freq: selected,
      });
      setPopup({
        message: `Refresh frequency updated to ${labelFor(selected)}.`,
        type: "success",
      });
      setTimeout(() => setPopup(null), 4000);
      onUpdated();
    } catch (err) {
      setPopup({
        message: `Failed to update refresh frequency: ${err}`,
        type: "error",
      });
      setTimeout(() => setPopup(null), 6000);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <>
      {popup}
      <Title className="mb-2">Refresh frequency</Title>
      <Card>
        <Text className="mb-3">
          How often this connector re-polls the source for new or updated
          documents. Currently:{" "}
          <span className="font-semibold">
            {labelFor(connector.refresh_freq)}
          </span>
          .
        </Text>
        <div className="flex items-center gap-3">
          <select
            value={selected}
            onChange={(e) => setSelected(parseInt(e.target.value, 10))}
            disabled={isSaving}
            className="h-9 rounded-md border border-border bg-background px-2 text-sm"
          >
            {REFRESH_FREQ_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <Button
            size="xs"
            color="green"
            disabled={!dirty || isSaving}
            loading={isSaving}
            onClick={handleSave}
          >
            Save
          </Button>
        </div>
      </Card>
    </>
  );
}
