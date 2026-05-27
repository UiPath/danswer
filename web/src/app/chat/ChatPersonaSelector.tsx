import { Persona } from "@/app/admin/assistants/interfaces";
import { FiCheck, FiChevronDown, FiPlusSquare, FiEdit2, FiSearch } from "react-icons/fi";
import { CustomDropdown, DefaultDropdownElement } from "@/components/Dropdown";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { checkUserIdOwnsAssistant } from "@/lib/assistants/checkOwnership";

// The default persona (id=0 in personas.yaml) has no document_set scope and
// behaves like a generic search-everything mode. Surface it as "Search" in
// the trigger so users don't have to think about it as an "assistant."
const DEFAULT_PERSONA_ID = 0;

function isSearchModePersona(persona: Persona | undefined): boolean {
  return (
    !!persona &&
    persona.id === DEFAULT_PERSONA_ID &&
    (persona.document_sets?.length ?? 0) === 0
  );
}

function PersonaItem({
  id,
  name,
  onSelect,
  isSelected,
  isOwner,
}: {
  id: number;
  name: string;
  onSelect: (personaId: number) => void;
  isSelected: boolean;
  isOwner: boolean;
}) {
  return (
    <div className="flex w-full">
      <div
        key={id}
        className={`
          flex
          flex-grow
          px-3 
          text-sm 
          py-2 
          my-0.5
          rounded
          mx-1
          select-none 
          cursor-pointer 
          text-emphasis
          bg-background
          hover:bg-hover-light
          ${isSelected ? "bg-hover text-selected-emphasis" : ""}
        `}
        onClick={() => {
          onSelect(id);
        }}
      >
        {name}
        {isSelected && (
          <div className="ml-auto mr-1 my-auto">
            <FiCheck />
          </div>
        )}
      </div>
      {isOwner && (
        <Link href={`/assistants/edit/${id}`} className="mx-2 my-auto">
          <FiEdit2 className="hover:bg-hover p-0.5 my-auto" size={20} />
        </Link>
      )}
    </div>
  );
}

export function ChatPersonaSelector({
  personas,
  selectedPersonaId,
  onPersonaChange,
  userId,
}: {
  personas: Persona[];
  selectedPersonaId: number | null;
  onPersonaChange: (persona: Persona | null) => void;
  userId: string | undefined;
}) {
  const router = useRouter();

  const currentlySelectedPersona = personas.find(
    (persona) => persona.id === selectedPersonaId
  );

  return (
    <CustomDropdown
      dropdown={
        <div
          className={`
            border 
            border-border 
            bg-background
            rounded-lg 
            shadow-lg 
            flex 
            flex-col 
            w-64 
            max-h-96 
            overflow-y-auto 
            p-1
            overscroll-contain`}
        >
          {personas.map((persona) => {
            const isSelected = persona.id === selectedPersonaId;
            const isOwner = checkUserIdOwnsAssistant(userId, persona);
            return (
              <PersonaItem
                key={persona.id}
                id={persona.id}
                name={persona.name}
                onSelect={(clickedPersonaId) => {
                  const clickedPersona = personas.find(
                    (persona) => persona.id === clickedPersonaId
                  );
                  if (clickedPersona) {
                    onPersonaChange(clickedPersona);
                  }
                }}
                isSelected={isSelected}
                isOwner={isOwner}
              />
            );
          })}

          <div className="border-t border-border pt-2">
            <DefaultDropdownElement
              name={
                <div className="flex items-center">
                  <FiPlusSquare className="mr-2" />
                  New Assistant
                </div>
              }
              onSelect={() => router.push("/assistants/new")}
              isSelected={false}
            />
          </div>
        </div>
      }
    >
      <div className="select-none text-xl text-strong font-bold flex items-center px-2 rounded cursor-pointer hover:bg-hover-light">
        {isSearchModePersona(currentlySelectedPersona) ? (
          <>
            <FiSearch className="mr-2" />
            <span>Search</span>
          </>
        ) : (
          <span>{currentlySelectedPersona?.name || "Default"}</span>
        )}
        <FiChevronDown className="my-auto ml-1" />
      </div>
    </CustomDropdown>
  );
}
