from datetime import timedelta
from typing import Any

from onyx.configs.app_configs import LLM_MODEL_UPDATE_API_URL
from onyx.configs.constants import ONYX_CLOUD_CELERY_TASK_PREFIX
from onyx.configs.constants import OnyxCeleryPriority
from onyx.configs.constants import OnyxCeleryQueues
from onyx.configs.constants import OnyxCeleryTask
from shared_configs.configs import MULTI_TENANT

# choosing 15 minutes because it roughly gives us enough time to process many tasks
# we might be able to reduce this greatly if we can run a unified
# loop across all tenants rather than tasks per tenant

# we set expires because it isn't necessary to queue up these tasks
# it's only important that they run relatively regularly
BEAT_EXPIRES_DEFAULT = 15 * 60  # 15 minutes (in seconds)

# tasks that only run in the cloud
# the name attribute must start with ONYX_CELERY_CLOUD_PREFIX = "cloud" to be filtered
# by the DynamicTenantScheduler
cloud_tasks_to_schedule = [
    {
        "name": f"{ONYX_CLOUD_CELERY_TASK_PREFIX}_check-for-indexing",
        "task": OnyxCeleryTask.CLOUD_CHECK_FOR_INDEXING,
        "schedule": timedelta(seconds=15),
        "options": {
            "priority": OnyxCeleryPriority.HIGHEST,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
]

# tasks that run in either self-hosted on cloud
tasks_to_schedule = [
    {
        "name": "check-for-vespa-sync",
        "task": OnyxCeleryTask.CHECK_FOR_VESPA_SYNC_TASK,
        "schedule": timedelta(seconds=20),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "check-for-connector-deletion",
        "task": OnyxCeleryTask.CHECK_FOR_CONNECTOR_DELETION,
        "schedule": timedelta(seconds=20),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "check-for-prune",
        "task": OnyxCeleryTask.CHECK_FOR_PRUNING,
        "schedule": timedelta(seconds=15),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "kombu-message-cleanup",
        "task": OnyxCeleryTask.KOMBU_MESSAGE_CLEANUP_TASK,
        "schedule": timedelta(seconds=3600),
        "options": {
            "priority": OnyxCeleryPriority.LOWEST,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "monitor-vespa-sync",
        "task": OnyxCeleryTask.MONITOR_VESPA_SYNC,
        "schedule": timedelta(seconds=5),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "monitor-background-processes",
        "task": OnyxCeleryTask.MONITOR_BACKGROUND_PROCESSES,
        "schedule": timedelta(seconds=5),
        "options": {
            "priority": OnyxCeleryPriority.HIGH,
            "expires": BEAT_EXPIRES_DEFAULT,
            "queue": OnyxCeleryQueues.MONITORING,
        },
    },
    {
        "name": "check-for-doc-permissions-sync",
        "task": OnyxCeleryTask.CHECK_FOR_DOC_PERMISSIONS_SYNC,
        "schedule": timedelta(seconds=30),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
    {
        "name": "check-for-external-group-sync",
        "task": OnyxCeleryTask.CHECK_FOR_EXTERNAL_GROUP_SYNC,
        "schedule": timedelta(seconds=20),
        "options": {
            "priority": OnyxCeleryPriority.MEDIUM,
            "expires": BEAT_EXPIRES_DEFAULT,
        },
    },
]

if not MULTI_TENANT:
    tasks_to_schedule.append(
        {
            "name": "check-for-indexing",
            "task": OnyxCeleryTask.CHECK_FOR_INDEXING,
            "schedule": timedelta(seconds=15),
            "options": {
                "priority": OnyxCeleryPriority.MEDIUM,
                "expires": BEAT_EXPIRES_DEFAULT,
            },
        }
    )

# Only add the LLM model update task if the API URL is configured
if LLM_MODEL_UPDATE_API_URL:
    tasks_to_schedule.append(
        {
            "name": "check-for-llm-model-update",
            "task": OnyxCeleryTask.CHECK_FOR_LLM_MODEL_UPDATE,
            "schedule": timedelta(hours=1),  # Check every hour
            "options": {
                "priority": OnyxCeleryPriority.LOW,
                "expires": BEAT_EXPIRES_DEFAULT,
            },
        }
    )


def get_cloud_tasks_to_schedule() -> list[dict[str, Any]]:
    return cloud_tasks_to_schedule


def get_tasks_to_schedule() -> list[dict[str, Any]]:
    return tasks_to_schedule
