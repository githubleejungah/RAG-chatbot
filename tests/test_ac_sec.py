"""AC-SEC: 보안."""

import logging
import re
from pathlib import Path

import httpx
import responses

from helpers import API_KEY, HEADERS, SEARCH_URL, FakeStore, Fakes, azure_client, hit, openai_json, search_json

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATTERNS = [
    re.compile(r"AccountKey=|SharedAccessKey=|-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"""(?i)(api[_-]?key|secret|password|token)["']?\s*[:=]\s*["'][A-Za-z0-9+/_\-]{16,}["']"""),
    re.compile(r"""["'][A-Za-z0-9+/]{40,}={0,2}["']"""),
]


def find_secrets(text: str) -> list[str]:
    return [match.group() for pattern in SECRET_PATTERNS for match in pattern.finditer(text)]


def test_ac_sec_01_no_secrets_in_source():
    # 검사기가 실제로 잡는지 먼저 확인한다.
    for sample in (
        'API_KEY = "abcdefghijklmnop1234"',
        "DefaultEndpointsProtocol=https;AccountKey=abc",
        '"Qk1Xc2VjcmV0S2V5VmFsdWVGb3JUZXN0aW5nT25seTEyMzQ1Njc4"',
    ):
        assert find_secrets(sample), sample

    files = [p for folder in ("src", "infra") for p in (ROOT / folder).rglob("*") if p.is_file()]
    files = [p for p in files if "__pycache__" not in p.parts]
    assert any(p.name == "api.py" for p in files) and any(p.name == "resources.bicep" for p in files)
    violations = {str(p): found for p in files if (found := find_secrets(p.read_text(encoding="utf-8")))}
    assert violations == {}


@responses.activate
def test_ac_sec_02_azure_clients_are_keyless(monkeypatch):
    responses.add(responses.POST, SEARCH_URL, json=search_json())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return openai_json(request)

    assert azure_client(monkeypatch, handler).post("/ask", json={"question": "q"}, headers=HEADERS).status_code == 200

    # 임베딩, 채팅, 검색 모두 토큰으로 호출하고 키 헤더는 보내지 않는다.
    assert len(seen) == 2
    all_headers = [*seen, responses.calls[0].request.headers]
    for headers in all_headers:
        assert headers["Authorization"] == "Bearer fake-token"
        assert "api-key" not in {name.lower() for name in headers}


@responses.activate
def test_ac_sec_03_logs_exclude_key_and_question(monkeypatch, caplog):
    question = "로그에남으면안되는질문문장"
    responses.add(responses.POST, SEARCH_URL, json=search_json())

    with caplog.at_level(logging.INFO):
        # 정상, 입력 오류, 인증 오류, 내부 오류, 실제 SDK 경로를 모두 지난다.
        fakes = Fakes()
        fakes.ask(question=question)
        fakes.ask(question=question, top_k=99)
        fakes.client.post("/ask", json={"question": question}, headers={"X-API-Key": "틀린키".encode().hex()})
        Fakes(store=FakeStore(error=RuntimeError(f"질문 포함 예외 {question}"))).ask(question=question)
        Fakes(hits=[hit("c1", 0.1)]).ask(question=question)
        azure_client(monkeypatch).post("/ask", json={"question": question}, headers=HEADERS)

    assert "request_id=" in caplog.text
    assert question not in caplog.text
    assert API_KEY not in caplog.text
