"""AC-CFG: 설정."""

import pytest

from api import create_app
from config import ConfigError, load_settings
from helpers import AZURE_ENV, ENV, HEADERS, Fakes, hit
from providers import MemoryVectorStore, build_providers


def test_ac_cfg_01_invalid_provider_fails_startup():
    with pytest.raises(ConfigError) as error:
        create_app(env={**ENV, "LLM_PROVIDER": "openai"})
    assert "azure" in str(error.value) and "ollama" in str(error.value)


def test_ac_cfg_02_ollama_provider_avoids_azure(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("ollama 모드에서 Azure 클라이언트를 만들면 안 된다")

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", forbidden)
    monkeypatch.setattr("azure.search.documents.SearchClient", forbidden)

    settings = load_settings({**ENV, "LOCAL_INDEX_PATH": str(tmp_path / "index.json")})
    providers = build_providers(settings)

    assert str(providers.embedder._client.base_url) == "http://localhost:11434/v1/"
    assert str(providers.chat._client.base_url) == "http://localhost:11434/v1/"
    assert isinstance(providers.store, MemoryVectorStore)

    # 메모리 저장소는 Azure와 같은 척도로 점수를 매기고, 다른 프로세스가 읽을 수 있게 파일로 남긴다.
    documents = [
        {"id": "same", "source": "a", "content": "x", "content_vector": [1.0, 0.0]},
        {"id": "orthogonal", "source": "a", "content": "y", "content_vector": [0.0, 1.0]},
    ]
    providers.store.upsert(documents)
    reloaded = MemoryVectorStore(tmp_path / "index.json")
    hits = reloaded.search([1.0, 0.0], 2)
    assert [(h.chunk_id, round(h.score, 6)) for h in hits] == [("same", 1.0), ("orthogonal", 0.5)]


def test_ac_cfg_03_missing_env_fails_without_values():
    secret_value = "설정값이노출되면안됨"
    env = {**AZURE_ENV, "API_KEY": secret_value}
    del env["AZURE_SEARCH_ENDPOINT"]
    del env["AZURE_OPENAI_CHAT_DEPLOYMENT"]

    with pytest.raises(ConfigError) as error:
        create_app(env=env)

    message = str(error.value)
    assert "AZURE_SEARCH_ENDPOINT" in message
    assert "AZURE_OPENAI_CHAT_DEPLOYMENT" in message
    assert "API_KEY" not in message
    assert secret_value not in message

    for name in ("LLM_PROVIDER", "API_KEY", "MIN_SEARCH_SCORE"):
        env = {key: value for key, value in ENV.items() if key != name}
        with pytest.raises(ConfigError, match=name):
            create_app(env=env)


def test_ac_cfg_04_health_without_auth():
    fakes = Fakes()
    response = fakes.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert fakes.external_calls == 0


def test_ac_cfg_05_threshold_from_env():
    hits = [hit("c1", 0.7)]
    low = Fakes(hits=hits, env={**ENV, "MIN_SEARCH_SCORE": "0.6"}).ask().json()
    high = Fakes(hits=hits, env={**ENV, "MIN_SEARCH_SCORE": "0.8"}).ask().json()
    assert low["status"] == "answered"
    assert high["status"] == "not_found"

    for bad in ("abc", "0", "1.5", "-0.1"):
        with pytest.raises(ConfigError, match="MIN_SEARCH_SCORE"):
            load_settings({**ENV, "MIN_SEARCH_SCORE": bad})
