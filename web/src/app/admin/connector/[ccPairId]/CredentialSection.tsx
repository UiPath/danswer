"use client";

import { useState } from "react";
import * as Yup from "yup";
import { Button, Card, Divider, Text, Title } from "@tremor/react";
import { Formik, Form } from "formik";
import { Credential } from "@/lib/types";
import { updateCredential } from "@/lib/credential";
import { TextFormField } from "@/components/admin/connectors/Field";
import { EditIcon } from "@/components/icons/icons";
import { Popup } from "@/components/admin/connectors/Popup";

const SENSITIVE_KEY_PATTERNS = ["password", "secret", "token", "key", "private"];

function isSensitiveKey(key: string): boolean {
  const lower = key.toLowerCase();
  return SENSITIVE_KEY_PATTERNS.some((p) => lower.includes(p));
}

function humanizeKey(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function maskValue(key: string, value: unknown): string {
  if (value == null) return "";
  const str = String(value);
  if (!isSensitiveKey(key)) return str;
  if (str.length <= 4) return "••••";
  return `${"•".repeat(8)}${str.slice(-4)}`;
}

interface Props {
  credential: Credential<Record<string, unknown>>;
  onUpdated: () => void;
}

export function CredentialSection({ credential, onUpdated }: Props) {
  const [isEditing, setIsEditing] = useState(false);
  const [popup, setPopup] = useState<{
    message: string;
    type: "success" | "error";
  } | null>(null);

  const credentialJson = credential.credential_json || {};
  const keys = Object.keys(credentialJson);

  if (keys.length === 0) {
    return null;
  }

  // Pre-build the validation schema and initial values from the existing
  // credential JSON. Every existing key is treated as required (matches
  // the create-time forms which require everything).
  const initialValues: Record<string, string> = {};
  const schemaShape: Record<string, Yup.AnySchema> = {};
  for (const key of keys) {
    const value = credentialJson[key];
    initialValues[key] = value == null ? "" : String(value);
    schemaShape[key] = Yup.string().required(`${humanizeKey(key)} is required`);
  }
  const validationSchema = Yup.object().shape(schemaShape);

  return (
    <div className="mt-6">
      {popup && <Popup message={popup.message} type={popup.type} />}
      <div className="flex items-center">
        <Title>Credential</Title>
        <button
          className="ml-2 hover:bg-hover rounded p-1"
          title={isEditing ? "Close credential editor" : "Edit credential"}
          onClick={() => setIsEditing((v) => !v)}
        >
          <EditIcon size={16} />
        </button>
      </div>

      {!isEditing ? (
        <div className="mt-2">
          <Text className="mb-2">
            Credential ID <b>{credential.id}</b>. Sensitive values are masked.
            Click the pencil to edit.
          </Text>
          <Card>
            <table className="text-sm">
              <tbody>
                {keys.map((key) => (
                  <tr key={key} className="align-top">
                    <td className="font-medium pr-4 py-1 whitespace-nowrap">
                      {humanizeKey(key)}
                    </td>
                    <td className="font-mono py-1 break-all">
                      {maskValue(key, credentialJson[key])}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      ) : (
        <Card className="mt-2">
          <Text className="mb-3">
            Update the credential JSON below. The change is saved against the
            existing credential row, so all connectors that share this
            credential pick it up on their next run — no re-indexing needed.
          </Text>
          <Formik
            initialValues={initialValues}
            validationSchema={validationSchema}
            enableReinitialize
            onSubmit={async (values, helpers) => {
              helpers.setSubmitting(true);
              try {
                const resp = await updateCredential(credential.id, {
                  credential_json: values,
                  admin_public: credential.admin_public,
                });
                if (resp.ok) {
                  setPopup({ message: "Credential updated", type: "success" });
                  setIsEditing(false);
                  onUpdated();
                } else {
                  const err = await resp.json().catch(() => ({}));
                  setPopup({
                    message: `Error: ${err.detail || resp.statusText}`,
                    type: "error",
                  });
                }
              } catch (e) {
                setPopup({ message: `Error: ${e}`, type: "error" });
              } finally {
                helpers.setSubmitting(false);
                setTimeout(() => setPopup(null), 4000);
              }
            }}
          >
            {({ isSubmitting }) => (
              <Form>
                {keys.map((key) => (
                  <TextFormField
                    key={key}
                    name={key}
                    label={`${humanizeKey(key)}:`}
                    type={isSensitiveKey(key) ? "password" : "text"}
                  />
                ))}
                <div className="flex gap-2 justify-center mt-4">
                  <Button
                    type="submit"
                    size="xs"
                    color="green"
                    disabled={isSubmitting}
                    className="w-64"
                  >
                    Save changes
                  </Button>
                  <Button
                    type="button"
                    size="xs"
                    color="gray"
                    onClick={() => setIsEditing(false)}
                  >
                    Cancel
                  </Button>
                </div>
              </Form>
            )}
          </Formik>
        </Card>
      )}
      <Divider />
    </div>
  );
}
