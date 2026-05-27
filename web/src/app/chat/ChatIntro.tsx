import { getSourceMetadataForSources, listSourceMetadata } from "@/lib/sources";
import { ValidSources } from "@/lib/types";
import Image from "next/image";
import { Persona } from "../admin/assistants/interfaces";
import { Divider } from "@tremor/react";
import {
  FiBookmark,
  FiChevronRight,
  FiCpu,
  FiFilter,
  FiInfo,
  FiUser,
  FiX,
  FiZoomIn,
} from "react-icons/fi";
import { IconType } from "react-icons";
import { HoverPopup } from "@/components/HoverPopup";
import { Modal } from "@/components/Modal";
import { useState } from "react";
import { Logo } from "@/components/Logo";

const MAX_PERSONAS_TO_DISPLAY = 4;

function HelperItemDisplay({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="cursor-pointer hover:bg-hover-light border border-border rounded py-2 px-4">
      <div className="text-emphasis font-bold text-lg flex">{title}</div>
      <div className="text-sm">{description}</div>
    </div>
  );
}

function StepCard({
  icon: Icon,
  title,
  subtitle,
  onClick,
}: {
  icon: IconType;
  title: string;
  subtitle: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="
        group
        text-left
        rounded-lg
        border border-border
        bg-background
        hover:bg-hover-light
        hover:border-accent
        transition-colors
        px-4 py-3
        flex flex-col gap-1
      "
    >
      <Icon className="text-accent" size={20} />
      <div className="font-semibold text-sm text-emphasis">{title}</div>
      <div className="text-xs text-subtle">{subtitle}</div>
    </button>
  );
}

function OnboardingSteps({
  setConfigModalActiveTab,
}: {
  setConfigModalActiveTab: (tab: string | null) => void;
}) {
  return (
    <div className="mt-6 grid grid-cols-3 gap-3 items-stretch">
      <StepCard
        icon={FiUser}
        title="Assistant"
        subtitle="grounds context"
        onClick={() => setConfigModalActiveTab("assistants")}
      />
      <StepCard
        icon={FiFilter}
        title="Filters"
        subtitle="narrow sources"
        onClick={() => setConfigModalActiveTab("filters")}
      />
      <StepCard
        icon={FiCpu}
        title="Model"
        subtitle="tune reasoning"
        onClick={() => setConfigModalActiveTab("llms")}
      />
    </div>
  );
}

export function ChatIntro({
  availableSources,
  selectedPersona,
  setConfigModalActiveTab,
}: {
  availableSources: ValidSources[];
  selectedPersona: Persona;
  setConfigModalActiveTab?: (tab: string | null) => void;
}) {
  const availableSourceMetadata = getSourceMetadataForSources(availableSources);

  const [displaySources, setDisplaySources] = useState(false);

  return (
    <>
      <div className="flex justify-center items-center h-full">
        <div className="w-message-xs 2xl:w-message-sm 3xl:w-message">
          <div className="flex">
            <div className="mx-auto">
              <Logo height={80} width={80} className="m-auto" />

              <div className="m-auto text-3xl font-bold text-strong mt-4 w-fit">
                {selectedPersona?.name || "How can I help you today?"}
              </div>
              {selectedPersona && (
                <div className="mt-1">{selectedPersona.description}</div>
              )}
            </div>
          </div>

          {setConfigModalActiveTab && (
            <OnboardingSteps
              setConfigModalActiveTab={setConfigModalActiveTab}
            />
          )}

          {selectedPersona && selectedPersona.num_chunks !== 0 && (
            <>
              <Divider />
              <div>
                {selectedPersona.document_sets.length > 0 && (
                  <div className="mt-2">
                    <p className="font-bold mb-1 mt-4 text-emphasis">
                      Knowledge Sets:{" "}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {selectedPersona.document_sets.map((documentSet) => (
                        <div key={documentSet.id} className="w-fit">
                          <HoverPopup
                            mainContent={
                              <span className="flex w-fit p-1 rounded border border-border text-xs font-medium cursor-default">
                                <div className="mr-1 my-auto">
                                  <FiBookmark />
                                </div>
                                {documentSet.name}
                              </span>
                            }
                            popupContent={
                              <div className="flex py-1 w-96">
                                <FiInfo className="my-auto mr-2" />
                                <div className="text-sm">
                                  {documentSet.description}
                                </div>
                              </div>
                            }
                            direction="top"
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {availableSources.length > 0 && (
                  <div className="mt-1">
                    <p className="font-bold mb-1 mt-4 text-emphasis">
                      Connected Sources:{" "}
                    </p>
                    <div className={`flex flex-wrap gap-2`}>
                      {availableSourceMetadata.map((sourceMetadata) => (
                        <span
                          key={sourceMetadata.internalName}
                          className="flex w-fit p-1 rounded border border-border text-xs font-medium cursor-default"
                        >
                          <div className="mr-1 my-auto">
                            {sourceMetadata.icon({})}
                          </div>
                          <div className="my-auto">
                            {sourceMetadata.displayName}
                          </div>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
