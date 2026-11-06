"""외부 서비스 경계. SDK 오류는 여기서 공통 예외로 바꾸고, 앞쪽 코드는 SDK를 모른다."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import openai
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ServiceRequestTimeoutError,
    ServiceResponseTimeoutError,
)

from config import Settings

OPENAI_SCOPE = "https://ai.azure.com/.default"
MAX_COMPLETION_TOKENS = 1000
DEFAULT_RETRY_AFTER = "5"

# 질의 경로의 제한 시간(초). design/decisions.md D-13.
EMBEDDING_TIMEOUT = 5.0
SEARCH_TIMEOUT = 5.0
CHAT_TIMEOUT = 20.0

# 인덱싱은 한 번에 많이 보내고 호출자가 기다릴 수 있으므로 넉넉하게 둔다.
INGEST_TIMEOUT = 60.0
INGEST_RETRIES = 3

# 이 PC의 Ollama는 CPU로 실행되어 느리다. AC-PERF는 이 모드에 적용하지 않는다.
OLLAMA_TIMEOUT = 600.0

VECTOR_FIELD = "content_vector"
EMBEDDING_DIMENSIONS = {"azure": 1536, "ollama": 1024}


class UpstreamBusy(Exception):
    def __init__(self, retry_after: str = DEFAULT_RETRY_AFTER) -> None:
        super().__init__("upstream busy")
        self.retry_after = retry_after


class UpstreamTimeout(Exception):
    pass


class UpstreamError(Exception):
    pass


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    source: str
    content: str
    score: float


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ChatModel(Protocol):
    def complete(self, system: str, user: str, schema: dict) -> str: ...


class VectorStore(Protocol):
    def search(self, vector: list[float], k: int) -> list[SearchHit]: ...


@dataclass(frozen=True)
class Providers:
    embedder: Embedder
    chat: ChatModel
    store: VectorStore


def _retry_after(headers) -> str:
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    return value if value and str(value).isdigit() else DEFAULT_RETRY_AFTER


def _call_openai(call):
    try:
        return call()
    except openai.RateLimitError as exc:
        raise UpstreamBusy(_retry_after(exc.response.headers)) from None
    except openai.APITimeoutError:
        raise UpstreamTimeout() from None
    except (openai.OpenAIError, AzureError) as exc:
        # AzureError는 토큰 발급 실패다. 메시지는 내부 정보를 담을 수 있어 전달하지 않는다.
        raise UpstreamError(type(exc).__name__) from None


def _call_search(call):
    try:
        return call()
    except (ServiceRequestTimeoutError, ServiceResponseTimeoutError):
        raise UpstreamTimeout() from None
    except HttpResponseError as exc:
        if exc.status_code == 429:
            raise UpstreamBusy(_retry_after(exc.response.headers if exc.response else None)) from None
        raise UpstreamError(type(exc).__name__) from None
    except AzureError as exc:
        raise UpstreamError(type(exc).__name__) from None


class OpenAIEmbedder:
    def __init__(self, client: openai.OpenAI, model: str) -> None:
        self._client = client
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = _call_openai(lambda: self._client.embeddings.create(model=self._model, input=texts))
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


class OpenAIChat:
    def __init__(self, client: openai.OpenAI, model: str, params: dict) -> None:
        self._client = client
        self._model = model
        self._params = params

    def complete(self, system: str, user: str, schema: dict) -> str:
        response = _call_openai(
            lambda: self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": schema},
                **self._params,
            )
        )
        return response.choices[0].message.content or ""


class AzureSearchStore:
    """Azure AI Search 벡터 인덱스."""

    def __init__(self, client, index_client=None, index_name: str = "") -> None:
        self._client = client
        self._index_client = index_client
        self._index_name = index_name

    def search(self, vector: list[float], k: int) -> list[SearchHit]:
        from azure.search.documents.models import VectorizedQuery

        def run() -> list[SearchHit]:
            results = self._client.search(
                search_text=None,
                vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=k, fields=VECTOR_FIELD)],
                select=["id", "source", "content"],
                top=k,
            )
            return [
                SearchHit(chunk_id=row["id"], source=row["source"], content=row["content"], score=row["@search.score"])
                for row in results
            ]

        return _call_search(run)

    def ensure_index(self, dimensions: int) -> None:
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            HnswParameters,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )

        index = SearchIndex(
            name=self._index_name,
            fields=[
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SimpleField(name="content", type=SearchFieldDataType.String),
                SimpleField(name="source", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="chunk_index", type=SearchFieldDataType.Int32),
                SearchField(
                    name=VECTOR_FIELD,
                    type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True,
                    hidden=True,
                    vector_search_dimensions=dimensions,
                    vector_search_profile_name="default",
                ),
            ],
            vector_search=VectorSearch(
                algorithms=[HnswAlgorithmConfiguration(name="hnsw-cosine", parameters=HnswParameters(metric="cosine"))],
                profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="hnsw-cosine")],
            ),
        )
        _call_search(lambda: self._index_client.create_or_update_index(index))

    def list_ids(self) -> set[str]:
        return _call_search(lambda: {row["id"] for row in self._client.search(search_text="*", select=["id"])})

    def upsert(self, documents: list[dict]) -> None:
        results = _call_search(lambda: self._client.merge_or_upload_documents(documents=documents))
        if not all(result.succeeded for result in results):
            raise UpstreamError("일부 문서 업로드 실패")

    def delete(self, ids: list[str]) -> None:
        _call_search(lambda: self._client.delete_documents(documents=[{"id": value} for value in ids]))


class MemoryVectorStore:
    """로컬 개발용 저장소. 인덱싱과 API가 다른 프로세스이므로 파일로 공유한다."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._documents: dict[str, dict] = {}
        if self._path and self._path.exists():
            self._documents = json.loads(self._path.read_text(encoding="utf-8"))

    def search(self, vector: list[float], k: int) -> list[SearchHit]:
        scored = [(self._score(vector, doc[VECTOR_FIELD]), doc) for doc in self._documents.values()]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            SearchHit(chunk_id=doc["id"], source=doc["source"], content=doc["content"], score=score)
            for score, doc in scored[:k]
        ]

    @staticmethod
    def _score(a: list[float], b: list[float]) -> float:
        """Azure AI Search의 cosine 점수와 같은 척도로 계산한다: 1 / (1 + 코사인 거리)."""
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        cosine = sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0
        return 1 / (2 - cosine)

    def ensure_index(self, dimensions: int) -> None:
        return None

    def list_ids(self) -> set[str]:
        return set(self._documents)

    def upsert(self, documents: list[dict]) -> None:
        self._documents.update({doc["id"]: doc for doc in documents})
        self._save()

    def delete(self, ids: list[str]) -> None:
        for value in ids:
            self._documents.pop(value, None)
        self._save()

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._documents, ensure_ascii=False), encoding="utf-8")


def _local_index_path(settings: Settings) -> Path:
    return Path(settings.local_index_path or Path(__file__).resolve().parent.parent / "data" / "local_index.json")


def _build_azure(settings: Settings, *, ingest: bool, openai_http_client=None) -> Providers:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    credential = DefaultAzureCredential()
    base_url = settings.azure_openai_endpoint.rstrip("/") + "/openai/v1/"
    token_provider = get_bearer_token_provider(credential, OPENAI_SCOPE)

    def openai_client(timeout: float, retries: int) -> openai.OpenAI:
        return openai.OpenAI(
            base_url=base_url,
            api_key=token_provider,
            timeout=timeout,
            max_retries=retries,
            http_client=openai_http_client,
        )

    embedding_timeout = INGEST_TIMEOUT if ingest else EMBEDDING_TIMEOUT
    search_timeout = INGEST_TIMEOUT if ingest else SEARCH_TIMEOUT
    retries = INGEST_RETRIES if ingest else 0
    search_options = {"retry_total": retries, "connection_timeout": search_timeout, "read_timeout": search_timeout}

    client = SearchClient(settings.azure_search_endpoint, settings.azure_search_index, credential, **search_options)
    index_client = SearchIndexClient(settings.azure_search_endpoint, credential, **search_options) if ingest else None

    return Providers(
        embedder=OpenAIEmbedder(openai_client(embedding_timeout, retries), settings.azure_openai_embedding_deployment),
        chat=OpenAIChat(
            openai_client(CHAT_TIMEOUT, 0),
            settings.azure_openai_chat_deployment,
            {"max_completion_tokens": MAX_COMPLETION_TOKENS, "temperature": 0.0},
        ),
        store=AzureSearchStore(client, index_client, settings.azure_search_index),
    )


def _build_ollama(settings: Settings) -> Providers:
    client = openai.OpenAI(
        base_url=settings.ollama_base_url, api_key="ollama", timeout=OLLAMA_TIMEOUT, max_retries=0
    )
    return Providers(
        embedder=OpenAIEmbedder(client, settings.ollama_embedding_model),
        chat=OpenAIChat(client, settings.ollama_chat_model, {"temperature": 0.0, "max_tokens": MAX_COMPLETION_TOKENS}),
        store=MemoryVectorStore(_local_index_path(settings)),
    )


def build_providers(settings: Settings, *, ingest: bool = False, openai_http_client=None) -> Providers:
    if settings.provider == "ollama":
        return _build_ollama(settings)
    return _build_azure(settings, ingest=ingest, openai_http_client=openai_http_client)
