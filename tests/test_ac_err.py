"""AC-ERR: 오류 처리. 가짜 제공자와 실제 SDK(가짜 HTTP) 양쪽에서 확인한다."""

import json

import httpx
import requests
import responses

from helpers import (
    HEADERS,
    SEARCH_URL,
    FakeChat,
    FakeEmbedder,
    Fakes,
    FakeStore,
    azure_client,
    openai_json,
    search_json,
)
from providers import UpstreamBusy, UpstreamError, UpstreamTimeout

ASK = {"question": "환불 기간은?"}


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def post_raw(fakes, content, headers=HEADERS):
    return fakes.client.post("/ask", content=content, headers={**headers, "Content-Type": "application/json"})


def failing(path_suffix, respond):
    """지정한 OpenAI 경로만 실패시키는 처리기를 만들고, 호출 횟수를 기록한다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(path_suffix):
            calls.append(request.url.path)
            return respond(request)
        return openai_json(request)

    return handler, calls


def test_ac_err_01_invalid_question():
    fakes = Fakes()
    bodies = [b"{not json", b"{}", b'{"question": 123}', b'{"question": null}', b'{"question": "   "}', b'["q"]']
    for body in bodies:
        assert_error(post_raw(fakes, body), 422, "INVALID_REQUEST")
    assert fakes.external_calls == 0


def test_ac_err_02_question_length_boundary():
    fakes = Fakes()
    assert_error(fakes.ask(question="가" * 1001), 422, "INVALID_REQUEST")
    assert fakes.external_calls == 0

    assert fakes.ask(question="가" * 1000).status_code == 200
    # 길이는 앞뒤 공백을 뺀 뒤 잰다.
    assert fakes.ask(question="  " + "가" * 1000 + "  ").status_code == 200


def test_ac_err_03_top_k_out_of_range():
    fakes = Fakes()
    for value in (0, 11, -1, "3", 1.5, True, None):
        assert_error(fakes.ask(top_k=value), 422, "INVALID_REQUEST")
    assert fakes.external_calls == 0
    for value in (1, 10):
        assert fakes.ask(top_k=value).status_code == 200


@responses.activate
def test_ac_err_05_upstream_429_returns_503(monkeypatch):
    # 어느 단계의 혼잡이든 같은 응답이다.
    for fakes in (
        Fakes(embedder=FakeEmbedder(error=UpstreamBusy("9"))),
        Fakes(store=FakeStore(error=UpstreamBusy("9"))),
        Fakes(chat=FakeChat(error=UpstreamBusy("9"))),
    ):
        response = fakes.ask()
        assert_error(response, 503, "UPSTREAM_BUSY")
        assert response.headers["Retry-After"] == "9"

    # 실제 SDK: OpenAI 429. 재시도 없이 1회만 호출하고 Retry-After를 전달한다.
    responses.add(responses.POST, SEARCH_URL, json=search_json())
    for suffix in ("/embeddings", "/chat/completions"):
        handler, calls = failing(suffix, lambda _r: httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {}}))
        response = azure_client(monkeypatch, handler).post("/ask", json=ASK, headers=HEADERS)
        assert_error(response, 503, "UPSTREAM_BUSY")
        assert response.headers["Retry-After"] == "7"
        assert len(calls) == 1, suffix

    # 실제 SDK: AI Search 429.
    responses.reset()
    responses.add(responses.POST, SEARCH_URL, status=429, json={"error": {"message": "busy"}})
    response = azure_client(monkeypatch).post("/ask", json=ASK, headers=HEADERS)
    assert_error(response, 503, "UPSTREAM_BUSY")
    assert "Retry-After" in response.headers
    assert len(responses.calls) == 1


@responses.activate
def test_ac_err_06_upstream_timeout_returns_504(monkeypatch):
    for fakes in (
        Fakes(embedder=FakeEmbedder(error=UpstreamTimeout())),
        Fakes(store=FakeStore(error=UpstreamTimeout())),
        Fakes(chat=FakeChat(error=UpstreamTimeout())),
    ):
        assert_error(fakes.ask(), 504, "UPSTREAM_TIMEOUT")

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    responses.add(responses.POST, SEARCH_URL, json=search_json())
    for suffix in ("/embeddings", "/chat/completions"):
        handler, calls = failing(suffix, timeout)
        assert_error(azure_client(monkeypatch, handler).post("/ask", json=ASK, headers=HEADERS), 504, "UPSTREAM_TIMEOUT")
        assert len(calls) == 1, suffix

    for error in (requests.exceptions.ConnectTimeout(), requests.exceptions.ReadTimeout()):
        responses.reset()
        responses.add(responses.POST, SEARCH_URL, body=error)
        assert_error(azure_client(monkeypatch).post("/ask", json=ASK, headers=HEADERS), 504, "UPSTREAM_TIMEOUT")
        assert len(responses.calls) == 1


@responses.activate
def test_ac_err_07_upstream_failure_returns_502(monkeypatch):
    for fakes in (
        Fakes(embedder=FakeEmbedder(error=UpstreamError())),
        Fakes(store=FakeStore(error=UpstreamError())),
        Fakes(chat=FakeChat(error=UpstreamError())),
    ):
        assert_error(fakes.ask(), 502, "UPSTREAM_ERROR")

    def connection_error(request):
        raise httpx.ConnectError("refused", request=request)

    responses.add(responses.POST, SEARCH_URL, json=search_json())
    for suffix in ("/embeddings", "/chat/completions"):
        for respond in (
            lambda _r: httpx.Response(500, json={"error": {"message": "내부 정보"}}),
            lambda _r: httpx.Response(401, json={"error": {"message": "내부 정보"}}),
            connection_error,
        ):
            handler, calls = failing(suffix, respond)
            response = azure_client(monkeypatch, handler).post("/ask", json=ASK, headers=HEADERS)
            assert_error(response, 502, "UPSTREAM_ERROR")
            assert "내부 정보" not in response.text
            assert len(calls) == 1, suffix

    for status in (500, 403):
        responses.reset()
        responses.add(responses.POST, SEARCH_URL, status=status, json={"error": {"message": "내부 정보"}})
        response = azure_client(monkeypatch).post("/ask", json=ASK, headers=HEADERS)
        assert_error(response, 502, "UPSTREAM_ERROR")
        assert "내부 정보" not in response.text


def test_ac_err_08_invalid_model_output():
    marker = "모델원문표시"
    outputs = [
        "",
        "   ",
        f"{marker} JSON이 아닌 답변",
        f"```json\n{json.dumps({'answerable': True, 'answer': marker, 'citations': []})}\n```",
        json.dumps({"answer": marker}),
        json.dumps({"answerable": "true", "answer": marker, "citations": []}),
        json.dumps({"answerable": True, "answer": marker, "citations": "c1"}),
        json.dumps({"answerable": True, "answer": "  ", "citations": []}),
        json.dumps([marker]),
    ]
    for output in outputs:
        fakes = Fakes(chat=FakeChat(output))
        response = fakes.ask()
        assert_error(response, 502, "MODEL_OUTPUT_INVALID")
        assert marker not in response.text


def test_ac_err_09_unexpected_error_hides_details():
    fakes = Fakes(store=FakeStore(error=RuntimeError("내부비밀 연결 정보")))
    # TestClient는 앱이 예외를 다시 던지면 실패한다. 응답으로 끝나는지도 함께 확인한다.
    response = fakes.ask()
    assert_error(response, 500, "INTERNAL_ERROR")
    assert "내부비밀" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_ac_err_10_error_envelope():
    fakes = Fakes()
    responses_to_check = [
        fakes.client.post("/ask", json=ASK),  # 401
        fakes.client.get("/없는경로"),  # 404
        fakes.client.get("/ask"),  # 405
        post_raw(fakes, b"{"),  # 422
        Fakes(store=FakeStore(error=RuntimeError("x"))).ask(),  # 500
        Fakes(store=FakeStore(error=UpstreamError())).ask(),  # 502
        Fakes(chat=FakeChat("이상한 출력")).ask(),  # 502
        Fakes(store=FakeStore(error=UpstreamBusy())).ask(),  # 503
        Fakes(store=FakeStore(error=UpstreamTimeout())).ask(),  # 504
    ]
    assert {r.status_code for r in responses_to_check} == {401, 404, 405, 422, 500, 502, 503, 504}
    for response in responses_to_check:
        body = response.json()
        assert set(body) == {"error"}, response.text
        assert set(body["error"]) == {"code", "message", "request_id"}
        assert all(isinstance(value, str) and value for value in body["error"].values())
