"use client";

import { useState } from "react";
import * as Yup from "yup";
import { useFormikContext } from "formik";
import { FiPlus, FiX } from "react-icons/fi";
import { EditIcon, HighspotIcon, TrashIcon } from "@/components/icons/icons";
import { errorHandlingFetcher as fetcher } from "@/lib/fetcher";
import useSWR, { useSWRConfig } from "swr";
import { LoadingAnimation } from "@/components/Loading";
import { HealthCheckBanner } from "@/components/health/healthcheck";
import { Button, Card, Divider, Text, Title } from "@tremor/react";
import {
  HighspotConfig,
  HighspotCredentialJson,
  ConnectorIndexingStatus,
  Credential,
} from "@/lib/types";
import { adminDeleteCredential, linkCredential } from "@/lib/credential";
import { CredentialForm } from "@/components/admin/connectors/CredentialForm";
import { TextFormField } from "@/components/admin/connectors/Field";
import { ConnectorsTable } from "@/components/admin/connectors/table/ConnectorsTable";
import { ConnectorForm } from "@/components/admin/connectors/ConnectorForm";
import { usePublicCredentials } from "@/lib/hooks";
import { AdminPageTitle } from "@/components/admin/Title";
import { SearchMultiSelectDropdown } from "@/components/Dropdown";

interface HighspotSpotResponse {
  id: string;
  name: string;
}

/** Multi-select for Highspot Spot names. Reads/writes
 *  `spot_names: string[]` on the surrounding Formik form via
 *  `useFormikContext`. Fetches the live list of Spots from the
 *  Highspot API (server-side route), and renders selected chips +
 *  a searchable dropdown of unselected ones. Mandatory: form-level
 *  yup validation enforces `min(1)`. */
const HighspotSpotsMultiSelect = ({
  credentialId,
}: {
  credentialId: number;
}) => {
  const { values, setFieldValue, errors, touched } =
    useFormikContext<HighspotConfig>();
  const selected = values.spot_names ?? [];

  const {
    data: spots,
    isLoading,
    error,
  } = useSWR<HighspotSpotResponse[]>(
    `/api/manage/admin/connector/highspot/spots/${credentialId}`,
    fetcher
  );

  const showError = touched.spot_names && errors.spot_names;

  return (
    <div className="mb-3">
      <label className="text-sm font-medium text-emphasis">Spots:</label>
      <p className="text-xs text-subtle mb-2">
        Select one or more Spots to index. The list comes live from Highspot
        using the credential above. Re-open the page if you add new Spots in
        Highspot and want them to appear here.
      </p>

      {/* Selected chips */}
      {selected.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {selected.map((name) => (
            <div
              key={name}
              className="flex items-center rounded-lg px-3 py-1 border border-border bg-background-strong hover:bg-hover cursor-pointer"
              onClick={() =>
                setFieldValue(
                  "spot_names",
                  selected.filter((n) => n !== name)
                )
              }
            >
              <span className="text-sm">{name}</span>
              <FiX className="ml-2 my-auto" />
            </div>
          ))}
        </div>
      )}

      {/* Loading / error / dropdown */}
      {isLoading ? (
        <Text className="text-xs italic">Loading Spots from Highspot…</Text>
      ) : error ? (
        <Text className="text-xs text-red-500">
          Failed to load Spots from Highspot:{" "}
          {error?.info?.detail || error?.message || "unknown error"}. Verify the
          credentials in Step 1.
        </Text>
      ) : (
        (() => {
          const available = (spots ?? [])
            .filter((s) => !selected.includes(s.name))
            .map((s) => ({
              name: s.name,
              value: s.name,
              metadata: { spotId: s.id },
            }));
          if (
            available.length === 0 &&
            selected.length === (spots?.length ?? 0)
          ) {
            return (
              <Text className="text-xs italic">
                All available Spots are selected.
              </Text>
            );
          }
          return (
            <SearchMultiSelectDropdown
              options={available}
              onSelect={(option) => {
                const name = String(option.value);
                if (!selected.includes(name)) {
                  setFieldValue("spot_names", [...selected, name]);
                }
              }}
              itemComponent={({ option }) => (
                <div className="flex px-4 py-2.5 hover:bg-hover cursor-pointer">
                  <div className="my-auto">{option.name}</div>
                  <div className="ml-auto my-auto">
                    <FiPlus />
                  </div>
                </div>
              )}
            />
          );
        })()
      )}

      {showError && (
        <Text className="text-xs text-red-500 mt-1">
          {String(errors.spot_names)}
        </Text>
      )}
    </div>
  );
};

const MainSection = () => {
  const { mutate } = useSWRConfig();
  const [isEditingCredential, setIsEditingCredential] = useState(false);

  const {
    data: connectorIndexingStatuses,
    isLoading: isConnectorIndexingStatusesLoading,
    error: isConnectorIndexingStatusesError,
  } = useSWR<ConnectorIndexingStatus<any, any>[]>(
    "/api/manage/admin/connector/indexing-status",
    fetcher
  );

  const {
    data: credentialsData,
    isLoading: isCredentialsLoading,
    error: isCredentialsError,
    refreshCredentials,
  } = usePublicCredentials();

  if (
    (!connectorIndexingStatuses && isConnectorIndexingStatusesLoading) ||
    (!credentialsData && isCredentialsLoading)
  ) {
    return <LoadingAnimation text="Loading" />;
  }

  if (isConnectorIndexingStatusesError || !connectorIndexingStatuses) {
    return <div>Failed to load connectors</div>;
  }

  if (isCredentialsError || !credentialsData) {
    return <div>Failed to load credentials</div>;
  }

  const highspotConnectorIndexingStatuses: ConnectorIndexingStatus<
    HighspotConfig,
    HighspotCredentialJson
  >[] = connectorIndexingStatuses.filter(
    (connectorIndexingStatus) =>
      connectorIndexingStatus.connector.source === "highspot"
  );

  // Discriminator field is highspot_key — there's only one credential
  // shape for Highspot in this fork.
  const highspotCredential: Credential<HighspotCredentialJson> | undefined =
    credentialsData.find(
      (credential) => credential.credential_json?.highspot_key
    );

  return (
    <>
      <Text>
        The Highspot connector indexes Spots and the Items inside them via
        Highspot&apos;s REST API. WebLink items are scraped via headless
        Chromium; downloadable file items (PDF / DOCX / PPTX / XLSX / EML / EPUB
        / HTML / TXT) are extracted to text.
      </Text>

      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 1: Provide Highspot credentials
      </Title>
      {highspotCredential ? (
        <>
          <div className="flex mb-1 text-sm items-center">
            <Text className="my-auto">Existing Highspot Key: </Text>
            <Text className="ml-1 italic my-auto">
              {highspotCredential.credential_json.highspot_key}
            </Text>
            <button
              className="ml-2 hover:bg-hover rounded p-1"
              title="Edit credential"
              onClick={() => setIsEditingCredential((v) => !v)}
            >
              <EditIcon size={16} />
            </button>
            <Button
              size="xs"
              color="red"
              className="ml-3 text-inverted"
              onClick={async () => {
                await adminDeleteCredential(highspotCredential.id);
                setIsEditingCredential(false);
                refreshCredentials();
              }}
            >
              <TrashIcon />
            </Button>
          </div>
          {isEditingCredential && (
            <Card className="mt-2">
              <Text className="mb-2">
                Update the Highspot key/secret. The change is saved against the
                existing credential, so all linked Highspot connectors pick it
                up on their next poll.
              </Text>
              <CredentialForm<HighspotCredentialJson>
                existingCredentialId={highspotCredential.id}
                formBody={
                  <>
                    <TextFormField name="highspot_key" label="Highspot Key:" />
                    <TextFormField
                      name="highspot_secret"
                      label="Highspot Secret:"
                      type="password"
                    />
                    <TextFormField
                      name="highspot_url"
                      label="Highspot Base URL (optional):"
                      subtext="Leave blank to use the default https://api-su2.highspot.com/v1.0/."
                    />
                  </>
                }
                validationSchema={Yup.object().shape({
                  highspot_key: Yup.string().required(
                    "Please enter your Highspot key"
                  ),
                  highspot_secret: Yup.string().required(
                    "Please enter your Highspot secret"
                  ),
                  highspot_url: Yup.string().optional(),
                })}
                initialValues={{
                  highspot_key:
                    highspotCredential.credential_json.highspot_key || "",
                  highspot_secret:
                    highspotCredential.credential_json.highspot_secret || "",
                  highspot_url:
                    highspotCredential.credential_json.highspot_url || "",
                }}
                onSubmit={(isSuccess) => {
                  if (isSuccess) {
                    setIsEditingCredential(false);
                    refreshCredentials();
                  }
                }}
                extraActions={
                  <Button
                    type="button"
                    size="xs"
                    color="gray"
                    onClick={() => setIsEditingCredential(false)}
                  >
                    Cancel
                  </Button>
                }
              />
            </Card>
          )}
        </>
      ) : (
        <>
          <Text className="mb-2">
            Provide a Highspot API key + secret pair generated from the Highspot
            admin console (Settings → API Access). The optional base URL is for
            tenants on a non-default Highspot region.
          </Text>
          <Card>
            <CredentialForm<HighspotCredentialJson>
              formBody={
                <>
                  <TextFormField name="highspot_key" label="Highspot Key:" />
                  <TextFormField
                    name="highspot_secret"
                    label="Highspot Secret:"
                    type="password"
                  />
                  <TextFormField
                    name="highspot_url"
                    label="Highspot Base URL (optional):"
                    subtext="Leave blank to use the default https://api-su2.highspot.com/v1.0/."
                  />
                </>
              }
              validationSchema={Yup.object().shape({
                highspot_key: Yup.string().required(
                  "Please enter your Highspot key"
                ),
                highspot_secret: Yup.string().required(
                  "Please enter your Highspot secret"
                ),
                highspot_url: Yup.string().optional(),
              })}
              initialValues={{
                highspot_key: "",
                highspot_secret: "",
                highspot_url: "",
              }}
              onSubmit={(isSuccess) => {
                if (isSuccess) {
                  refreshCredentials();
                }
              }}
            />
          </Card>
        </>
      )}

      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 2: Which Spots do you want to index?
      </Title>

      {highspotConnectorIndexingStatuses.length > 0 && (
        <>
          <Text className="mb-2">
            We pull the latest items from each Spot listed below every{" "}
            <b>10 minutes</b>.
          </Text>
          <div className="mb-2">
            <ConnectorsTable<HighspotConfig, HighspotCredentialJson>
              connectorIndexingStatuses={highspotConnectorIndexingStatuses}
              liveCredential={highspotCredential}
              getCredential={(credential) =>
                credential.credential_json.highspot_key
              }
              specialColumns={[
                {
                  header: "Spots",
                  key: "spots",
                  getValue: (ccPairStatus) => {
                    const cfg =
                      ccPairStatus.connector.connector_specific_config;
                    return cfg.spot_names && cfg.spot_names.length > 0
                      ? cfg.spot_names.join(", ")
                      : "(all)";
                  },
                },
              ]}
              onUpdate={() =>
                mutate("/api/manage/admin/connector/indexing-status")
              }
              onCredentialLink={async (connectorId) => {
                if (highspotCredential) {
                  await linkCredential(connectorId, highspotCredential.id);
                  mutate("/api/manage/admin/connector/indexing-status");
                }
              }}
            />
          </div>
          <Divider />
        </>
      )}

      {highspotCredential ? (
        <Card>
          <h2 className="font-bold mb-3">Connect to a New Highspot Tenant</h2>
          <ConnectorForm<HighspotConfig>
            nameBuilder={(values) =>
              `HighspotConnector-${(values.spot_names ?? []).join("_")}`
            }
            source="highspot"
            inputType="poll"
            formBodyBuilder={() => (
              <HighspotSpotsMultiSelect credentialId={highspotCredential.id} />
            )}
            validationSchema={Yup.object().shape({
              // Mandatory: pick at least one Spot. Empty/no-spot
              // configs would index every Spot the credential can
              // see, which is rarely what an admin actually wants
              // and is a big enough blast radius (Spot count can be
              // hundreds) that we don't allow it from the UI.
              spot_names: Yup.array()
                .of(Yup.string().required("Spot name cannot be empty"))
                .min(1, "Please select at least one Spot")
                .required("Please select at least one Spot"),
            })}
            initialValues={{
              spot_names: [],
            }}
            credentialId={highspotCredential.id}
            refreshFreq={10 * 60} // 10 minutes default
          />
        </Card>
      ) : (
        <Text>
          Please provide your Highspot key + secret in Step 1 first! Once done
          with that, you can specify which Spots you want to index.
        </Text>
      )}
    </>
  );
};

export default function Page() {
  return (
    <div className="mx-auto container">
      <div className="mb-4">
        <HealthCheckBanner />
      </div>

      <AdminPageTitle icon={<HighspotIcon size={32} />} title="Highspot" />

      <MainSection />
    </div>
  );
}
