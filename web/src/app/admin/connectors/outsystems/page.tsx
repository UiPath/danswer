"use client";

import { useState } from "react";
import * as Yup from "yup";
import { EditIcon, GlobeIcon, TrashIcon } from "@/components/icons/icons";
import { errorHandlingFetcher as fetcher } from "@/lib/fetcher";
import useSWR, { useSWRConfig } from "swr";
import { LoadingAnimation } from "@/components/Loading";
import { HealthCheckBanner } from "@/components/health/healthcheck";
import { Button, Card, Divider, Text, Title } from "@tremor/react";
import {
  OutSystemsConfig,
  OutSystemsCredentialJson,
  ConnectorIndexingStatus,
  Credential,
} from "@/lib/types";
import { adminDeleteCredential, linkCredential } from "@/lib/credential";
import { CredentialForm } from "@/components/admin/connectors/CredentialForm";
import { TextFormField } from "@/components/admin/connectors/Field";
import { ConnectorsTable } from "@/components/admin/connectors/table/ConnectorsTable";
import {
  ConnectorForm,
  UpdateConnectorForm,
} from "@/components/admin/connectors/ConnectorForm";
import { usePublicCredentials } from "@/lib/hooks";
import { AdminPageTitle } from "@/components/admin/Title";

// Interim credential form fields — the short-lived browser session captured
// from DevTools (see the outsystems connector). Swaps to a service account
// later. Reused for both the create and edit cases below.
const credentialFormBody = (
  <>
    <TextFormField
      name="outsystems_cookie"
      label="Session Cookie (the `cookie:` request header):"
      type="password"
    />
    <TextFormField
      name="outsystems_csrf"
      label="CSRF token (the `x-csrftoken` request header):"
      type="password"
    />
    <TextFormField
      name="outsystems_api_version"
      label="Page API version (versionInfo.apiVersion from a DataActionGetPage request):"
    />
    <TextFormField
      name="outsystems_file_api_version"
      label="File API version (optional — apiVersion from an ActionFileMetadata_Get request):"
      subtext="Enables downloading attached PDFs/docs. Leave blank to index page text only."
    />
    <TextFormField
      name="outsystems_base_url"
      label="Base URL (optional):"
      subtext="Leave blank to use https://inside.uipath.com."
    />
  </>
);

const credentialValidation = Yup.object().shape({
  outsystems_cookie: Yup.string().required("Please paste the session cookie"),
  outsystems_csrf: Yup.string().required("Please paste the x-csrftoken value"),
  outsystems_api_version: Yup.string().required("Please paste the apiVersion"),
  outsystems_file_api_version: Yup.string().optional(),
  outsystems_base_url: Yup.string().optional(),
});

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

  const outsystemsConnectorIndexingStatuses: ConnectorIndexingStatus<
    OutSystemsConfig,
    OutSystemsCredentialJson
  >[] = connectorIndexingStatuses.filter(
    (status) => status.connector.source === "outsystems"
  );

  const outsystemsCredential: Credential<OutSystemsCredentialJson> | undefined =
    credentialsData.find((credential) => credential.credential_json?.outsystems_csrf);

  return (
    <>
      <Text>
        The OutSystems connector indexes pages from an OutSystems app
        (currently the UiPath Intranet, inside.uipath.com). It is an <b>interim, one-time</b> connector: it
        authenticates with a short-lived browser session, so create it with no
        refresh schedule and run a single index while the session is fresh. It
        will be replaced by a service-account version later.
      </Text>

      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 1: Provide an OutSystems session
      </Title>
      <Text className="mb-2">
        From a browser logged in to inside.uipath.com, open DevTools → Network,
        click any page, find a POST to <code>DataActionGetPage</code>, and copy
        its <code>cookie</code> and <code>x-csrftoken</code> request headers and
        the request body&apos;s <code>versionInfo.apiVersion</code>.
      </Text>

      {outsystemsCredential ? (
        <>
          <div className="flex mb-1 text-sm items-center">
            <Text className="my-auto">Existing session credential</Text>
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
                await adminDeleteCredential(outsystemsCredential.id);
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
                Re-paste a fresh session (cookie / csrf / apiVersion). Needed
                whenever the previous session has expired.
              </Text>
              <CredentialForm<OutSystemsCredentialJson>
                existingCredentialId={outsystemsCredential.id}
                formBody={credentialFormBody}
                validationSchema={credentialValidation}
                initialValues={{
                  outsystems_cookie: "",
                  outsystems_csrf: "",
                  outsystems_api_version: "",
                  outsystems_file_api_version: "",
                  outsystems_base_url:
                    outsystemsCredential.credential_json.outsystems_base_url || "",
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
        <Card>
          <CredentialForm<OutSystemsCredentialJson>
            formBody={credentialFormBody}
            validationSchema={credentialValidation}
            initialValues={{
              outsystems_cookie: "",
              outsystems_csrf: "",
              outsystems_api_version: "",
              outsystems_file_api_version: "",
              outsystems_base_url: "",
            }}
            onSubmit={(isSuccess) => {
              if (isSuccess) {
                refreshCredentials();
              }
            }}
          />
        </Card>
      )}

      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 2: Index OutSystems pages
      </Title>

      {outsystemsConnectorIndexingStatuses.length > 0 && (
        <>
          <Text className="mb-2">
            The connector below was created. Trigger an index run from its row
            (it has no refresh schedule, so it only runs when you ask it to).
          </Text>
          <div className="mb-2">
            <ConnectorsTable<OutSystemsConfig, OutSystemsCredentialJson>
              connectorIndexingStatuses={outsystemsConnectorIndexingStatuses}
              liveCredential={outsystemsCredential}
              getCredential={(credential) =>
                credential.credential_json.outsystems_csrf ? "session set" : ""
              }
              specialColumns={[
                {
                  header: "Page range",
                  key: "page_range",
                  getValue: (ccPairStatus) => {
                    const cfg =
                      ccPairStatus.connector.connector_specific_config;
                    return `${cfg.page_id_start ?? 1} – ${cfg.page_id_end ?? 600}`;
                  },
                },
              ]}
              onUpdate={() =>
                mutate("/api/manage/admin/connector/indexing-status")
              }
              onCredentialLink={async (connectorId) => {
                if (outsystemsCredential) {
                  await linkCredential(connectorId, outsystemsCredential.id);
                  mutate("/api/manage/admin/connector/indexing-status");
                }
              }}
            />
          </div>

          {/* Edit the page range to RESUME after a session expiry, or to run in
              cookie-sized chunks. page_id_start is the cursor: set it to the
              PageId from the failure message/logs and use the cc-pair page's
              "Run Update" (NOT "Run Complete Re-Indexing") so already-indexed
              pages aren't re-embedded. Lowering page_id_start to the resume
              point means earlier pages aren't re-fetched at all. */}
          {outsystemsConnectorIndexingStatuses.map((status) => (
            <Card key={status.connector.id} className="mb-3">
              <h2 className="font-bold mb-1">Edit page range (resume / chunk)</h2>
              <Text className="mb-3 text-sm">
                To resume after a session expiry, set <b>First PageId</b> to the
                PageId in the failure message, save, then use{" "}
                <b>&quot;Run Update&quot;</b> on the connector&apos;s page.
              </Text>
              <UpdateConnectorForm<OutSystemsConfig>
                nameBuilder={() => "OutSystemsConnector"}
                existingConnector={status.connector}
                formBody={
                  <>
                    <TextFormField name="page_id_start" label="First PageId:" />
                    <TextFormField name="page_id_end" label="Last PageId:" />
                  </>
                }
                validationSchema={Yup.object().shape({
                  page_id_start: Yup.number()
                    .min(1)
                    .required("Please enter a start PageId"),
                  page_id_end: Yup.number()
                    .min(1)
                    .required("Please enter an end PageId"),
                })}
                onSubmit={(isSuccess) => {
                  if (isSuccess) {
                    mutate("/api/manage/admin/connector/indexing-status");
                  }
                }}
              />
            </Card>
          ))}
          <Divider />
        </>
      )}

      {outsystemsCredential ? (
        <Card>
          <h2 className="font-bold mb-3">Create the OutSystems connector</h2>
          <ConnectorForm<OutSystemsConfig>
            nameBuilder={() => "OutSystemsConnector"}
            source="outsystems"
            inputType="load_state"
            formBody={
              <>
                <TextFormField
                  name="page_id_start"
                  label="First PageId:"
                  subtext="Pages are enumerated by sequential id; default 1."
                />
                <TextFormField
                  name="page_id_end"
                  label="Last PageId:"
                  subtext="Upper bound of the id range to scan; default 600."
                />
              </>
            }
            validationSchema={Yup.object().shape({
              page_id_start: Yup.number()
                .min(1)
                .required("Please enter a start PageId"),
              page_id_end: Yup.number()
                .min(1)
                .required("Please enter an end PageId"),
            })}
            initialValues={{ page_id_start: 1, page_id_end: 600 }}
            credentialId={outsystemsCredential.id}
            // null => one-time, refresh_freq=None (never auto-re-indexed; the
            // session credential would be expired by then anyway).
            refreshFreq={null}
          />
        </Card>
      ) : (
        <Text>
          Please provide a session credential in Step 1 first.
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

      <AdminPageTitle icon={<GlobeIcon size={32} />} title="OutSystems" />

      <MainSection />
    </div>
  );
}
