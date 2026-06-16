import { ChangeEvent, FC, useEffect, useRef, useState } from "react";
import { ChevronDownIcon } from "./icons/icons";
import { FiCheck, FiChevronDown } from "react-icons/fi";
import { Popover } from "./popover/Popover";

export interface Option<T> {
  name: string;
  value: T;
  description?: string;
  metadata?: { [key: string]: any };
  icon?: React.FC<{ size?: number; className?: string }>;
  // Optional haystack the search matches against instead of just `name`.
  // Lets callers make extra fields searchable (e.g. a web connector's URL,
  // which lives in its config, not its display name). Falls back to `name`.
  searchableText?: string;
}

export type StringOrNumberOption = Option<string | number>;

function StandardDropdownOption<T>({
  index,
  option,
  handleSelect,
}: {
  index: number;
  option: Option<T>;
  handleSelect: (option: Option<T>) => void;
}) {
  return (
    <button
      onClick={() => handleSelect(option)}
      className={`w-full text-left block px-4 py-2.5 text-sm hover:bg-gray-800 ${index !== 0 ? " border-t-2 border-gray-600" : ""}`}
      role="menuitem"
    >
      <p className="font-medium">{option.name}</p>
      {option.description && (
        <div>
          <p className="text-xs text-gray-300">{option.description}</p>
        </div>
      )}
    </button>
  );
}

export function SearchMultiSelectDropdown({
  options,
  onSelect,
  itemComponent,
}: {
  options: StringOrNumberOption[];
  onSelect: (selected: StringOrNumberOption) => void;
  itemComponent?: FC<{ option: StringOrNumberOption }>;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const dropdownRef = useRef<HTMLDivElement>(null);

  const handleSelect = (option: StringOrNumberOption) => {
    onSelect(option);
    setIsOpen(false);
    setSearchTerm(""); // Clear search term after selection
  };

  // Search = token-based AND match + relevance ranking over `searchableText`
  // (falling back to `name`). Split the query on whitespace and require every
  // term to appear (any order), then score so the closest matches sort first:
  // exact name > name prefix > contiguous-in-name > contiguous-in-haystack >
  // scattered tokens; ties break toward shorter names and earlier matches.
  // With no query, original order is preserved. Results are capped so a broad
  // query doesn't render hundreds of rows (the best MAX_RENDERED show; the
  // rest are reachable by typing more).
  const MAX_RENDERED = 50;
  const query = searchTerm.trim().toLowerCase();
  const terms = query.split(/\s+/).filter(Boolean);

  const scoreOption = (option: StringOrNumberOption): number => {
    if (terms.length === 0) return 0; // no query → keep all, original order
    const name = option.name.toLowerCase();
    const haystack = (option.searchableText ?? option.name).toLowerCase();
    if (!terms.every((term) => haystack.includes(term))) return -1; // no match
    let score = 1; // base: it matches
    if (name === query) score += 1000;
    else if (name.startsWith(query)) score += 600;
    else if (name.includes(query)) score += 400;
    else if (haystack.startsWith(query)) score += 300;
    else if (haystack.includes(query)) score += 200;
    if (terms.every((term) => name.includes(term))) score += 100; // hits the name, not just config
    const firstPos = haystack.indexOf(terms[0]);
    if (firstPos >= 0) score += (Math.max(0, 60 - firstPos) / 60) * 50; // earlier = better
    score -= name.length * 0.05; // shorter names win ties
    return score;
  };

  const ranked = options
    .map((option) => ({ option, score: scoreOption(option) }))
    .filter((entry) => entry.score >= 0)
    .sort((a, b) => b.score - a.score);
  const totalMatches = ranked.length;
  const filteredOptions = ranked
    .slice(0, MAX_RENDERED)
    .map((entry) => entry.option);
  const hiddenMatchCount = totalMatches - filteredOptions.length;

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    };

    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, []);

  return (
    <div className="relative inline-block text-left w-full" ref={dropdownRef}>
      <div>
        <input
          type="text"
          placeholder="Search..."
          value={searchTerm}
          onChange={(e: ChangeEvent<HTMLInputElement>) => {
            if (!searchTerm) {
              setIsOpen(true);
            }
            if (!e.target.value) {
              setIsOpen(false);
            }
            setSearchTerm(e.target.value);
          }}
          onFocus={() => setIsOpen(true)}
          className={`inline-flex 
          justify-between 
          w-full 
          px-4 
          py-2 
          text-sm 
          bg-background
          border
          border-border
          rounded-md 
          shadow-sm 
          `}
          onClick={(e) => e.stopPropagation()}
        />
        <button
          type="button"
          className={`absolute top-0 right-0 
            text-sm 
            h-full px-2 border-l border-border`}
          aria-expanded="true"
          aria-haspopup="true"
          onClick={() => setIsOpen(!isOpen)}
        >
          <ChevronDownIcon className="my-auto" />
        </button>
      </div>

      {isOpen && (
        <div
          className={`origin-top-right
            absolute
            left-0
            mt-3
            w-full
            rounded-md
            shadow-lg
            bg-background
            border
            border-border
            max-h-80
            overflow-y-auto
            overscroll-contain`}
        >
          <div
            role="menu"
            aria-orientation="vertical"
            aria-labelledby="options-menu"
          >
            {filteredOptions.length ? (
              filteredOptions.map((option, index) =>
                itemComponent ? (
                  <div
                    key={option.name}
                    onClick={() => {
                      setIsOpen(false);
                      handleSelect(option);
                    }}
                  >
                    {itemComponent({ option })}
                  </div>
                ) : (
                  <StandardDropdownOption
                    key={index}
                    option={option}
                    index={index}
                    handleSelect={handleSelect}
                  />
                )
              )
            ) : (
              <button
                key={0}
                className={`w-full text-left block px-4 py-2.5 text-sm hover:bg-hover`}
                role="menuitem"
                onClick={() => setIsOpen(false)}
              >
                No matches found...
              </button>
            )}
            {hiddenMatchCount > 0 && (
              <div className="px-4 py-2 text-xs text-subtle border-t border-border">
                Showing top {filteredOptions.length} of {totalMatches} matches —
                keep typing to narrow.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export const CustomDropdown = ({
  children,
  dropdown,
  direction = "down", // Default to 'down' if not specified
}: {
  children: JSX.Element | string;
  dropdown: JSX.Element | string;
  direction?: "up" | "down";
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    };

    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, []);

  return (
    <div className="relative inline-block text-left w-full" ref={dropdownRef}>
      <div onClick={() => setIsOpen(!isOpen)}>{children}</div>

      {isOpen && (
        <div
          onClick={() => setIsOpen(!isOpen)}
          className={`absolute ${direction === "up" ? "bottom-full pb-2" : "pt-2"} w-full z-30 box-shadow`}
        >
          {dropdown}
        </div>
      )}
    </div>
  );
};

export function DefaultDropdownElement({
  name,
  icon,
  description,
  onSelect,
  isSelected,
  includeCheckbox = false,
}: {
  name: string | JSX.Element;
  icon?: React.FC<{ size?: number; className?: string }>;
  description?: string;
  onSelect?: () => void;
  isSelected?: boolean;
  includeCheckbox?: boolean;
}) {
  return (
    <div
      className={`
        flex
        mx-1
        px-2
        text-sm 
        py-1.5 
        my-1
        select-none 
        cursor-pointer 
        bg-background
        rounded
        hover:bg-hover-light
      `}
      onClick={onSelect}
    >
      <div>
        <div className="flex">
          {includeCheckbox && (
            <input
              type="checkbox"
              className="mr-2"
              checked={isSelected}
              onChange={() => null}
            />
          )}
          {icon && icon({ size: 16, className: "mr-2 h-4 w-4 my-auto" })}
          {name}
        </div>
        {description && <div className="text-xs">{description}</div>}
      </div>
      {isSelected && (
        <div className="ml-auto mr-1 my-auto">
          <FiCheck />
        </div>
      )}
    </div>
  );
}

export function DefaultDropdown({
  options,
  selected,
  onSelect,
  includeDefault = false,
  side,
  maxHeight,
  defaultValue,
}: {
  options: StringOrNumberOption[];
  selected: string | null;
  onSelect: (value: string | number | null) => void;
  includeDefault?: boolean;
  defaultValue?: string;
  side?: "top" | "right" | "bottom" | "left";
  maxHeight?: string;
}) {
  const selectedOption = options.find((option) => option.value === selected);
  const [isOpen, setIsOpen] = useState(false);

  const Content = (
    <div
      className={`
      flex 
      text-sm 
      bg-background 
      px-3
      py-1.5 
      rounded-lg 
      border 
      border-border 
      cursor-pointer`}
    >
      <p className="line-clamp-1">
        {selectedOption?.name ||
          (includeDefault ? defaultValue ?? "Default" : "Select an option...")}
      </p>
      <FiChevronDown className="my-auto ml-auto" />
    </div>
  );

  const Dropdown = (
    <div
      className={`
        border 
        border 
        rounded-lg 
        flex 
        flex-col 
        bg-background
        ${maxHeight || "max-h-96"}
        overflow-y-auto 
        overscroll-contain`}
    >
      {includeDefault && (
        <DefaultDropdownElement
          key={-1}
          name="Default"
          onSelect={() => {
            onSelect(null);
          }}
          isSelected={selected === null}
        />
      )}
      {options.map((option, ind) => {
        const isSelected = option.value === selected;
        return (
          <DefaultDropdownElement
            key={option.value}
            name={option.name}
            description={option.description}
            onSelect={() => onSelect(option.value)}
            isSelected={isSelected}
            icon={option.icon}
          />
        );
      })}
    </div>
  );

  return (
    <div onClick={() => setIsOpen(!isOpen)}>
      <Popover
        open={isOpen}
        onOpenChange={(open) => setIsOpen(open)}
        content={Content}
        popover={Dropdown}
        align="start"
        side={side}
        sideOffset={5}
        matchWidth
        triggerMaxWidth
      />
    </div>
  );
}

export function ControlledPopup({
  children,
  popupContent,
  isOpen,
  setIsOpen,
}: {
  children: JSX.Element | string;
  popupContent: JSX.Element | string;
  isOpen: boolean;
  setIsOpen: (value: boolean) => void;
}) {
  const filtersRef = useRef<HTMLDivElement>(null);
  // hides logout popup on any click outside
  const handleClickOutside = (event: MouseEvent) => {
    if (
      filtersRef.current &&
      !filtersRef.current.contains(event.target as Node)
    ) {
      setIsOpen(false);
    }
  };

  useEffect(() => {
    document.addEventListener("mousedown", handleClickOutside);

    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, []);

  return (
    <div ref={filtersRef} className="relative">
      {children}
      {isOpen && (
        <div
          className={`
            absolute 
            top-0 
            bg-background 
            border 
            border-border 
            z-30 
            rounded 
            text-emphasis 
            shadow-lg`}
          style={{ transform: "translateY(calc(-100% - 5px))" }}
        >
          {popupContent}
        </div>
      )}
    </div>
  );
}
