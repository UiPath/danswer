"use client";

import { useState } from "react";
import * as Yup from "yup";
import { EditIcon, TrashIcon, SalesforceIcon } from "@/components/icons/icons";
import { errorHandlingFetcher as fetcher } from "@/lib/fetcher";
import useSWR, { useSWRConfig } from "swr";
import { LoadingAnimation } from "@/components/Loading";
import { HealthCheckBanner } from "@/components/health/healthcheck";
import { Button } from "@tremor/react";
import {
  SalesforceConfig,
  SalesforceCredentialJson,
  ConnectorIndexingStatus,
  Credential,
} from "@/lib/types"; // Modify or create these types as required
import { adminDeleteCredential, linkCredential } from "@/lib/credential";
import { CredentialForm } from "@/components/admin/connectors/CredentialForm";
import { TextFormField } from "@/components/admin/connectors/Field";
import { ConnectorsTable } from "@/components/admin/connectors/table/ConnectorsTable";
import { ConnectorForm } from "@/components/admin/connectors/ConnectorForm";
import { usePublicCredentials } from "@/lib/hooks";
import { AdminPageTitle } from "@/components/admin/Title";
import { Card, Text, Title } from "@tremor/react";

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

  const SalesforceConnectorIndexingStatuses: ConnectorIndexingStatus<
    SalesforceConfig,
    SalesforceCredentialJson
  >[] = connectorIndexingStatuses.filter(
    (connectorIndexingStatus) =>
      connectorIndexingStatus.connector.source === "salesforce"
  );

  // Match credentials tagged for the Account connector. Untagged credentials
  // (created before sf_credential_kind existed) are accepted as legacy
  // "account" credentials so existing setups keep working.
  const SalesforceCredential: Credential<SalesforceCredentialJson> | undefined =
    credentialsData.find(
      (credential) =>
        credential.credential_json?.sf_username &&
        (credential.credential_json?.sf_credential_kind === "account" ||
          credential.credential_json?.sf_credential_kind === undefined)
    );

  return (
    <>
      <Text>
        The Salesforce connector indexes Account records — including standard
        and custom fields like Name, Owner, RecordType, CSM, TAM, CSD,
        Maintenance Flag, Vertical, and Annual Revenue — making them queryable
        within Darwin.
      </Text>

      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 1: Provide Salesforce credentials
      </Title>
      {SalesforceCredential ? (
        <>
          <div className="flex mb-1 text-sm items-center">
            <Text className="my-auto">Existing SalesForce Username: </Text>
            <Text className="ml-1 italic my-auto">
              {SalesforceCredential.credential_json.sf_username}
            </Text>
            <button
              className="ml-1 hover:bg-hover rounded p-1"
              title="Edit credential"
              onClick={() => setIsEditingCredential((v) => !v)}
            >
              <EditIcon size={16} />
            </button>
            <button
              className="ml-1 hover:bg-hover rounded p-1"
              title="Delete credential"
              onClick={async () => {
                await adminDeleteCredential(SalesforceCredential.id);
                setIsEditingCredential(false);
                refreshCredentials();
              }}
            >
              <TrashIcon />
            </button>
          </div>
          {isEditingCredential && (
            <Card className="mt-2">
              <Text className="mb-2">
                Update the Salesforce Connected App credentials below. All
                connectors using this credential will pick up the change on
                their next run.
              </Text>
              <CredentialForm<SalesforceCredentialJson>
                existingCredentialId={SalesforceCredential.id}
                formBody={
                  <>
                    <TextFormField
                      name="sf_client_id"
                      label="Salesforce Client Id:"
                    />
                    <TextFormField
                      name="sf_client_secret"
                      label="Salesforce Client Secret:"
                      type="password"
                    />
                    <TextFormField
                      name="sf_username"
                      label="Salesforce Username:"
                    />
                    <TextFormField
                      name="sf_password"
                      label="Salesforce Password:"
                      type="password"
                    />
                  </>
                }
                validationSchema={Yup.object().shape({
                  sf_client_id: Yup.string().required(
                    "Please enter your Salesforce Client Id"
                  ),
                  sf_client_secret: Yup.string().required(
                    "Please enter your Salesforce Client Secret"
                  ),
                  sf_username: Yup.string().required(
                    "Please enter your Salesforce username"
                  ),
                  sf_password: Yup.string().required(
                    "Please enter your Salesforce password"
                  ),
                  // Hidden discriminator (set in initialValues below);
                  // declared here so the schema's inferred Shape matches
                  // SalesforceCredentialJson.
                  sf_credential_kind: Yup.string()
                    .oneOf(["account", "kbarticles"])
                    .optional(),
                })}
                initialValues={{
                  sf_client_id:
                    SalesforceCredential.credential_json.sf_client_id || "",
                  sf_client_secret:
                    SalesforceCredential.credential_json.sf_client_secret || "",
                  sf_username:
                    SalesforceCredential.credential_json.sf_username || "",
                  sf_password:
                    SalesforceCredential.credential_json.sf_password || "",
                  sf_credential_kind: "account",
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
            As a first step, please provide the Salesforce Connected App&apos;s
            client_id and client_secret along with the Salesforce account&apos;s
            username and password.
          </Text>
          <Card className="mt-2">
            <CredentialForm<SalesforceCredentialJson>
              formBody={
                <>
                  <TextFormField
                    name="sf_client_id"
                    label="Salesforce Client Id:"
                  />
                  <TextFormField
                    name="sf_client_secret"
                    label="Salesforce Client Secret:"
                    type="password"
                  />
                  <TextFormField
                    name="sf_username"
                    label="Salesforce Username:"
                  />
                  <TextFormField
                    name="sf_password"
                    label="Salesforce Password:"
                    type="password"
                  />
                </>
              }
              validationSchema={Yup.object().shape({
                sf_client_id: Yup.string().required(
                  "Please enter your Salesforce Client Id"
                ),
                sf_client_secret: Yup.string().required(
                  "Please enter your Salesforce Client Secret"
                ),
                sf_username: Yup.string().required(
                  "Please enter your Salesforce username"
                ),
                sf_password: Yup.string().required(
                  "Please enter your Salesforce password"
                ),
                sf_credential_kind: Yup.string()
                  .oneOf(["account", "kbarticles"])
                  .optional(),
              })}
              initialValues={{
                sf_client_id: "",
                sf_client_secret: "",
                sf_username: "",
                sf_password: "",
                sf_credential_kind: "account",
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
        Step 2: Manage Salesforce Connector
      </Title>

      {SalesforceConnectorIndexingStatuses.length > 0 && (
        <>
          <Text className="mb-2">
            The latest state of your Salesforce Account records is fetched every
            10 minutes.
          </Text>
          <div className="mb-2">
            <ConnectorsTable<SalesforceConfig, SalesforceCredentialJson>
              connectorIndexingStatuses={SalesforceConnectorIndexingStatuses}
              liveCredential={SalesforceCredential}
              getCredential={(credential) =>
                credential.credential_json.sf_client_secret
              }
              onUpdate={() =>
                mutate("/api/manage/admin/connector/indexing-status")
              }
              onCredentialLink={async (connectorId) => {
                if (SalesforceCredential) {
                  await linkCredential(connectorId, SalesforceCredential.id);
                  mutate("/api/manage/admin/connector/indexing-status");
                }
              }}
              includeName
            />
          </div>
        </>
      )}

      {SalesforceCredential ? (
        <Card className="mt-4">
          <Text className="mb-2">
            The Salesforce connector indexes the <b>Account</b> object using a
            curated set of fields. Filtering is configured at the indexer
            process via environment variables — restart the indexer worker after
            changing them:
          </Text>
          <ul className="list-disc list-inside text-sm mb-4">
            <li>
              <code>SF_ACCOUNT_NAME_FILTER</code> — substring match on
              Account.Name (set <code>SF_ACCOUNT_NAME_EXACT=1</code> for an
              exact match).
            </li>
            <li>
              <code>SF_MAINTENANCE_FLAG_FILTER</code> — exact match on
              Maintenance_Flag__c; comma-separate for multiple values.
            </li>
          </ul>
          <ConnectorForm<SalesforceConfig>
            nameBuilder={() => "SF-Account"}
            ccPairNameBuilder={() => "SF-Account"}
            source="salesforce"
            inputType="poll"
            formBody={<></>}
            // SalesforceConfig has `requested_objects?: string[]`; the
            // SF-Account flow doesn't render an input for it (the
            // backend hard-codes the Account object set), but yup's
            // Shape inference still requires the field be declared.
            validationSchema={Yup.object().shape({
              requested_objects: Yup.array()
                .of(Yup.string().required())
                .optional(),
            })}
            initialValues={{}}
            credentialId={SalesforceCredential.id}
            refreshFreq={10 * 60} // 10 minutes
          />
        </Card>
      ) : (
        <Text>
          Please provide all Salesforce info in Step 1 first! Once you&apos;re
          done with that, you can create the Account connector below.
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

      <AdminPageTitle icon={<SalesforceIcon size={32} />} title="SF-Account" />

      <MainSection />
    </div>
  );
}
