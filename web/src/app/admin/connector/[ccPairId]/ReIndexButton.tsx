"use client";

import { PopupSpec, usePopup } from "@/components/admin/connectors/Popup";
import { runConnector } from "@/lib/connector";
import { Button, Divider, Text } from "@tremor/react";
import { useRouter } from "next/navigation";
import { mutate } from "swr";
import { buildCCPairInfoUrl } from "./lib";
import { useState } from "react";
import { Modal } from "@/components/Modal";

function ReIndexPopup({
  connectorId,
  credentialId,
  ccPairId,
  setPopup,
  hide,
}: {
  connectorId: number;
  credentialId: number;
  ccPairId: number;
  setPopup: (popupSpec: PopupSpec | null) => void;
  hide: () => void;
}) {
  const [priority, setPriority] = useState<number>(0);

  async function triggerIndexing(fromBeginning: boolean) {
    const clamped = Math.max(0, Math.min(100, Math.floor(priority || 0)));
    const errorMsg = await runConnector(
      connectorId,
      [credentialId],
      fromBeginning,
      clamped
    );
    if (errorMsg) {
      setPopup({
        message: errorMsg,
        type: "error",
      });
    } else {
      setPopup({
        message:
          clamped > 0
            ? `Triggered connector run with priority ${clamped}`
            : "Triggered connector run",
        type: "success",
      });
    }
    mutate(buildCCPairInfoUrl(ccPairId));
  }

  return (
    <Modal title="Run Indexing" onOutsideClick={hide}>
      <div>
        <div className="flex items-center gap-2 mb-3">
          <label className="text-sm">Priority:</label>
          <input
            type="number"
            min={0}
            max={100}
            step={10}
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
            className="border rounded px-2 py-1 w-20 bg-background text-sm"
          />
          <Text className="text-xs text-subtle">
            0 = normal (default). Higher values jump the queue ahead of other
            queued runs without affecting them. Steps of 10; conventional
            ceiling: 100.
          </Text>
        </div>

        <Button
          className="ml-auto"
          color="green"
          size="xs"
          onClick={() => {
            triggerIndexing(false);
            hide();
          }}
        >
          Run Update
        </Button>

        <Text className="mt-2">
          This will pull in and index all documents that have changed and/or
          have been added since the last successful indexing run.
        </Text>

        <Divider />

        <Button
          className="ml-auto"
          color="green"
          size="xs"
          onClick={() => {
            triggerIndexing(true);
            hide();
          }}
        >
          Run Complete Re-Indexing
        </Button>

        <Text className="mt-2">
          This will cause a complete re-indexing of all documents from the
          source.
        </Text>

        <Text className="mt-2">
          <b>NOTE:</b> depending on the number of documents stored in the
          source, this may take a long time.
        </Text>
      </div>
    </Modal>
  );
}

export function ReIndexButton({
  ccPairId,
  connectorId,
  credentialId,
  isDisabled,
}: {
  ccPairId: number;
  connectorId: number;
  credentialId: number;
  isDisabled: boolean;
}) {
  const { popup, setPopup } = usePopup();
  const [reIndexPopupVisible, setReIndexPopupVisible] = useState(false);

  return (
    <>
      {reIndexPopupVisible && (
        <ReIndexPopup
          connectorId={connectorId}
          credentialId={credentialId}
          ccPairId={ccPairId}
          setPopup={setPopup}
          hide={() => setReIndexPopupVisible(false)}
        />
      )}
      {popup}
      <Button
        className="ml-auto"
        color="green"
        size="xs"
        onClick={() => {
          setReIndexPopupVisible(true);
        }}
        disabled={isDisabled}
        tooltip={
          isDisabled
            ? "Connector must be active in order to run indexing"
            : undefined
        }
      >
        Run Indexing
      </Button>
    </>
  );
}
