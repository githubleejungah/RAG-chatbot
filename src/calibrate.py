"""임계치 보정. 절차는 design/decisions.md D-07을 따른다.

실행:
  python src/calibrate.py select                    선정용 질문으로 임계치를 계산한다. 임베딩과 검색만 호출한다.
  python src/calibrate.py validate --threshold 값   검증용 질문을 답변 흐름 전체로 한 번만 평가한다. 채팅도 호출한다.

임계치는 주제 밖 질문만 막는다. 같은 주제인데 답이 없는 질문은 점수가 높아 임계치로 구분할 수 없으며,
모델 판단과 인용문 대조가 막는다. 검증 결과를 본 뒤 값을 바꾸면 검증의 의미가 없어지므로 다시 평가하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

import rag
from config import ConfigError, load_settings
from providers import UpstreamBusy, UpstreamError, UpstreamTimeout, build_providers

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "data" / "calibration_questions.json"
RESULT_PATH = ROOT / "calibration_result.json"
TOP_K = rag.DEFAULT_TOP_K
ABOVE_MAX = 0.001
LABELS = ("answerable", "off_topic", "in_domain")


def top_score(question: str, providers) -> float:
    vector = providers.embedder.embed([question])[0]
    return max((hit.score for hit in providers.store.search(vector, TOP_K)), default=0.0)


def choose_threshold(answerable: list[float], off_topic: list[float]) -> dict:
    """두 분포 사이의 중간값을 쓰고, 겹치면 주제 밖 질문을 모두 막는 값을 쓴다."""
    lowest_answerable = min(answerable)
    highest_off_topic = max(off_topic)
    overlap = highest_off_topic >= lowest_answerable
    threshold = highest_off_topic + ABOVE_MAX if overlap else (lowest_answerable + highest_off_topic) / 2
    threshold = round(threshold, 4)
    return {
        "threshold": threshold,
        "cosine": round(2 - 1 / threshold, 4),
        "overlap": overlap,
        "lowest_answerable": lowest_answerable,
        "highest_off_topic": highest_off_topic,
        "missed_answerable": sum(score < threshold for score in answerable),
    }


def answer_status(question: str, providers, threshold: float) -> dict:
    """실제 답변 흐름의 결과. 모델 출력 오류도 숨기지 않고 기록한다."""
    try:
        result = rag.ask(
            question,
            TOP_K,
            embedder=providers.embedder,
            store=providers.store,
            chat=providers.chat,
            min_score=threshold,
        )
    except rag.ModelOutputInvalid:
        return {"question": question, "status": "model_output_invalid", "score": None, "answer": ""}
    return {"question": question, "status": result.status, "score": result.top_score, "answer": result.answer}


def summarize(rows: dict, threshold: float) -> dict:
    count = lambda label, status: sum(row["status"] == status for row in rows[label])  # noqa: E731
    return {
        "threshold": threshold,
        "answerable_answered": count("answerable", "answered"),
        "answerable_total": len(rows["answerable"]),
        "off_topic_blocked": count("off_topic", "not_found"),
        "off_topic_total": len(rows["off_topic"]),
        "in_domain_blocked": count("in_domain", "not_found"),
        "in_domain_total": len(rows["in_domain"]),
        "model_output_invalid": sum(row["status"] == "model_output_invalid" for label in LABELS for row in rows[label]),
    }


def _load_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_result(path: Path, result: dict) -> None:
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def run_select(questions: dict, providers, result_path: Path) -> int:
    result = _load_result(result_path)
    if "validation" in result:
        print("이미 검증을 마쳤습니다. 다시 선정하려면 새 검증 질문을 만들고 결과 파일을 옮기세요.", file=sys.stderr)
        return 1
    selection = questions["selection"]
    rows = {
        label: [{"question": question, "score": top_score(question, providers)} for question in selection[label]]
        for label in LABELS
    }
    for label in LABELS:
        print(f"[{label}]")
        for row in sorted(rows[label], key=lambda item: item["score"]):
            print(f"  {row['score']:.4f}  {row['question']}")
    scores = lambda label: [row["score"] for row in rows[label]]  # noqa: E731
    chosen = choose_threshold(scores("answerable"), scores("off_topic"))
    result["selection"] = {"rows": rows, **chosen}
    _save_result(result_path, result)
    print(json.dumps(chosen, ensure_ascii=False, indent=2))
    return 0


def run_validate(questions: dict, providers, threshold: float, result_path: Path) -> int:
    result = _load_result(result_path)
    if "selection" not in result:
        print("먼저 select를 실행하세요.", file=sys.stderr)
        return 1
    if "validation" in result:
        print("검증은 한 번만 합니다. 결과를 본 뒤 값을 조정하면 검증의 의미가 없어집니다.", file=sys.stderr)
        return 1
    validation = questions["validation"]
    rows: dict[str, list[dict]] = {}
    for label in LABELS:
        print(f"[{label}]")
        rows[label] = []
        for question in validation[label]:
            row = answer_status(question, providers, threshold)
            rows[label].append(row)
            print(f"  {row['status']:<22} {question}", flush=True)
    outcome = summarize(rows, threshold)
    result["validation"] = {"rows": rows, **outcome}
    _save_result(result_path, result)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="임계치를 보정합니다.")
    parser.add_argument("mode", choices=("select", "validate"))
    parser.add_argument("--threshold", type=float, help="validate에 쓸 값")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--result", type=Path, default=RESULT_PATH)
    args = parser.parse_args(argv)
    if args.mode == "validate" and args.threshold is None:
        parser.error("validate에는 --threshold가 필요합니다.")

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    try:
        providers = build_providers(load_settings(for_api=False), ingest=True)
        questions = json.loads(args.questions.read_text(encoding="utf-8"))
        if args.mode == "select":
            return run_select(questions, providers, args.result)
        return run_validate(questions, providers, args.threshold, args.result)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
    except (UpstreamBusy, UpstreamTimeout, UpstreamError) as exc:
        print(f"외부 서비스 오류: {type(exc).__name__}. 권한과 설정을 확인하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
