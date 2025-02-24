"""Module with custom fields processing functions"""
import os
from typing import Any
from typing import List

from jira import JIRA
from jira.resources import CustomFieldOption
from jira.resources import Issue
from jira.resources import User

from danswer.connectors.models import BasicExpertInfo
from danswer.utils.logger import setup_logger

logger = setup_logger()

JIRA_API_VERSION = os.environ.get("JIRA_API_VERSION") or "2"


def best_effort_basic_expert_info(obj: Any) -> BasicExpertInfo | None:
    display_name = None
    email = None
    if hasattr(obj, "display_name"):
        display_name = obj.display_name
    else:
        display_name = obj.get("displayName")

    if hasattr(obj, "emailAddress"):
        email = obj.emailAddress
    else:
        email = obj.get("emailAddress")

    if not email and not display_name:
        return None

    return BasicExpertInfo(display_name=display_name, email=email)


def best_effort_get_field_from_issue(jira_issue: Issue, field: str) -> Any:
    if hasattr(jira_issue.fields, field):
        return getattr(jira_issue.fields, field)

    try:
        return jira_issue.raw["fields"][field]
    except Exception:
        return None
    

def extract_text_from_content(content: dict) -> str:
    texts = []
    if "content" in content:
        for block in content["content"]:
            if "content" in block:
                for item in block["content"]:
                    if item["type"] == "text":
                        texts.append(item["text"])
    return " ".join(texts)


def get_comment_strs(
    issue: Issue, comment_email_blacklist: tuple[str, ...] = ()
) -> list[str]:
    comment_strs = []
    for comment in issue.fields.comment.comments:
        try:
            body_text = (
                comment.body
                if JIRA_API_VERSION == "2"
                else extract_text_from_content(comment.raw["body"])
            )

            if (
                hasattr(comment, "author")
                and hasattr(comment.author, "emailAddress")
                and comment.author.emailAddress in comment_email_blacklist
            ):
                continue  # Skip adding comment if author's email is in blacklist

            comment_strs.append(body_text)
        except Exception as e:
            logger.error(f"Failed to process comment due to an error: {e}")
            continue

    return comment_strs


class CustomFieldExtractor:
    @staticmethod
    def _process_custom_field_value(value: Any) -> str:
        """
        Process a custom field value to a string
        """
        try:
            if isinstance(value, str):
                return value
            elif isinstance(value, CustomFieldOption):
                return value.value
            elif isinstance(value, User):
                return value.displayName
            elif isinstance(value, List):
                return " ".join(
                    [CustomFieldExtractor._process_custom_field_value(v) for v in value]
                )
            else:
                return str(value)
        except Exception as e:
            logger.error(f"Error processing custom field value {value}: {e}")
            return ""

    @staticmethod
    def get_issue_custom_fields(
        jira: Issue, custom_fields: dict, max_value_length: int = 250
    ) -> dict:
        """
        Process all custom fields of an issue to a dictionary of strings
        :param jira: jira_issue, bug or similar
        :param custom_fields: custom fields dictionary
        :param max_value_length: maximum length of the value to be processed, if exceeded, it will be truncated
        """

        issue_custom_fields = {
            custom_fields[key]: value
            for key, value in jira.fields.__dict__.items()
            if value and key in custom_fields.keys()
        }

        processed_fields = {}

        if issue_custom_fields:
            for key, value in issue_custom_fields.items():
                processed = CustomFieldExtractor._process_custom_field_value(value)
                # We need max length  parameter, because there are some plugins that often has very long description
                # and there is just a technical information so we just avoid long values
                if len(processed) < max_value_length:
                    processed_fields[key] = processed

        return processed_fields

    @staticmethod
    def get_all_custom_fields(jira_client: JIRA) -> dict:
        """Get all custom fields from Jira"""
        fields = jira_client.fields()
        fields_dct = {
            field["id"]: field["name"] for field in fields if field["custom"] is True
        }
        return fields_dct


class CommonFieldExtractor:
    @staticmethod
    def get_issue_common_fields(jira: Issue) -> dict:
        return {
            "Priority": jira.fields.priority.name if jira.fields.priority else None,
            "Reporter": jira.fields.reporter.displayName
            if jira.fields.reporter
            else None,
            "Assignee": jira.fields.assignee.displayName
            if jira.fields.assignee
            else None,
            "Status": jira.fields.status.name if jira.fields.status else None,
            "Resolution": jira.fields.resolution.name
            if jira.fields.resolution
            else None,
        }
