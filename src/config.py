"""환경 변수를 읽어 검증한다. 키와 엔드포인트는 코드에 두지 않는다."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PROVIDERS = ("azure", "ollama")

_AZURE_REQUIRED = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_INDEX",
)
_API_REQUIRED = ("API_KEY", "MIN_SEARCH_SCORE")
MIN_API_KEY_CHARS = 32
# Key Vault 참조가 해석되지 않으면 App Service가 이 문자열을 그대로 설정 값으로 넘긴다.
_UNRESOLVED_REFERENCE = "@microsoft.keyvault("


class ConfigError(Exception):
    """설정이 잘못되어 시작할 수 없다. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str = ""
    min_search_score: float = 0.0
    azure_openai_endpoint: str = ""
    azure_openai_chat_deployment: str = ""
    azure_openai_embedding_deployment: str = ""
    azure_search_endpoint: str = ""
    azure_search_index: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_chat_model: str = "llama3.2"
    ollama_embedding_model: str = "bge-m3"
    local_index_path: str = ""


def load_settings(env: Mapping[str, str] | None = None, *, for_api: bool = True) -> Settings:
    """설정을 읽는다. 인덱싱(`for_api=False`)에는 API 키와 임계치가 필요 없다."""
    env = os.environ if env is None else env
    get = lambda name: (env.get(name) or "").strip()  # noqa: E731

    provider = get("LLM_PROVIDER")
    if not provider:
        raise ConfigError("필수 환경 변수가 없습니다: LLM_PROVIDER")
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER 값이 잘못되었습니다. 허용 값: {', '.join(PROVIDERS)}")

    required = list(_API_REQUIRED) if for_api else []
    if provider == "azure":
        required += _AZURE_REQUIRED
    missing = [name for name in required if not get(name)]
    if missing:
        raise ConfigError(f"필수 환경 변수가 없습니다: {', '.join(missing)}")

    min_score = 0.0
    if for_api:
        api_key = get("API_KEY")
        if api_key.lower().startswith(_UNRESOLVED_REFERENCE):
            raise ConfigError("API_KEY의 Key Vault 참조가 해석되지 않았습니다.")
        if len(api_key) < MIN_API_KEY_CHARS:
            raise ConfigError(f"API_KEY는 {MIN_API_KEY_CHARS}자 이상이어야 합니다.")
        try:
            min_score = float(get("MIN_SEARCH_SCORE"))
        except ValueError:
            raise ConfigError("MIN_SEARCH_SCORE는 숫자여야 합니다.") from None
        if not 0 < min_score <= 1:
            raise ConfigError("MIN_SEARCH_SCORE는 0보다 크고 1 이하여야 합니다.")

    defaults = Settings(provider=provider)
    return Settings(
        provider=provider,
        api_key=get("API_KEY"),
        min_search_score=min_score,
        azure_openai_endpoint=get("AZURE_OPENAI_ENDPOINT"),
        azure_openai_chat_deployment=get("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        azure_openai_embedding_deployment=get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
        azure_search_endpoint=get("AZURE_SEARCH_ENDPOINT"),
        azure_search_index=get("AZURE_SEARCH_INDEX"),
        ollama_base_url=defaults.ollama_base_url,
        ollama_chat_model=get("OLLAMA_CHAT_MODEL") or defaults.ollama_chat_model,
        ollama_embedding_model=get("OLLAMA_EMBEDDING_MODEL") or defaults.ollama_embedding_model,
        local_index_path=get("LOCAL_INDEX_PATH"),
    )
