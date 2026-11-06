"""실제 모델 평가. 평가 질문을 답변 흐름 전체로 1회씩 돌려 정답률과 환각을 잽니다.

실행: LLM_PROVIDER=ollama MIN_SEARCH_SCORE=값 python src/evaluate.py [--output eval_result.json]
확인: EVAL_RESULT=eval_result.json pytest -m model_eval

기대 결과로 맞았는지만 자동으로 판정합니다. 금지 표현으로 정해 두지 않은 한글 환각은 잡지 못하므로
결과 파일의 답변을 사람이 함께 읽어야 합니다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import rag
from config import MIN_API_KEY_CHARS, ConfigError, load_settings
from providers import UpstreamBusy, UpstreamError, UpstreamTimeout, build_providers

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "data" / "eval_questions.json"
DOCS_PATH = ROOT / "data" / "docs"
RESULT_PATH = ROOT / "eval_result.json"
MIN_ACCURACY = 0.8
_UNUSED_API_KEY = "x" * MIN_API_KEY_CHARS


class _RecordingChat:
    """차단된 답변의 원인을 볼 수 있도록 모델 원본 출력을 결과 파일에 남긴다."""

    def __init__(self, chat) -> None:
        self.chat = chat
        self.outputs: list[str] = []

    def complete(self, system: str, user: str, schema: dict) -> str:
        output = self.chat.complete(system, user, schema)
        self.outputs.append(output)
        return output


def fingerprint(questions_path: Path = QUESTIONS_PATH) -> str:
    """이전 코드나 질문으로 낸 결과를 통과로 세지 않으려고 결과에 함께 남긴다."""
    digest = hashlib.sha256()
    for path in (Path(rag.__file__), Path(__file__), questions_path):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def judge(case: dict, status: str, answer: str, documents: str) -> dict:
    """기대 결과와 환각을 판정한다. 환각이 있으면 정답으로 세지 않는다."""
    claims = " ".join(sentence for sentence in rag._sentences(answer) if rag.NOT_CONFIRMED_MARK not in sentence)
    hallucination = []
    if status == "answered":
        if case["expect"] == "not_found":
            hallucination.append("답이 없는 질문에 답함")
        hallucination += [f"금지 표현: {term}" for term in case.get("forbidden", []) if term in answer]
        unknown = rag._facts(claims) - rag._facts(f"{case['question']} {documents}")
        hallucination += [f"문서에 없는 표기: {token}" for token in sorted(unknown)]
    expected = (
        status == case["expect"]
        and all(term in answer for term in case.get("must_include", []))
        and not any(term in answer for term in case.get("must_exclude", []))
    )
    return {"correct": expected and not hallucination, "hallucination": hallucination}


def summarize(rows: list[dict]) -> dict:
    correct = sum(row["correct"] for row in rows)
    by_category: dict[str, dict] = {}
    for row in rows:
        group = by_category.setdefault(row["category"], {"total": 0, "correct": 0, "hallucinations": 0})
        group["total"] += 1
        group["correct"] += row["correct"]
        group["hallucinations"] += bool(row["hallucination"])
    return {
        "total": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows),
        "hallucinations": sum(bool(row["hallucination"]) for row in rows),
        "by_category": by_category,
    }


def evaluate_one(case: dict, providers, min_score: float, documents: str) -> dict:
    chat = _RecordingChat(providers.chat)
    row = {
        "question": case["question"],
        "category": case.get("category", "base"),
        "expect": case["expect"],
        "model_outputs": chat.outputs,
    }
    try:
        result = rag.ask(
            case["question"],
            rag.DEFAULT_TOP_K,
            embedder=providers.embedder,
            store=providers.store,
            chat=chat,
            min_score=min_score,
        )
    except rag.ModelOutputInvalid:
        return {**row, "status": "model_output_invalid", "answer": "", "steps": [], "correct": False, "hallucination": []}
    verdict = judge(case, result.status, result.answer, documents)
    return {**row, "status": result.status, "answer": result.answer, "steps": result.steps, **verdict}


def run(cases: list[dict], providers, min_score: float, documents: str) -> dict:
    rows = []
    for case in cases:
        row = evaluate_one(case, providers, min_score, documents)
        rows.append(row)
        print(f"{'O' if row['correct'] else 'X'} {row['status']} {row['steps']} {row['question']}", flush=True)
        print(f"    {row['answer']}", flush=True)
    return {"summary": summarize(rows), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 모델로 답변 품질을 평가합니다.")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--output", type=Path, default=RESULT_PATH)
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    try:
        # 평가는 API를 띄우지 않으므로 키는 쓰지 않지만, 임계치는 실제 서비스처럼 설정 값을 검증해 적용한다.
        settings = load_settings({**os.environ, "API_KEY": _UNUSED_API_KEY})
        providers = build_providers(settings, ingest=True)
        # 실행 중에 코드나 질문이 바뀌어도 실제로 돌린 판의 지문이 남도록 먼저 계산한다.
        run_fingerprint = fingerprint(args.questions)
        cases = json.loads(args.questions.read_text(encoding="utf-8"))
        documents =" ".join(path.read_text(encoding="utf-8") for path in sorted(DOCS_PATH.iterdir()))
        result = run(cases, providers, settings.min_search_score, documents)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1
    except (UpstreamBusy, UpstreamTimeout, UpstreamError) as exc:
        print(f"외부 서비스 오류: {type(exc).__name__}. 권한과 설정을 확인하세요.", file=sys.stderr)
        return 1

    result["fingerprint"] = run_fingerprint
    result["min_search_score"] = settings.min_search_score
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = result["summary"]
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["accuracy"] >= MIN_ACCURACY and summary["hallucinations"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
