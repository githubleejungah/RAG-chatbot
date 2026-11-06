"""FastAPI 앱 조립: 라우트, 인증, 오류 처리. 응답 형식은 design/architecture.md 5절을 따른다."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

import rag
from config import Settings, load_settings
from providers import Providers, UpstreamBusy, UpstreamError, UpstreamTimeout, build_providers

logger = logging.getLogger("ragbot")

MAX_QUESTION_CHARS = 1000


class AskRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    question: str
    top_k: int = Field(default=rag.DEFAULT_TOP_K, ge=1, le=10)

    @field_validator("question")
    @classmethod
    def _check_question(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= MAX_QUESTION_CHARS:
            raise ValueError("question length")
        return value


def _error(request: Request, status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers=headers,
    )


def create_app(
    settings: Settings | None = None,
    providers: Providers | None = None,
    env: Mapping[str, str] | None = None,
) -> FastAPI:
    """앱을 만든다. 설정이 잘못되면 ConfigError로 시작에 실패한다."""
    settings = settings or load_settings(env)
    providers = providers or build_providers(settings)

    app = FastAPI(title="문서 Q&A RAG 챗봇", docs_url=None, redoc_url=None, openapi_url=None)

    def _valid_key(request: Request) -> bool:
        supplied = request.headers.get("x-api-key", "").encode()
        return secrets.compare_digest(supplied, settings.api_key.encode())

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            # FastAPI는 의존성보다 JSON 해석을 먼저 하므로, 인증은 본문을 읽기 전인 여기서 한다.
            if request.method == "POST" and request.url.path == "/ask" and not _valid_key(request):
                response = _error(request, 401, "UNAUTHORIZED", "API 키가 없거나 올바르지 않습니다.")
            else:
                response = await call_next(request)
        except Exception as exc:
            # 여기서 끝내 서버 로그에 예외 메시지가 남지 않게 한다. 메시지에 질문이나 설정 값이 있을 수 있다.
            logger.error("request_id=%s unexpected_error=%s", request.state.request_id, type(exc).__name__)
            response = _error(request, 500, "INTERNAL_ERROR", "내부 오류가 발생했습니다.")
        # 질문 원문과 API 키는 남기지 않는다.
        logger.info(
            "request_id=%s path=%s status=%s latency_ms=%.1f top_score=%s steps=%s",
            request.state.request_id,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            getattr(request.state, "top_score", None),
            ",".join(getattr(request.state, "steps", [])) or None,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def _on_invalid(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(request, 422, "INVALID_REQUEST", "요청 형식이 올바르지 않습니다.")

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(request, exc.status_code, code, "요청을 처리할 수 없습니다.")

    @app.exception_handler(UpstreamBusy)
    async def _on_busy(request: Request, exc: UpstreamBusy) -> JSONResponse:
        return _error(
            request, 503, "UPSTREAM_BUSY", "서비스가 혼잡합니다. 잠시 후 다시 시도하세요.", {"Retry-After": exc.retry_after}
        )

    @app.exception_handler(UpstreamTimeout)
    async def _on_timeout(request: Request, _exc: UpstreamTimeout) -> JSONResponse:
        return _error(request, 504, "UPSTREAM_TIMEOUT", "요청 시간이 초과되었습니다.")

    @app.exception_handler(UpstreamError)
    async def _on_upstream(request: Request, _exc: UpstreamError) -> JSONResponse:
        return _error(request, 502, "UPSTREAM_ERROR", "외부 서비스 호출에 실패했습니다.")

    @app.exception_handler(rag.ModelOutputInvalid)
    async def _on_model_output(request: Request, _exc: rag.ModelOutputInvalid) -> JSONResponse:
        request.state.steps = ["model_output_invalid"]
        return _error(request, 502, "MODEL_OUTPUT_INVALID", "모델 응답을 처리할 수 없습니다.")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/ask")
    def ask(body: AskRequest, request: Request) -> dict:
        result = rag.ask(
            body.question,
            body.top_k,
            embedder=providers.embedder,
            store=providers.store,
            chat=providers.chat,
            min_score=settings.min_search_score,
        )
        request.state.top_score = result.top_score
        request.state.steps = result.steps
        return {
            "status": result.status,
            "answer": result.answer,
            "sources": result.sources,
            "retrieved": result.retrieved,
            "top_score": result.top_score,
            "request_id": request.state.request_id,
        }

    return app
