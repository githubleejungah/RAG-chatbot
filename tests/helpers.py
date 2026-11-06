"""테스트 공통 도구. 외부 호출은 두 층에서 가짜로 바꾼다.

- 가짜 제공자: 흐름과 API 동작을 시험한다.
- 가짜 HTTP: 실제 openai, azure SDK를 그대로 쓰고 네트워크만 막아, 요청 내용과 오류 변환을 시험한다.
"""

from __future__ import annotations

import json
import re

import httpx
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient

from api import create_app
from config import load_settings
from providers import MemoryVectorStore, Providers, SearchHit, build_providers

API_KEY = "unit-test-api-key-0123456789abcdef"
HEADERS = {"X-API-Key": API_KEY}
THRESHOLD = 0.625
CONTENT = "환불은 구매 후 14일 안에 신청할 수 있습니다."
QUOTE = "환불은 구매 후 14일 안에"
MODEL_ANSWER = "환불은 구매 후 14일 안에 하시면 됩니다."
SEARCH_URL = re.compile(r"https://search\.example/indexes.*")

ENV = {"LLM_PROVIDER": "ollama", "API_KEY": API_KEY, "MIN_SEARCH_SCORE": str(THRESHOLD)}
AZURE_ENV = {
    **ENV,
    "LLM_PROVIDER": "azure",
    "AZURE_OPENAI_ENDPOINT": "https://openai.example/",
    "AZURE_OPENAI_CHAT_DEPLOYMENT": "chat-deploy",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "embed-deploy",
    "AZURE_SEARCH_ENDPOINT": "https://search.example",
    "AZURE_SEARCH_INDEX": "docs",
}


def hit(chunk_id: str = "c1", score: float = 0.8, content: str = CONTENT, source: str = "policy.md") -> SearchHit:
    return SearchHit(chunk_id=chunk_id, source=source, content=content, score=score)


def model_output(chunk_id: str = "c1", quote: str = QUOTE, answer: str = MODEL_ANSWER, answerable: bool = True) -> str:
    return json.dumps({"answerable": answerable, "answer": answer, "citations": [{"chunk_id": chunk_id, "quote": quote}]})


class FakeEmbedder:
    def __init__(self, dimensions: int = 3, error: Exception | None = None) -> None:
        self.dimensions = dimensions
        self.error = error
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.error:
            raise self.error
        return [[0.1] * self.dimensions for _ in texts]


class FakeStore:
    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None) -> None:
        self.hits = hits or []
        self.error = error
        self.calls: list[tuple[list[float], int]] = []

    def search(self, vector: list[float], k: int) -> list[SearchHit]:
        self.calls.append((vector, k))
        if self.error:
            raise self.error
        return self.hits


class FakeChat:
    def __init__(self, output: str | None = None, error: Exception | None = None) -> None:
        self.output = model_output() if output is None else output
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.schemas: list[dict] = []

    def complete(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user))
        self.schemas.append(schema)
        if self.error:
            raise self.error
        return self.output


class SpyStore(MemoryVectorStore):
    """인덱싱 시험용. 실제 저장 동작을 유지하면서 호출을 기록한다."""

    def __init__(self) -> None:
        super().__init__(None)
        self.ensure_calls = 0
        self.upsert_calls = 0

    def ensure_index(self, dimensions: int) -> None:
        self.ensure_calls += 1

    def upsert(self, documents: list[dict]) -> None:
        self.upsert_calls += 1
        super().upsert(documents)


class Fakes:
    def __init__(self, hits=None, output=None, embedder=None, store=None, chat=None, env=None) -> None:
        self.embedder = embedder or FakeEmbedder()
        self.store = store or FakeStore([hit()] if hits is None else hits)
        self.chat = chat or FakeChat(output)
        self.client = TestClient(
            create_app(
                settings=load_settings(env or ENV),
                providers=Providers(embedder=self.embedder, chat=self.chat, store=self.store),
            )
        )

    def ask(self, **body):
        body.setdefault("question", "환불은 구매 후 며칠 안에 신청하나요?")
        return self.client.post("/ask", json=body, headers=HEADERS)

    @property
    def external_calls(self) -> int:
        return len(self.embedder.calls) + len(self.store.calls) + len(self.chat.calls)


class FakeCredential:
    """토큰만 돌려주는 자격 증명. 실제 Azure에 접속하지 않는다."""

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        return AccessToken("fake-token", 4102444800)


def openai_json(request: httpx.Request) -> httpx.Response:
    """정상 응답을 돌려주는 가짜 Azure OpenAI."""
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "m",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": model_output()},
                }
            ],
        },
    )


def search_json(score: float = 0.8) -> dict:
    return {"value": [{"@search.score": score, "id": "c1", "source": "policy.md", "content": CONTENT}]}


def azure_providers(monkeypatch, handler=openai_json, *, ingest: bool = False) -> Providers:
    """실제 SDK로 만든 Azure 제공자. HTTP만 가짜다."""
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", FakeCredential)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return build_providers(load_settings(AZURE_ENV), ingest=ingest, openai_http_client=http_client)


def azure_client(monkeypatch, handler=openai_json) -> TestClient:
    return TestClient(create_app(settings=load_settings(AZURE_ENV), providers=azure_providers(monkeypatch, handler)))
