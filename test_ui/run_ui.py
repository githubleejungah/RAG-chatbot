"""로컬 테스트 UI. Azure 없이 실제 앱 코드(src/)를 Ollama 모드로 띄우고 화면을 붙인다.

실행: python test_ui/run_ui.py [--port 8765] [--no-browser]
이 폴더는 azure.yaml의 배포 대상(src/)이 아니므로 App Service에 올라가지 않는다.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from api import create_app  # noqa: E402
from config import load_settings  # noqa: E402
from ingest import DEFAULT_DOCS, IngestError, ingest  # noqa: E402
from providers import EMBEDDING_DIMENSIONS, UpstreamBusy, UpstreamError, UpstreamTimeout, build_providers  # noqa: E402

# bge-m3 + llama3.2로 보정한 값. design/decisions.md D-07. Azure 모델의 임계치(0.5735)와 척도가 다르다.
LOCAL_MIN_SEARCH_SCORE = "0.6411"
EXPECTED = {"answerable": "answered", "off_topic": "not_found", "in_domain": "not_found"}
CATEGORY_LABELS = {"answerable": "문서에 답이 있음", "off_topic": "주제 밖", "in_domain": "같은 주제지만 답이 없음"}


def load_samples() -> list[dict]:
    """보정에 쓰지 않은 검증 질문을 예시로 쓴다."""
    questions = json.loads((ROOT / "data" / "calibration_questions.json").read_text(encoding="utf-8"))["validation"]
    return [
        {"group": CATEGORY_LABELS[category], "question": question, "expected": EXPECTED[category]}
        for category, items in questions.items()
        for question in items
    ]


def build_app():
    # 실행할 때마다 새로 만드는 임시 키다. .env를 읽지 않고, 어디에도 저장하지 않는다.
    api_key = secrets.token_urlsafe(32)
    env = {"LLM_PROVIDER": "ollama", "API_KEY": api_key, "MIN_SEARCH_SCORE": LOCAL_MIN_SEARCH_SCORE}
    settings = load_settings(env)

    print("문서를 인덱싱하는 중입니다...", flush=True)
    result = ingest(DEFAULT_DOCS, build_providers(settings, ingest=True), EMBEDDING_DIMENSIONS[settings.provider])
    print(f"인덱싱 완료: 청크 {result['chunks']}개, 삭제 {result['deleted']}개", flush=True)

    # 인덱스 파일이 갱신된 뒤에 저장소를 읽도록 인덱싱 다음에 앱을 만든다.
    app = create_app(settings=settings, providers=build_providers(settings))
    page = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/ui/config")
    def ui_config() -> dict:
        return {
            "api_key": api_key,
            "min_score": settings.min_search_score,
            "chat_model": settings.ollama_chat_model,
            "embedding_model": settings.ollama_embedding_model,
            "samples": load_samples(),
        }

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 챗봇 로컬 테스트 UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않습니다.")
    args = parser.parse_args()

    try:
        app = build_app()
    except (UpstreamError, UpstreamTimeout, UpstreamBusy):
        print("시작 실패: Ollama에 연결하지 못했습니다. Ollama 실행 여부와 bge-m3 모델을 확인하세요.", file=sys.stderr)
        return 1
    except IngestError as exc:
        print(f"시작 실패: {exc}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"테스트 UI: {url}  (끝내려면 Ctrl+C)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    # 키를 화면에 내려 주는 페이지이므로 이 PC 안에서만 접속할 수 있게 한다.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
