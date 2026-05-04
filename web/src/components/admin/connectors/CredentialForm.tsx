import React, { useState } from "react";
import { Formik, Form } from "formik";
import * as Yup from "yup";
import { Popup } from "./Popup";
import { CredentialBase } from "@/lib/types";
import { createCredential, updateCredential } from "@/lib/credential";
import { Button } from "@tremor/react";

export async function submitCredential<T>(
  credential: CredentialBase<T>,
  existingCredentialId?: number
): Promise<{ message: string; isSuccess: boolean }> {
  try {
    const response =
      existingCredentialId !== undefined
        ? await updateCredential(existingCredentialId, credential)
        : await createCredential(credential);
    if (response.ok) {
      return { message: "Success!", isSuccess: true };
    }
    const errorData = await response.json();
    return { message: `Error: ${errorData.detail}`, isSuccess: false };
  } catch (error) {
    return { message: `Error: ${error}`, isSuccess: false };
  }
}

interface Props<YupObjectType extends Yup.AnyObject> {
  formBody: JSX.Element | null;
  validationSchema: Yup.ObjectSchema<YupObjectType>;
  initialValues: YupObjectType;
  onSubmit: (isSuccess: boolean) => void;
  // When set, the form PATCHes the existing credential instead of creating
  // a new one. Provide initialValues = existing credential_json so the user
  // sees their current settings prefilled.
  existingCredentialId?: number;
  // Optional: rendered alongside the submit button (e.g. a Cancel that
  // closes the edit panel without saving).
  extraActions?: JSX.Element;
}

export function CredentialForm<T extends Yup.AnyObject>({
  formBody,
  validationSchema,
  initialValues,
  onSubmit,
  existingCredentialId,
  extraActions,
}: Props<T>): JSX.Element {
  const [popup, setPopup] = useState<{
    message: string;
    type: "success" | "error";
  } | null>(null);

  const isEditing = existingCredentialId !== undefined;

  return (
    <>
      {popup && <Popup message={popup.message} type={popup.type} />}
      <Formik
        initialValues={initialValues}
        validationSchema={validationSchema}
        enableReinitialize
        onSubmit={(values, formikHelpers) => {
          formikHelpers.setSubmitting(true);
          submitCredential<T>(
            {
              credential_json: values,
              admin_public: true,
            },
            existingCredentialId
          ).then(({ message, isSuccess }) => {
            setPopup({ message, type: isSuccess ? "success" : "error" });
            formikHelpers.setSubmitting(false);
            setTimeout(() => {
              setPopup(null);
            }, 4000);
            onSubmit(isSuccess);
          });
        }}
      >
        {({ isSubmitting }) => (
          <Form>
            {formBody}
            <div className="flex gap-2 justify-center">
              <Button
                type="submit"
                size="xs"
                color="green"
                disabled={isSubmitting}
                className="w-64"
              >
                {isEditing ? "Save changes" : "Update"}
              </Button>
              {extraActions}
            </div>
          </Form>
        )}
      </Formik>
    </>
  );
}
