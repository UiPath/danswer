import { useChatContext } from "@/components/context/ChatContext";
import { FilterManager } from "@/lib/hooks";
import { listSourceMetadata } from "@/lib/sources";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  DateRangePicker,
  DateRangePickerItem,
  Divider,
  Text,
} from "@tremor/react";
import { getXDaysAgo } from "@/lib/dateUtils";
import { DocumentSetSelectable } from "@/components/documentSet/DocumentSetSelectable";
import { Bubble } from "@/components/Bubble";

export function FiltersTab({
  filterManager,
}: {
  filterManager: FilterManager;
}): JSX.Element {
  const { availableSources, availableDocumentSets } = useChatContext();
  const [docSetFilter, setDocSetFilter] = useState("");
  const docSetInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (docSetInputRef.current) {
      docSetInputRef.current.focus();
    }
  }, []);

  const filteredDocumentSets = useMemo(() => {
    const q = docSetFilter.trim().toLowerCase();
    if (!q) return availableDocumentSets;
    return availableDocumentSets.filter((set) =>
      set.name.toLowerCase().includes(q)
    );
  }, [availableDocumentSets, docSetFilter]);

  const allSources = listSourceMetadata();
  const availableSourceMetadata = allSources.filter((source) =>
    availableSources.includes(source.internalName)
  );

  return (
    <div className="overflow-hidden flex flex-col">
      <div className="overflow-y-auto">
        <div>
          <div className="pb-4">
            <h3 className="text-lg font-semibold">Time Range</h3>
            <Text>
              Choose the time range we should search over. If only one date is
              selected, will only search after the specified date.
            </Text>
            <div className="mt-2">
              <DateRangePicker
                className="w-96"
                value={{
                  from: filterManager.timeRange?.from,
                  to: filterManager.timeRange?.to,
                  selectValue: filterManager.timeRange?.selectValue,
                }}
                onValueChange={(value) =>
                  filterManager.setTimeRange({
                    from: value.from,
                    to: value.to,
                    selectValue: value.selectValue,
                  })
                }
                selectPlaceholder="Select range"
                enableSelect
              >
                <DateRangePickerItem
                  key="Last 30 Days"
                  value="Last 30 Days"
                  from={getXDaysAgo(30)}
                  to={new Date()}
                >
                  Last 30 Days
                </DateRangePickerItem>
                <DateRangePickerItem
                  key="Last 7 Days"
                  value="Last 7 Days"
                  from={getXDaysAgo(7)}
                  to={new Date()}
                >
                  Last 7 Days
                </DateRangePickerItem>
                <DateRangePickerItem
                  key="Today"
                  value="Today"
                  from={getXDaysAgo(1)}
                  to={new Date()}
                >
                  Today
                </DateRangePickerItem>
              </DateRangePicker>
            </div>
          </div>

          <Divider />

          <div className="mb-8">
            <h3 className="text-lg font-semibold">Knowledge Sets</h3>
            <Text>
              Choose which knowledge sets we should search over. If multiple are
              selected, we will search through all of them.
            </Text>

            {availableDocumentSets.length > 0 ? (
              <>
                <div className="mt-3">
                  <input
                    ref={docSetInputRef}
                    className="w-96 border border-border py-1 px-2 rounded text-sm h-9"
                    placeholder={`Find a knowledge set (${availableDocumentSets.length} available, ${filterManager.selectedDocumentSets.length} selected)`}
                    value={docSetFilter}
                    onChange={(e) => setDocSetFilter(e.target.value)}
                  />
                  {filterManager.selectedDocumentSets.length > 0 && (
                    <button
                      type="button"
                      className="ml-3 text-xs text-link hover:underline"
                      onClick={() =>
                        filterManager.setSelectedDocumentSets([])
                      }
                    >
                      Clear selection
                    </button>
                  )}
                </div>

                <ul className="mt-3 max-h-72 overflow-y-auto pr-1 flex flex-col gap-y-1">
                  {filteredDocumentSets.length > 0 ? (
                    filteredDocumentSets.map((set) => {
                      const isSelected =
                        filterManager.selectedDocumentSets.includes(set.name);
                      return (
                        <DocumentSetSelectable
                          key={set.id}
                          documentSet={set}
                          isSelected={isSelected}
                          onSelect={() =>
                            filterManager.setSelectedDocumentSets((prev) =>
                              isSelected
                                ? prev.filter((s) => s !== set.name)
                                : [...prev, set.name]
                            )
                          }
                        />
                      );
                    })
                  ) : (
                    <li className="text-sm text-subtle italic px-2 py-2">
                      No matching knowledge sets
                    </li>
                  )}
                </ul>
              </>
            ) : (
              <ul className="mt-3">
                <li>No knowledge sets available</li>
              </ul>
            )}
          </div>

          <Divider />

          <div className="mb-4">
            <h3 className="text-lg font-semibold">Sources</h3>
            <Text>
              Choose which sources we should search over. If multiple sources
              are selected, we will search through all of them.
            </Text>
            <ul className="mt-3 flex gap-2">
              {availableSourceMetadata.length > 0 ? (
                availableSourceMetadata.map((sourceMetadata) => {
                  const isSelected = filterManager.selectedSources.some(
                    (selectedSource) =>
                      selectedSource.internalName ===
                      sourceMetadata.internalName
                  );
                  return (
                    <Bubble
                      key={sourceMetadata.internalName}
                      isSelected={isSelected}
                      onClick={() =>
                        filterManager.setSelectedSources((prev) =>
                          isSelected
                            ? prev.filter(
                                (s) =>
                                  s.internalName !== sourceMetadata.internalName
                              )
                            : [...prev, sourceMetadata]
                        )
                      }
                      showCheckbox={true}
                    >
                      <div className="flex items-center space-x-2">
                        {sourceMetadata?.icon({ size: 16 })}
                        <span>{sourceMetadata.displayName}</span>
                      </div>
                    </Bubble>
                  );
                })
              ) : (
                <li>No sources available</li>
              )}
            </ul>
          </div>

        </div>
      </div>
    </div>
  );
}
