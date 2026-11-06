"""data/docs의 문서를 청킹·임베딩해 벡터 인덱스에 올린다.

실행: python src/ingest.py [--docs 폴더]
성공하면 0, 실패하면 1로 끝난다.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from config import ConfigError, load_settings
from providers import (
    EMBEDDING_DIMENSIONS,
    VECTOR_FIELD,
    UpstreamBusy,
    UpstreamError,
    UpstreamTimeout,
    build_providers,
)

logger = logging.getLogger("ragbot.ingest")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
SUPPORTED_SUFFIXES = {".md", ".txt"}
EMBED_BATCH = 64
UPLOAD_BATCH = 500
DEFAULT_DOCS = Path(__file__).resolve().parent.parent / "data" / "docs"


class IngestError(Exception):
    pass


def _split_long(paragraph: str, size: int, overlap: int) -> list[str]:
    pieces = []
    start = 0
    while True:
        end = start + size
        pieces.append(paragraph[start:end])
        if end >= len(paragraph):
            return pieces
        start = end - overlap


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """문단을 먼저 묶고, 한 문단이 너무 길면 겹치게 자른다."""
    chunks: list[str] = []
    current = ""
    for paragraph in (part.strip() for part in text.replace("\r\n", "\n").split("\n\n")):
        if not paragraph:
            continue
        if len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(paragraph, size, overlap))
        elif current and len(current) + 2 + len(paragraph) > size:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def chunk_id(source: str, index: int) -> str:
    return hashlib.sha256(f"{source}\0{index}".encode()).hexdigest()[:32]


def load_chunks(docs_dir: Path) -> list[dict]:
    documents = []
    if not docs_dir.is_dir():
        return documents
    for path in sorted(p for p in docs_dir.rglob("*") if p.is_file()):
        source = path.relative_to(docs_dir).as_posix()
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            logger.warning("건너뜀(지원하지 않는 형식): %s", source)
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            logger.warning("건너뜀(UTF-8이 아님): %s", source)
            continue
        for index, content in enumerate(chunk_text(text)):
            documents.append({"id": chunk_id(source, index), "content": content, "source": source, "chunk_index": index})
    return documents


def ingest(docs_dir: Path, providers, dimensions: int) -> dict:
    documents = load_chunks(docs_dir)
    if not documents:
        raise IngestError(f"인덱싱할 문서가 없습니다: {docs_dir}")

    # 업로드 전에 전부 임베딩해, 차원이 틀리면 인덱스를 전혀 건드리지 않고 끝낸다.
    for start in range(0, len(documents), EMBED_BATCH):
        batch = documents[start : start + EMBED_BATCH]
        vectors = providers.embedder.embed([doc["content"] for doc in batch])
        for doc, vector in zip(batch, vectors, strict=True):
            if len(vector) != dimensions:
                raise IngestError(f"임베딩 차원이 {dimensions}이 아닙니다: {len(vector)}")
            doc[VECTOR_FIELD] = vector

    store = providers.store
    store.ensure_index(dimensions)
    for start in range(0, len(documents), UPLOAD_BATCH):
        store.upsert(documents[start : start + UPLOAD_BATCH])

    # 업로드가 끝난 뒤, 이번에 없는 청크(줄어든 문서, 삭제된 문서)를 지운다.
    stale = sorted(store.list_ids() - {doc["id"] for doc in documents})
    if stale:
        store.delete(stale)
    return {"chunks": len(documents), "deleted": len(stale)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="문서를 벡터 인덱스에 올립니다.")
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS, help="문서 폴더")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = load_settings(for_api=False)
        providers = build_providers(settings, ingest=True)
        result = ingest(args.docs, providers, EMBEDDING_DIMENSIONS[settings.provider])
    except (ConfigError, IngestError) as exc:
        logger.error("인덱싱 실패: %s", exc)
        return 1
    except UpstreamBusy:
        logger.error("인덱싱 실패: 외부 서비스가 혼잡합니다. 잠시 후 다시 실행하세요.")
        return 1
    except UpstreamTimeout:
        logger.error("인덱싱 실패: 외부 서비스 호출 시간이 초과되었습니다.")
        return 1
    except UpstreamError as exc:
        logger.error("인덱싱 실패: 외부 서비스 오류 (%s). 권한과 설정을 확인하세요.", exc)
        return 1
    logger.info("인덱싱 완료: 청크 %d개, 삭제 %d개", result["chunks"], result["deleted"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
