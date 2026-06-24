"use client";

import { useState } from "react";
import { ArrayHelpers, FieldArray, Form, Formik } from "formik";
import * as Yup from "yup";
import { PopupSpec } from "@/components/admin/connectors/Popup";
import { createDocumentSet, updateDocumentSet } from "./lib";
import {
  Connector,
  ConnectorIndexingStatus,
  DocumentSet,
  UserGroup,
} from "@/lib/types";
import {
  BooleanFormField,
  Label,
  SubLabel,
  TextFormField,
} from "@/components/admin/connectors/Field";
import { ConnectorTitle } from "@/components/admin/connectors/ConnectorTitle";
import {
  SearchMultiSelectDropdown,
  DefaultDropdown,
} from "@/components/Dropdown";
import { getSourceMetadata } from "@/lib/sources";
import { Button, Divider, Text } from "@tremor/react";
import { FiPlus, FiUsers, FiX } from "react-icons/fi";
import { usePaidEnterpriseFeaturesEnabled } from "@/components/settings/usePaidEnterpriseFeaturesEnabled";

interface SetCreationPopupProps {
  ccPairs: ConnectorIndexingStatus<any, any>[];
  userGroups: UserGroup[] | undefined;
  onClose: () => void;
  setPopup: (popupSpec: PopupSpec | null) => void;
  existingDocumentSet?: DocumentSet;
}

// Summarize the connector_specific_config so two cc-pairs with the
// same display name (e.g. multiple Confluence entries pointing to
// different wiki URLs) can be told apart in the picker.
function summarizeConnectorConfig(
  config: Record<string, any> | null | undefined
): string {
  if (!config) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(config)) {
    if (value === undefined || value === null || value === "") continue;
    if (typeof value === "boolean") continue;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      parts.push(`${key}: ${value.join(", ")}`);
    } else if (typeof value === "object") {
      continue;
    } else {
      parts.push(`${key}: ${value}`);
    }
  }
  return parts.join(" • ");
}

export const DocumentSetCreationForm = ({
  ccPairs,
  userGroups,
  onClose,
  setPopup,
  existingDocumentSet,
}: SetCreationPopupProps) => {
  const isPaidEnterpriseFeaturesEnabled = usePaidEnterpriseFeaturesEnabled();

  const isUpdate = existingDocumentSet !== undefined;

  // Optional connector-type filter for the picker below. Defaults to "all" so
  // every connector shows; narrows the picker to a single source when chosen.
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const sourceCounts = ccPairs.reduce<Record<string, number>>((acc, ccPair) => {
    const source = ccPair.connector.source as string;
    acc[source] = (acc[source] ?? 0) + 1;
    return acc;
  }, {});
  const sourceOptions = [
    { name: `All connectors (${ccPairs.length})`, value: "all" },
    ...Array.from(new Set(ccPairs.map((ccPair) => ccPair.connector.source)))
      .map((source) => ({
        name: `${getSourceMetadata(source).displayName} (${
          sourceCounts[source as string]
        })`,
        value: source as string,
      }))
      .sort((a, b) => a.name.localeCompare(b.name)),
  ];

  return (
    <div>
      <Formik
        initialValues={{
          name: existingDocumentSet ? existingDocumentSet.name : "",
          description: existingDocumentSet
            ? existingDocumentSet.description
            : "",
          cc_pair_ids: existingDocumentSet
            ? existingDocumentSet.cc_pair_descriptors.map(
                (ccPairDescriptor) => {
                  return ccPairDescriptor.id;
                }
              )
            : ([] as number[]),
          is_public: existingDocumentSet ? existingDocumentSet.is_public : true,
          users: existingDocumentSet ? existingDocumentSet.users : [],
          groups: existingDocumentSet ? existingDocumentSet.groups : [],
        }}
        validationSchema={Yup.object().shape({
          name: Yup.string().required("Please enter a name for the set"),
          description: Yup.string().required(
            "Please enter a description for the set"
          ),
          cc_pair_ids: Yup.array()
            .of(Yup.number().required())
            .required("Please select at least one connector"),
        })}
        onSubmit={async (values, formikHelpers) => {
          formikHelpers.setSubmitting(true);
          // If the document set is public, then we don't want to send any groups
          const processedValues = {
            ...values,
            groups: values.is_public ? [] : values.groups,
          };

          let response;
          if (isUpdate) {
            response = await updateDocumentSet({
              id: existingDocumentSet.id,
              ...processedValues,
            });
          } else {
            response = await createDocumentSet(processedValues);
          }
          formikHelpers.setSubmitting(false);
          if (response.ok) {
            setPopup({
              message: isUpdate
                ? "Successfully updated document set!"
                : "Successfully created document set!",
              type: "success",
            });
            onClose();
          } else {
            const errorMsg = await response.text();
            setPopup({
              message: isUpdate
                ? `Error updating document set - ${errorMsg}`
                : `Error creating document set - ${errorMsg}`,
              type: "error",
            });
          }
        }}
      >
        {({ isSubmitting, values, setFieldValue }) => (
          <Form>
            <TextFormField
              name="name"
              label="Name:"
              placeholder="A name for the document set"
              disabled={isUpdate}
              autoCompleteDisabled={true}
            />
            <TextFormField
              name="description"
              label="Description:"
              placeholder="Describe what the document set represents"
              autoCompleteDisabled={true}
            />

            <Divider />

            <Label>Pick your connectors</Label>
            <SubLabel>
              All documents indexed by the selected connectors will be a part of
              this document set. Filter by connector type, then search by name
              and click to add; click a selected connector to remove it.
            </SubLabel>

            <FieldArray
              name="cc_pair_ids"
              render={(arrayHelpers: ArrayHelpers) => {
                const selectedCCPairs = ccPairs.filter((ccPair) =>
                  values.cc_pair_ids.includes(ccPair.cc_pair_id)
                );
                const availableOptions = ccPairs
                  .filter(
                    (ccPair) =>
                      !values.cc_pair_ids.includes(ccPair.cc_pair_id) &&
                      (sourceFilter === "all" ||
                        ccPair.connector.source === sourceFilter)
                  )
                  .map((ccPair) => {
                    const configSummary = summarizeConnectorConfig(
                      ccPair.connector.connector_specific_config
                    );
                    return {
                      name: ccPair.name?.toString() || "",
                      value: ccPair.cc_pair_id?.toString() ?? "",
                      // Make the connector's source + config (which for web
                      // connectors holds the URL) searchable, not just the
                      // display name — so typing a URL fragment finds it.
                      searchableText: [
                        ccPair.name,
                        ccPair.connector?.source,
                        configSummary,
                      ]
                        .filter(Boolean)
                        .join(" "),
                      metadata: {
                        ccPairId: ccPair.cc_pair_id,
                        connector: ccPair.connector,
                        configSummary,
                      },
                    };
                  });
                return (
                  <div className="mb-3">
                    {selectedCCPairs.length > 0 && (
                      <div className="mb-3 flex flex-wrap gap-2">
                        {selectedCCPairs.map((ccPair) => {
                          const ind = values.cc_pair_ids.indexOf(
                            ccPair.cc_pair_id
                          );
                          const configSummary = summarizeConnectorConfig(
                            ccPair.connector.connector_specific_config
                          );
                          return (
                            <div
                              key={`${ccPair.connector.id}-${ccPair.credential.id}`}
                              className="flex rounded-lg px-3 py-1 border border-border bg-background-strong hover:bg-hover cursor-pointer"
                              onClick={() => arrayHelpers.remove(ind)}
                              title={configSummary || undefined}
                            >
                              <div className="my-auto">
                                <ConnectorTitle
                                  connector={ccPair.connector}
                                  ccPairId={ccPair.cc_pair_id}
                                  ccPairName={ccPair.name}
                                  isLink={false}
                                  showMetadata={false}
                                />
                              </div>
                              <FiX className="ml-2 my-auto" />
                            </div>
                          );
                        })}
                      </div>
                    )}
                    <div className="w-52 mb-3">
                      <DefaultDropdown
                        options={sourceOptions}
                        selected={sourceFilter}
                        onSelect={(value) =>
                          setSourceFilter((value as string) ?? "all")
                        }
                      />
                    </div>
                    <SearchMultiSelectDropdown
                      options={availableOptions}
                      onSelect={(option) => {
                        const ccPairId = parseInt(option.value as string);
                        if (
                          !Number.isNaN(ccPairId) &&
                          !values.cc_pair_ids.includes(ccPairId)
                        ) {
                          arrayHelpers.push(ccPairId);
                        }
                      }}
                      itemComponent={({ option }) => {
                        const configSummary =
                          (option?.metadata?.configSummary as string) || "";
                        return (
                          <div
                            className="flex px-4 py-2.5 hover:bg-hover cursor-pointer"
                            title={configSummary || undefined}
                          >
                            <div className="my-auto min-w-0">
                              <ConnectorTitle
                                ccPairId={option?.metadata?.ccPairId as number}
                                ccPairName={option.name}
                                connector={
                                  option?.metadata?.connector as Connector<any>
                                }
                                isLink={false}
                                showMetadata={false}
                              />
                              {configSummary && (
                                <div className="text-xs text-subtle mt-0.5 truncate">
                                  {configSummary}
                                </div>
                              )}
                            </div>
                            <div className="ml-auto my-auto pl-2">
                              <FiPlus />
                            </div>
                          </div>
                        );
                      }}
                    />
                    <div className="mt-2 flex gap-4 text-sm">
                      <button
                        type="button"
                        className="text-link hover:text-link-hover disabled:opacity-50 disabled:cursor-default"
                        disabled={availableOptions.length === 0}
                        onClick={() =>
                          setFieldValue("cc_pair_ids", [
                            ...values.cc_pair_ids,
                            ...availableOptions.map((option) =>
                              parseInt(option.value as string)
                            ),
                          ])
                        }
                      >
                        {sourceFilter === "all"
                          ? "Select all"
                          : "Select all of this type"}
                        {availableOptions.length > 0
                          ? ` (${availableOptions.length})`
                          : ""}
                      </button>
                      {values.cc_pair_ids.length > 0 && (
                        <button
                          type="button"
                          className="text-link hover:text-link-hover"
                          onClick={() => setFieldValue("cc_pair_ids", [])}
                        >
                          Clear selection
                        </button>
                      )}
                    </div>
                  </div>
                );
              }}
            />

            {isPaidEnterpriseFeaturesEnabled &&
              userGroups &&
              userGroups.length > 0 && (
                <div>
                  <Divider />

                  <BooleanFormField
                    name="is_public"
                    label="Is Public?"
                    subtext={
                      <>
                        If the document set is public, then it will be visible
                        to <b>all users</b>. If it is not public, then only
                        users in the specified groups will be able to see it.
                      </>
                    }
                  />

                  <Divider />
                  <h2 className="mb-1 font-medium text-base">
                    Groups with Access
                  </h2>
                  {!values.is_public ? (
                    <>
                      <Text className="mb-3">
                        If any groups are specified, then this Document Set will
                        only be visible to the specified groups. If no groups
                        are specified, then the Document Set will be visible to
                        all users.
                      </Text>
                      <FieldArray
                        name="groups"
                        render={(arrayHelpers: ArrayHelpers) => (
                          <div className="flex gap-2 flex-wrap">
                            {userGroups.map((userGroup) => {
                              const ind = values.groups.indexOf(userGroup.id);
                              let isSelected = ind !== -1;
                              return (
                                <div
                                  key={userGroup.id}
                                  className={
                                    `
                              px-3 
                              py-1
                              rounded-lg 
                              border
                              border-border 
                              w-fit 
                              flex 
                              cursor-pointer ` +
                                    (isSelected
                                      ? " bg-background-strong"
                                      : " hover:bg-hover")
                                  }
                                  onClick={() => {
                                    if (isSelected) {
                                      arrayHelpers.remove(ind);
                                    } else {
                                      arrayHelpers.push(userGroup.id);
                                    }
                                  }}
                                >
                                  <div className="my-auto flex">
                                    <FiUsers className="my-auto mr-2" />{" "}
                                    {userGroup.name}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      />
                    </>
                  ) : (
                    <Text>
                      This Document Set is public, so this does not apply. If
                      you want to control which user groups see this Document
                      Set, mark it as non-public!
                    </Text>
                  )}
                </div>
              )}
            <div className="flex mt-6">
              <Button
                type="submit"
                disabled={isSubmitting}
                className="w-64 mx-auto"
              >
                {isUpdate ? "Update!" : "Create!"}
              </Button>
            </div>
          </Form>
        )}
      </Formik>
    </div>
  );
};
