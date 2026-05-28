from danswer.configs.app_configs import DISABLE_GENERATIVE_AI
from danswer.configs.chat_configs import QA_TIMEOUT
from danswer.configs.model_configs import GEN_AI_MODEL_NAME
from danswer.configs.model_configs import GEN_AI_TEMPERATURE
from danswer.configs.model_configs import GEN_AI_VENDOR
from danswer.db.models import Persona
from danswer.llm.custom_llm import CustomModelServer
from danswer.llm.exceptions import GenAIDisabledException
from danswer.llm.interfaces import LLM
from danswer.llm.override_models import LLMOverride


def get_main_llm_from_tuple(
    llms: tuple[LLM, LLM],
) -> LLM:
    return llms[0]


def get_llms_for_persona(
    persona: Persona,
    llm_override: LLMOverride | None = None,
    additional_headers: dict[str, str] | None = None,
) -> tuple[LLM, LLM]:
    model_provider_override = llm_override.model_provider if llm_override else None
    model_version_override = llm_override.model_version if llm_override else None
    llm_override.temperature if llm_override else None

    vendor = model_provider_override or GEN_AI_VENDOR
    model = model_version_override or GEN_AI_MODEL_NAME

    llm = get_llm(provider=vendor, model=model)
    return llm, llm


def get_default_llms(
    timeout: int = QA_TIMEOUT,
    temperature: float = GEN_AI_TEMPERATURE,
    additional_headers: dict[str, str] | None = None,
) -> tuple[LLM, LLM]:
    if DISABLE_GENERATIVE_AI:
        raise GenAIDisabledException()

    llm = get_llm(provider=GEN_AI_VENDOR, model=GEN_AI_MODEL_NAME, timeout=timeout)
    return llm, llm


def get_llm(
    provider: str,
    model: str,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    custom_config: dict[str, str] | None = None,
    temperature: float = GEN_AI_TEMPERATURE,
    timeout: int = QA_TIMEOUT,
    additional_headers: dict[str, str] | None = None,
) -> LLM:
    return CustomModelServer(
        timeout=timeout,
        api_key=api_key,
        llm_vendor=provider,
        llm_model_name=model,
    )
