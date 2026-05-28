import React, { useEffect, useRef, useState } from "react";
import { DocumentSet, ValidSources } from "@/lib/types";
import { SourceMetadata } from "@/lib/search/interfaces";
import {
  FiBook,
  FiBookmark,
  FiCalendar,
  FiFilter,
  FiMap,
  FiX,
} from "react-icons/fi";
import { DateRangePickerValue } from "@tremor/react";
import { listSourceMetadata } from "@/lib/sources";
import { SourceIcon } from "@/components/SourceIcon";
import { BasicClickable } from "@/components/BasicClickable";
import { ControlledPopup, DefaultDropdownElement } from "@/components/Dropdown";
import { getXDaysAgo } from "@/lib/dateUtils";
import { SourceSelectorProps } from "@/components/search/filtering/Filters";

enum FilterType {
  Source = "Source",
  KnowledgeSet = "Knowledge Set",
  TimeRange = "Time Range",
}

function SelectedBubble({
  children,
  onClick,
}: {
  children: string | JSX.Element;
  onClick: () => void;
}) {
  return (
    <div
      className={
        "flex text-xs cursor-pointer items-center border border-border " +
        "py-1 rounded-lg px-2 w-fit select-none hover:bg-hover"
      }
      onClick={onClick}
    >
      {children}
      <FiX className="ml-2" size={14} />
    </div>
  );
}

function SelectFilterType({
  onSelect,
  hasSources,
  hasKnowledgeSets,
}: {
  onSelect: (filterType: FilterType) => void;
  hasSources: boolean;
  hasKnowledgeSets: boolean;
}) {
  return (
    <div className="w-64">
      {hasSources && (
        <DefaultDropdownElement
          key={FilterType.Source}
          name={FilterType.Source}
          icon={FiMap}
          onSelect={() => onSelect(FilterType.Source)}
          isSelected={false}
        />
      )}

      {hasKnowledgeSets && (
        <DefaultDropdownElement
          key={FilterType.KnowledgeSet}
          name={FilterType.KnowledgeSet}
          icon={FiBook}
          onSelect={() => onSelect(FilterType.KnowledgeSet)}
          isSelected={false}
        />
      )}

      <DefaultDropdownElement
        key={FilterType.TimeRange}
        name={FilterType.TimeRange}
        icon={FiCalendar}
        onSelect={() => onSelect(FilterType.TimeRange)}
        isSelected={false}
      />
    </div>
  );
}

function SourcesSection({
  sources,
  selectedSources,
  onSelect,
}: {
  sources: SourceMetadata[];
  selectedSources: string[];
  onSelect: (source: SourceMetadata) => void;
}) {
  return (
    <div className="w-64">
      {sources.map((source) => (
        <DefaultDropdownElement
          key={source.internalName}
          name={source.displayName}
          icon={source.icon}
          onSelect={() => onSelect(source)}
          isSelected={selectedSources.includes(source.internalName)}
          includeCheckbox
        />
      ))}
    </div>
  );
}

function KnowledgeSetsSection({
  documentSets,
  selectedDocumentSets,
  onSelect,
}: {
  documentSets: DocumentSet[];
  selectedDocumentSets: string[];
  onSelect: (documentSetName: string) => void;
}) {
  const [filterValue, setFilterValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, []);

  const filterValueLower = filterValue.toLowerCase();
  const filteredDocumentSets = filterValueLower
    ? documentSets.filter((ds) =>
        ds.name.toLowerCase().includes(filterValueLower)
      )
    : documentSets;

  return (
    <div className="w-72">
      <div className="max-h-72 overflow-y-auto">
        {filteredDocumentSets.length > 0 ? (
          filteredDocumentSets.map((documentSet) => (
            <DefaultDropdownElement
              key={documentSet.name}
              name={documentSet.name}
              icon={FiBookmark}
              onSelect={() => onSelect(documentSet.name)}
              isSelected={selectedDocumentSets.includes(documentSet.name)}
              includeCheckbox
            />
          ))
        ) : (
          <div className="text-sm px-2 py-2 text-subtle">
            No matching knowledge sets
          </div>
        )}
      </div>

      <div className="mx-2 mb-2 pt-2 border-t border-border">
        <input
          ref={inputRef}
          className="w-full border border-border py-0.5 px-2 rounded text-sm h-8"
          placeholder={`Find a knowledge set (${documentSets.length})`}
          value={filterValue}
          onChange={(event) => setFilterValue(event.target.value)}
        />
      </div>
    </div>
  );
}

const LAST_30_DAYS = "Last 30 days";
const LAST_7_DAYS = "Last 7 days";
const TODAY = "Today";

function TimeRangeSection({
  selectedTimeRange,
  onSelect,
}: {
  selectedTimeRange: string | null;
  onSelect: (timeRange: DateRangePickerValue) => void;
}) {
  return (
    <div className="w-64">
      <DefaultDropdownElement
        key={LAST_30_DAYS}
        name={LAST_30_DAYS}
        onSelect={() =>
          onSelect({
            to: new Date(),
            from: getXDaysAgo(30),
            selectValue: LAST_30_DAYS,
          })
        }
        isSelected={selectedTimeRange === LAST_30_DAYS}
      />

      <DefaultDropdownElement
        key={LAST_7_DAYS}
        name={LAST_7_DAYS}
        onSelect={() =>
          onSelect({
            to: new Date(),
            from: getXDaysAgo(7),
            selectValue: LAST_7_DAYS,
          })
        }
        isSelected={selectedTimeRange === LAST_7_DAYS}
      />

      <DefaultDropdownElement
        key={TODAY}
        name={TODAY}
        onSelect={() =>
          onSelect({
            to: new Date(),
            from: getXDaysAgo(1),
            selectValue: TODAY,
          })
        }
        isSelected={selectedTimeRange === TODAY}
      />
    </div>
  );
}

export function ChatFilters({
  timeRange,
  setTimeRange,
  selectedSources,
  setSelectedSources,
  selectedDocumentSets,
  setSelectedDocumentSets,
  availableDocumentSets,
  existingSources,
}: SourceSelectorProps) {
  const [filtersOpen, setFiltersOpen] = useState(false);
  const handleFiltersToggle = (value: boolean) => {
    setSelectedFilterType(null);
    setFiltersOpen(value);
  };
  const [selectedFilterType, setSelectedFilterType] =
    useState<FilterType | null>(null);

  const handleSourceSelect = (source: SourceMetadata) => {
    setSelectedSources((prev: SourceMetadata[]) => {
      const prevSourceNames = prev.map((source) => source.internalName);
      if (prevSourceNames.includes(source.internalName)) {
        return prev.filter((s) => s.internalName !== source.internalName);
      } else {
        return [...prev, source];
      }
    });
  };

  const handleDocumentSetSelect = (documentSetName: string) => {
    setSelectedDocumentSets((prev: string[]) => {
      if (prev.includes(documentSetName)) {
        return prev.filter((s) => s !== documentSetName);
      } else {
        return [...prev, documentSetName];
      }
    });
  };

  const allSources = listSourceMetadata();
  const availableSources = allSources.filter((source) =>
    existingSources.includes(source.internalName)
  );

  let popupDisplay = null;
  if (selectedFilterType === FilterType.Source) {
    popupDisplay = (
      <SourcesSection
        sources={availableSources}
        selectedSources={selectedSources.map((source) => source.internalName)}
        onSelect={handleSourceSelect}
      />
    );
  } else if (selectedFilterType === FilterType.KnowledgeSet) {
    popupDisplay = (
      <KnowledgeSetsSection
        documentSets={availableDocumentSets}
        selectedDocumentSets={selectedDocumentSets}
        onSelect={handleDocumentSetSelect}
      />
    );
  } else if (selectedFilterType === FilterType.TimeRange) {
    popupDisplay = (
      <TimeRangeSection
        selectedTimeRange={timeRange?.selectValue || null}
        onSelect={(timeRange) => {
          setTimeRange(timeRange);
          handleFiltersToggle(!filtersOpen);
        }}
      />
    );
  } else {
    popupDisplay = (
      <SelectFilterType
        onSelect={(filterType) => setSelectedFilterType(filterType)}
        hasSources={availableSources.length > 0}
        hasKnowledgeSets={availableDocumentSets.length > 0}
      />
    );
  }

  return (
    <div className="flex">
      <ControlledPopup
        isOpen={filtersOpen}
        setIsOpen={handleFiltersToggle}
        popupContent={popupDisplay}
      >
        <div className="flex">
          <BasicClickable onClick={() => handleFiltersToggle(!filtersOpen)}>
            <div className="flex text-xs">
              <FiFilter className="my-auto mr-1" /> Filter
            </div>
          </BasicClickable>
        </div>
      </ControlledPopup>

      <div className="flex ml-4">
        {((timeRange && timeRange.selectValue !== undefined) ||
          selectedSources.length > 0 ||
          selectedDocumentSets.length > 0) && (
          <p className="text-xs my-auto mr-1">Currently applied:</p>
        )}
        <div className="flex flex-wrap gap-x-2">
          {timeRange && timeRange.selectValue && (
            <SelectedBubble onClick={() => setTimeRange(null)}>
              <div className="flex">{timeRange.selectValue}</div>
            </SelectedBubble>
          )}
          {existingSources.length > 0 &&
            selectedSources.map((source) => (
              <SelectedBubble
                key={source.internalName}
                onClick={() => handleSourceSelect(source)}
              >
                <>
                  <SourceIcon sourceType={source.internalName} iconSize={16} />
                  <span className="ml-2">{source.displayName}</span>
                </>
              </SelectedBubble>
            ))}
          {selectedDocumentSets.length > 0 &&
            selectedDocumentSets.map((documentSetName) => (
              <SelectedBubble
                key={documentSetName}
                onClick={() => handleDocumentSetSelect(documentSetName)}
              >
                <>
                  <div>
                    <FiBookmark />
                  </div>
                  <span className="ml-2">{documentSetName}</span>
                </>
              </SelectedBubble>
            ))}
        </div>
      </div>
    </div>
  );
}
