"use client";

import * as Yup from "yup";
import { useState } from "react";
import { EditIcon, GithubIcon, TrashIcon } from "@/components/icons/icons";
import { TextFormField } from "@/components/admin/connectors/Field";
import { HealthCheckBanner } from "@/components/health/healthcheck";
import useSWR, { useSWRConfig } from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";
import { ErrorCallout } from "@/components/ErrorCallout";
import {
  GithubFilesConfig,
  GithubCredentialJson,
  Credential,
  ConnectorIndexingStatus,
} from "@/lib/types";
import { ConnectorForm } from "@/components/admin/connectors/ConnectorForm";
import { LoadingAnimation } from "@/components/Loading";
import { CredentialForm } from "@/components/admin/connectors/CredentialForm";
import { adminDeleteCredential, linkCredential } from "@/lib/credential";
import { ConnectorsTable } from "@/components/admin/connectors/table/ConnectorsTable";
import { usePublicCredentials } from "@/lib/hooks";
import { Button, Card, Divider, Text, Title } from "@tremor/react";
import { AdminPageTitle } from "@/components/admin/Title";

const Main = () => {
  const { mutate } = useSWRConfig();
  const [isEditingCredential, setIsEditingCredential] = useState(false);
  const {
    data: connectorIndexingStatuses,
    isLoading: isConnectorIndexingStatusesLoading,
    error: connectorIndexingStatusesError,
  } = useSWR<ConnectorIndexingStatus<any, any>[]>(
    "/api/manage/admin/connector/indexing-status",
    errorHandlingFetcher
  );

  const {
    data: credentialsData,
    isLoading: isCredentialsLoading,
    error: credentialsError,
    refreshCredentials,
  } = usePublicCredentials();

  if (
    (!connectorIndexingStatuses && isConnectorIndexingStatusesLoading) ||
    (!credentialsData && isCredentialsLoading)
  ) {
    return <LoadingAnimation text="Loading" />;
  }

  if (connectorIndexingStatusesError || !connectorIndexingStatuses) {
    return (
      <ErrorCallout
        errorTitle="Something went wrong :("
        errorMsg={connectorIndexingStatusesError?.info?.detail}
      />
    );
  }

  if (credentialsError || !credentialsData) {
    return (
      <ErrorCallout
        errorTitle="Something went wrong :("
        errorMsg={credentialsError?.info?.detail}
      />
    );
  }

  const indexingStatuses: ConnectorIndexingStatus<
    GithubFilesConfig,
    GithubCredentialJson
  >[] = connectorIndexingStatuses.filter(
    (connectorIndexingStatus) =>
      connectorIndexingStatus.connector.source === "github_files"
  );
  const githubCredential: Credential<GithubCredentialJson> | undefined =
    credentialsData.find(
      (credential) => credential.credential_json?.github_access_token
    );

  return (
    <>
      <Title className="mb-2 mt-6 ml-auto mr-auto">
        Step 1: Provide your GitHub access token
      </Title>
      {githubCredential ? (
        <>
          <div className="flex mb-1 text-sm items-center">
            <p className="my-auto">Existing Access Token: </p>
            <p className="ml-1 italic my-auto">
              {githubCredential.credential_json.github_access_token}
            </p>
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
                await adminDeleteCredential(githubCredential.id);
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
                Update the GitHub personal access token. Both the GitHub and
                GitHub-Files connectors share this credential.
              </Text>
              <CredentialForm<GithubCredentialJson>
                existingCredentialId={githubCredential.id}
                formBody={
                  <TextFormField
                    name="github_access_token"
                    label="Access Token:"
                    type="password"
                  />
                }
                validationSchema={Yup.object().shape({
                  github_access_token: Yup.string().required(
                    "Please enter the access token for Github"
                  ),
                })}
                initialValues={{
                  github_access_token:
                    githubCredential.credential_json.github_access_token || "",
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
          <Text>
            The same access token used for the standard GitHub connector works
            here. The token needs <code>repo</code> scope (or just
            <code> public_repo</code> for public repos).
          </Text>
          <Card className="mt-4">
            <CredentialForm<GithubCredentialJson>
              formBody={
                <TextFormField
                  name="github_access_token"
                  label="Access Token:"
                  type="password"
                />
              }
              validationSchema={Yup.object().shape({
                github_access_token: Yup.string().required(
                  "Please enter the access token for Github"
                ),
              })}
              initialValues={{ github_access_token: "" }}
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
        Step 2: Configure the file scraper
      </Title>

      {indexingStatuses.length > 0 && (
        <>
          <Text className="mb-2">
            Configured GitHub-Files connectors below. We re-fetch matching files
            every <b>10</b> minutes (and skip the run if nothing under the path
            prefix has been committed since the last poll).
          </Text>
          <div className="mb-2">
            <ConnectorsTable<GithubFilesConfig, GithubCredentialJson>
              connectorIndexingStatuses={indexingStatuses}
              liveCredential={githubCredential}
              getCredential={(credential) =>
                credential.credential_json.github_access_token
              }
              onCredentialLink={async (connectorId) => {
                if (githubCredential) {
                  await linkCredential(connectorId, githubCredential.id);
                  mutate("/api/manage/admin/connector/indexing-status");
                }
              }}
              specialColumns={[
                {
                  header: "Repository",
                  key: "repository",
                  getValue: (ccPairStatus) => {
                    const c = ccPairStatus.connector.connector_specific_config;
                    return `${c.repo_owner}/${c.repo_name}`;
                  },
                },
                {
                  header: "Path",
                  key: "path_prefix",
                  getValue: (ccPairStatus) => {
                    const c = ccPairStatus.connector.connector_specific_config;
                    const ext = c.file_extension || ".json";
                    const branch = c.branch ? `@${c.branch}` : "";
                    return `${c.path_prefix}/<dir>/*${ext}${branch}`;
                  },
                },
              ]}
              onUpdate={() =>
                mutate("/api/manage/admin/connector/indexing-status")
              }
            />
          </div>
          <Divider />
        </>
      )}

      {githubCredential ? (
        <Card className="mt-4">
          <Text className="mb-4">
            Indexes files matching{" "}
            <code>&lt;path_prefix&gt;/&lt;dir&gt;/&lt;file&gt;&lt;ext&gt;</code>{" "}
            — i.e. exactly one folder under the prefix, file directly inside.
            Defaults target a{" "}
            <code>service-catalog/products/&lt;product&gt;/*.json</code> layout.
          </Text>

          <ConnectorForm<GithubFilesConfig>
            nameBuilder={(values) =>
              `GithubFiles-${values.repo_owner}/${values.repo_name}`
            }
            ccPairNameBuilder={(values) =>
              `${values.repo_owner}/${values.repo_name}`
            }
            source="github_files"
            inputType="poll"
            formBody={
              <>
                <TextFormField name="repo_owner" label="Repository Owner:" />
                <TextFormField name="repo_name" label="Repository Name:" />
                <TextFormField
                  name="path_prefix"
                  label="Path Prefix:"
                  subtext={
                    <>
                      The folder containing per-product subfolders. Files are
                      indexed at exactly one level deeper.
                    </>
                  }
                />
                <TextFormField
                  name="file_extension"
                  label="File Extension:"
                  subtext="e.g. .json — only files with this extension at the matching depth are indexed."
                />
                <TextFormField
                  name="branch"
                  label="Branch (optional):"
                  subtext="Leave blank to use the repository's default branch."
                />
              </>
            }
            validationSchema={Yup.object().shape({
              repo_owner: Yup.string().required(
                "Please enter the owner of the repository"
              ),
              repo_name: Yup.string().required(
                "Please enter the name of the repository"
              ),
              path_prefix: Yup.string().required(
                "Please enter the path prefix to scan"
              ),
              file_extension: Yup.string().required(
                "Please enter the file extension to filter on"
              ),
              branch: Yup.string(),
            })}
            initialValues={{
              repo_owner: "",
              repo_name: "",
              path_prefix: "service-catalog/products",
              file_extension: ".json",
              branch: "",
            }}
            refreshFreq={10 * 60}
            credentialId={githubCredential.id}
          />
        </Card>
      ) : (
        <Text>Provide your access token in Step 1 first.</Text>
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

      <AdminPageTitle icon={<GithubIcon size={32} />} title="GitHub-Files" />

      <Main />
    </div>
  );
}
