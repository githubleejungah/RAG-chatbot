"""AC-ING: 인덱싱."""

import logging

import pytest

import ingest
from helpers import FakeEmbedder, SpyStore
from providers import Providers


def providers_with(store=None, dimensions=4):
    return Providers(embedder=FakeEmbedder(dimensions), chat=None, store=store or SpyStore())


def run_main(monkeypatch, docs, providers):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(ingest, "load_dotenv", lambda: None)
    monkeypatch.setattr(ingest, "build_providers", lambda *_a, **_k: providers)
    return ingest.main(["--docs", str(docs)])


def test_ac_ing_01_chunk_size_overlap_and_coverage():
    unique = "".join(chr(0xAC00 + i) for i in range(2000))  # 글자가 모두 달라 위치를 정확히 비교할 수 있다.
    short = "짧은 첫 문단입니다."
    chunks = ingest.chunk_text(f"{short}\n\n{unique}\n\n마지막 문단입니다.\n")

    assert all(len(chunk) <= 800 for chunk in chunks)
    assert chunks[0] == short

    parts = chunks[1:-1]
    assert len(parts) > 1
    for left, right in zip(parts, parts[1:]):
        assert left[-120:] == right[:120], "문단 안에서 잘린 청크는 120자 겹쳐야 한다"

    # 겹침을 빼고 이으면 원문이 그대로 나와야 한다.
    rebuilt = parts[0] + "".join(part[120:] for part in parts[1:])
    assert rebuilt == unique
    assert chunks[-1] == "마지막 문단입니다."

    # 짧은 문단은 합치고, 공백을 제외한 글자를 잃지 않는다.
    text = "\n\n".join(f"{i}번 문단 내용입니다. " * 10 for i in range(20))
    merged = ingest.chunk_text(text)
    assert all(len(chunk) <= 800 for chunk in merged)
    assert "".join("".join(merged).split()) == "".join(text.split())


def test_ac_ing_02_reingest_is_idempotent(tmp_path):
    (tmp_path / "a.md").write_text("첫 문단\n\n" + "가" * 2000, encoding="utf-8")
    store = SpyStore()
    providers = providers_with(store)

    ingest.ingest(tmp_path, providers, 4)
    first_ids = store.list_ids()
    result = ingest.ingest(tmp_path, providers, 4)

    assert first_ids and store.list_ids() == first_ids
    assert result == {"chunks": len(first_ids), "deleted": 0}


def test_ac_ing_03_wrong_dimension_aborts(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("내용", encoding="utf-8")
    store = SpyStore()
    # ollama 기대 차원은 1024다. 맞으면 성공하고 틀리면 실패해야 한다.
    assert run_main(monkeypatch, tmp_path, providers_with(store, dimensions=1536)) != 0
    assert store.upsert_calls == 0
    assert store.ensure_calls == 0

    assert run_main(monkeypatch, tmp_path, providers_with(store, dimensions=1024)) == 0
    assert store.upsert_calls == 1


def test_ac_ing_04_no_documents_aborts(tmp_path, monkeypatch):
    store = SpyStore()
    store.upsert([{"id": "기존", "source": "x", "content": "y", "content_vector": [1.0]}])
    calls_before = store.upsert_calls

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "무시.pdf").write_bytes(b"%PDF")
    for docs in (empty, tmp_path / "없는폴더"):
        assert run_main(monkeypatch, docs, providers_with(store)) != 0

    assert store.list_ids() == {"기존"}
    assert store.upsert_calls == calls_before


def test_ac_ing_05_skips_unsupported_files(tmp_path, caplog):
    (tmp_path / "a.md").write_text("마크다운", encoding="utf-8")
    (tmp_path / "b.txt").write_text("텍스트", encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.docx").write_bytes(b"PK")

    with caplog.at_level(logging.WARNING, logger="ragbot.ingest"):
        documents = ingest.load_chunks(tmp_path)

    assert {doc["source"] for doc in documents} == {"a.md", "b.txt"}
    assert "c.pdf" in caplog.text
    assert "sub/d.docx" in caplog.text


def test_ac_ing_06_removes_stale_chunks(tmp_path):
    long_doc = tmp_path / "a.md"
    other = tmp_path / "b.md"
    long_doc.write_text("가" * 3000, encoding="utf-8")
    other.write_text("다른 문서", encoding="utf-8")
    store = SpyStore()
    providers = providers_with(store)

    ingest.ingest(tmp_path, providers, 4)
    before = len(store.list_ids())

    # 문서가 짧아지면 남은 이전 청크를 지운다.
    long_doc.write_text("짧아진 문서", encoding="utf-8")
    result = ingest.ingest(tmp_path, providers, 4)
    assert result["deleted"] == before - 2
    assert len(store.list_ids()) == 2
    assert ingest.chunk_id("a.md", 1) not in store.list_ids()

    # 파일이 삭제되면 그 문서의 청크도 지운다.
    other.unlink()
    ingest.ingest(tmp_path, providers, 4)
    assert store.list_ids() == {ingest.chunk_id("a.md", 0)}


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch):
    for name in ("API_KEY", "MIN_SEARCH_SCORE"):
        monkeypatch.delenv(name, raising=False)
