"""AC-PERF: 성능. PERF-02, 03은 배포 환경에서만 의미가 있어 SMOKE_BASE_URL이 없으면 건너뛴다."""

import math
import os
import time

import httpx
import pytest
import responses

from helpers import HEADERS, SEARCH_URL, Fakes, azure_client, openai_json, search_json

SAMPLES = 20
ANSWERED_QUESTION = os.environ.get("SMOKE_ANSWERED_QUESTION", "프로 요금제의 가격은 얼마인가요?")
NOT_FOUND_QUESTION = os.environ.get("SMOKE_NOT_FOUND_QUESTION", "엑셀에서 VLOOKUP 함수를 쓰는 법을 알려줘")

deployed = pytest.mark.skipif(not os.environ.get("SMOKE_BASE_URL"), reason="SMOKE_BASE_URL 없음: 4단계에서 실행")


def p95(values: list[float]) -> float:
    """최근접 순위법."""
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def measure_deployed(question: str, expected_status: str) -> float:
    base_url = os.environ["SMOKE_BASE_URL"].rstrip("/")
    headers = {"X-API-Key": os.environ["SMOKE_API_KEY"]}
    durations = []
    with httpx.Client(timeout=60) as client:
        client.post(f"{base_url}/ask", json={"question": question}, headers=headers)  # 예열 1회, 측정 제외
        for _ in range(SAMPLES):
            started = time.perf_counter()
            response = client.post(f"{base_url}/ask", json={"question": question}, headers=headers)
            durations.append(time.perf_counter() - started)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == expected_status
    return p95(durations)


def test_ac_perf_01_app_overhead_p95():
    fakes = Fakes()
    durations = []
    for _ in range(100):
        started = time.perf_counter()
        assert fakes.ask().status_code == 200
        durations.append(time.perf_counter() - started)
    assert p95(durations) <= 0.2


@pytest.mark.deployed
@deployed
def test_ac_perf_02_deployed_answered_p95():
    assert measure_deployed(ANSWERED_QUESTION, "answered") <= 8.0


@pytest.mark.deployed
@deployed
def test_ac_perf_03_deployed_not_found_p95():
    assert measure_deployed(NOT_FOUND_QUESTION, "not_found") <= 3.0


@responses.activate
def test_ac_perf_04_timeouts_and_no_retry(monkeypatch):
    responses.add(responses.POST, SEARCH_URL, json=search_json())
    timeouts = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts[request.url.path.rsplit("/", 1)[-1]] = request.extensions["timeout"]["read"]
        return openai_json(request)

    assert azure_client(monkeypatch, handler).post("/ask", json={"question": "q"}, headers=HEADERS).status_code == 200

    assert timeouts == {"embeddings": 5.0, "completions": 20.0}
    assert responses.calls[0].request.req_kwargs["timeout"] == (5.0, 5.0)

    # 재시도 0회: 일시적 오류에도 호출은 한 번뿐이다.
    for path in ("/embeddings", "/chat/completions"):
        calls = []

        def failing(request: httpx.Request, path=path, calls=calls) -> httpx.Response:
            if request.url.path.endswith(path):
                calls.append(1)
                return httpx.Response(503, json={"error": {}})
            return openai_json(request)

        azure_client(monkeypatch, failing).post("/ask", json={"question": "q"}, headers=HEADERS)
        assert len(calls) == 1, path

    responses.reset()
    responses.add(responses.POST, SEARCH_URL, status=503, json={"error": {"message": "x"}})
    azure_client(monkeypatch).post("/ask", json={"question": "q"}, headers=HEADERS)
    assert len(responses.calls) == 1
