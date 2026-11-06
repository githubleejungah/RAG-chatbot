"""AC에 직접 대응하지 않는 보강 시험. ingest가 실제 Azure SDK로 보내는 요청을 HTTP 층에서 확인한다."""

import json
import re

import httpx
import responses

import ingest

INDEX_URL = re.compile(r"https://search\.example/indexes.*")
DOCS_INDEX_URL = re.compile(r"https://search\.example/indexes.*/docs/search\.index.*")
DOCS_SEARCH_URL = re.compile(r"https://search\.example/indexes.*/docs/search\.post\.search.*")


def embeddings(request: httpx.Request) -> httpx.Response:
    count = len(json.loads(request.content)["input"])
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "m",
            "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2]} for i in range(count)],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


def index_result(request):
    keys = [row["id"] for row in json.loads(request.body)["value"]]
    body = {"value": [{"key": key, "status": True, "statusCode": 200} for key in keys]}
    return 200, {"Content-Type": "application/json"}, json.dumps(body)


@responses.activate
def test_ingest_requests_through_real_sdk(monkeypatch, tmp_path):
    from helpers import azure_providers

    (tmp_path / "a.md").write_text("문서 내용", encoding="utf-8")
    keep = ingest.chunk_id("a.md", 0)

    responses.add_callback(responses.POST, DOCS_INDEX_URL, callback=index_result)
    responses.add(
        responses.POST,
        DOCS_SEARCH_URL,
        json={"value": [{"@search.score": 1.0, "id": keep}, {"@search.score": 1.0, "id": "stale-id"}]},
    )
    responses.add(responses.PUT, INDEX_URL, json={"name": "docs", "fields": []}, status=201)

    providers = azure_providers(monkeypatch, embeddings, ingest=True)
    result = ingest.ingest(tmp_path, providers, 2)
    assert result == {"chunks": 1, "deleted": 1}

    # 인덱스 정의
    put = next(call for call in responses.calls if call.request.method == "PUT")
    definition = json.loads(put.request.body)
    fields = {field["name"]: field for field in definition["fields"]}
    assert set(fields) == {"id", "content", "source", "chunk_index", "content_vector"}
    assert fields["id"]["key"] is True
    assert fields["source"]["filterable"] is True
    vector = fields["content_vector"]
    assert vector["type"] == "Collection(Edm.Single)"
    assert vector["dimensions"] == 2
    assert vector["searchable"] is True
    assert vector["retrievable"] is False
    assert definition["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"
    assert definition["vectorSearch"]["profiles"][0]["name"] == vector["vectorSearchProfile"]

    # 업로드와 삭제
    batches = [
        json.loads(call.request.body)["value"]
        for call in responses.calls
        if "search.index" in call.request.url
    ]
    assert batches[0][0]["@search.action"] == "mergeOrUpload"
    assert batches[0][0]["id"] == keep
    assert batches[0][0]["content_vector"] == [0.1, 0.2]
    assert batches[0][0]["source"] == "a.md"
    assert batches[1] == [{"@search.action": "delete", "id": "stale-id"}]

    # 모든 요청은 토큰 인증이다.
    assert all(call.request.headers["Authorization"] == "Bearer fake-token" for call in responses.calls)
