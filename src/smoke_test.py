"""배포된 엔드포인트의 스모크 테스트. 답 있는 질문 2개 + 답 없는 질문 1개를 실제로 보낸다.

실행: python src/smoke_test.py [--base-url URL] [--output 파일]
환경 변수: SMOKE_BASE_URL, SMOKE_API_KEY (없으면 APP_API_KEY)
결과를 Markdown으로 저장한다. 모두 통과하면 0, 아니면 1로 끝난다. API 키는 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

NOT_FOUND = "문서에서 찾지 못함"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "4_smoke_result.md"

# 기대값은 data/docs의 원문에서 가져왔다. 표기 차이를 줄이려 쉼표와 공백을 빼고 비교한다.
ANSWERED_CASES = (
    ("프로 요금제의 가격은 얼마인가요?", "9900", "faq.md"),
    ("연간 결제는 며칠 안에 환불되나요?", "30일", "refund_policy.md"),
)
NOT_FOUND_QUESTION = "엑셀에서 VLOOKUP 함수를 쓰는 법을 알려줘"


@dataclass
class Check:
    name: str
    passed: bool = False
    seconds: float | None = None
    status_code: int | None = None
    status: str = ""
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    top_score: float | None = None
    problems: list[str] = field(default_factory=list)


def _squeeze(text: str) -> str:
    return "".join(text.split()).replace(",", "")


def _ask(client: httpx.Client, api_key: str, question: str) -> tuple[Check, dict]:
    check = Check(name=question)
    started = time.perf_counter()
    try:
        response = client.post("/ask", json={"question": question}, headers={"X-API-Key": api_key})
    except httpx.HTTPError as exc:
        check.problems.append(f"연결 실패: {type(exc).__name__}")
        return check, {}
    check.seconds = time.perf_counter() - started
    check.status_code = response.status_code
    try:
        body = response.json()
    except ValueError:
        check.problems.append("응답이 JSON이 아님")
        return check, {}
    if response.status_code != 200:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        check.problems.append(f"오류 응답: {error.get('code', '알 수 없음')}")
        return check, {}
    check.status = body.get("status", "")
    check.answer = body.get("answer", "")
    check.top_score = body.get("top_score")
    check.sources = [row.get("source", "") for row in body.get("sources", [])]
    return check, body


def check_health(client: httpx.Client) -> Check:
    check = Check(name="GET /health")
    started = time.perf_counter()
    try:
        response = client.get("/health")
    except httpx.HTTPError as exc:
        check.problems.append(f"연결 실패: {type(exc).__name__}")
        return check
    check.seconds = time.perf_counter() - started
    check.status_code = response.status_code
    check.passed = response.status_code == 200
    if not check.passed:
        check.problems.append("HTTP 200이 아님")
    return check


def check_auth(client: httpx.Client) -> Check:
    check = Check(name="API 키 없는 POST /ask")
    started = time.perf_counter()
    try:
        response = client.post("/ask", json={"question": "인증 확인"})
    except httpx.HTTPError as exc:
        check.problems.append(f"연결 실패: {type(exc).__name__}")
        return check
    check.seconds = time.perf_counter() - started
    check.status_code = response.status_code
    check.passed = response.status_code == 401
    if not check.passed:
        check.problems.append("HTTP 401이 아님: 키 없이 호출할 수 있으면 비용이 노출된다")
    return check


def check_answered(client: httpx.Client, api_key: str, question: str, keyword: str, source: str) -> Check:
    check, body = _ask(client, api_key, question)
    if not body:
        return check
    if check.status != "answered":
        check.problems.append(f"status가 answered가 아님: {check.status}")
    if keyword not in _squeeze(check.answer):
        check.problems.append(f"답변에 기대값 없음: {keyword}")
    if source not in check.sources:
        check.problems.append(f"기대한 출처 없음: {source}")
    if not all(row.get("quote") for row in body.get("sources", [])) or not body.get("sources"):
        check.problems.append("근거 인용문 없음")
    check.passed = not check.problems
    return check


def check_not_found(client: httpx.Client, api_key: str, question: str) -> Check:
    check, body = _ask(client, api_key, question)
    if not body:
        return check
    if check.status != "not_found":
        check.problems.append(f"status가 not_found가 아님: {check.status}")
    if check.answer != NOT_FOUND:
        check.problems.append("찾지 못함 문구가 아님")
    if body.get("sources") or body.get("retrieved"):
        check.problems.append("찾지 못함 응답에 근거가 포함됨")
    check.passed = not check.problems
    return check


def run_checks(client: httpx.Client, api_key: str) -> list[Check]:
    checks = [check_health(client), check_auth(client)]
    checks += [check_answered(client, api_key, *case) for case in ANSWERED_CASES]
    checks.append(check_not_found(client, api_key, NOT_FOUND_QUESTION))
    return checks


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(checks: list[Check], base_url: str, now: datetime) -> str:
    passed = sum(check.passed for check in checks)
    verdict = "통과" if passed == len(checks) else "실패"
    lines = [
        "# 4단계 스모크 테스트 결과",
        "",
        f"- **판정: {verdict} ({passed}/{len(checks)})**",
        f"- 실행 시각: {now.isoformat(timespec='seconds')}",
        f"- 대상: {base_url}",
        "",
        "| 확인 항목 | 판정 | HTTP | 상태 | 시간(초) | 최고 점수 | 출처 |",
        "|---|---|---|---|---|---|---|",
    ]
    for check in checks:
        seconds = "-" if check.seconds is None else f"{check.seconds:.2f}"
        score = "-" if check.top_score is None else f"{check.top_score:.3f}"
        lines.append(
            f"| {_cell(check.name)} | {'통과' if check.passed else '실패'} | {check.status_code or '-'} | "
            f"{check.status or '-'} | {seconds} | {score} | {_cell(', '.join(check.sources)) or '-'} |"
        )
    answers = [check for check in checks if check.answer]
    if answers:
        lines += ["", "## 답변", ""]
        lines += [f"- **{_cell(check.name)}**: {_cell(check.answer)}" for check in answers]
    failures = [check for check in checks if check.problems]
    if failures:
        lines += ["", "## 실패 내용", ""]
        lines += [f"- **{_cell(check.name)}**: {_cell('; '.join(check.problems))}" for check in failures]
    lines += [
        "",
        "질문 3개만 확인한 동작 점검입니다. 답변 품질이나 임계치의 적절성을 입증하지 않습니다.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="배포된 엔드포인트를 확인합니다.")
    parser.add_argument("--base-url", help="기본값: SMOKE_BASE_URL")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="결과 Markdown 경로")
    parser.add_argument("--timeout", type=float, default=60.0, help="요청 제한 시간(초)")
    args = parser.parse_args(argv)

    load_dotenv()
    base_url = (args.base_url or os.environ.get("SMOKE_BASE_URL") or "").rstrip("/")
    api_key = os.environ.get("SMOKE_API_KEY") or os.environ.get("APP_API_KEY") or ""
    if not base_url or not api_key:
        print("SMOKE_BASE_URL과 SMOKE_API_KEY(또는 APP_API_KEY)가 필요합니다.", file=sys.stderr)
        return 1

    with httpx.Client(base_url=base_url, timeout=args.timeout) as client:
        checks = run_checks(client, api_key)

    report = render(checks, base_url, datetime.now(timezone.utc).astimezone())
    args.output.write_text(report, encoding="utf-8")
    print(report)
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
