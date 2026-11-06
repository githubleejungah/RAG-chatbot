"""AC-EVAL: 실제 모델 평가 결과의 기준. 결과는 src/evaluate.py가 만든다. 비용과 시간이 들어 pytest에서 직접 돌리지 않는다."""

import json
import os
from pathlib import Path

import pytest

import evaluate

model_eval = pytest.mark.skipif(not os.environ.get("EVAL_RESULT"), reason="EVAL_RESULT 없음: evaluate.py 실행 후 확인")


def load_result() -> dict:
    result = json.loads(Path(os.environ["EVAL_RESULT"]).read_text(encoding="utf-8"))
    assert result["fingerprint"] == evaluate.fingerprint(), "코드나 평가 질문이 바뀐 뒤의 결과가 아니다"
    questions = json.loads(evaluate.QUESTIONS_PATH.read_text(encoding="utf-8"))
    assert result["summary"]["total"] == len(questions), "평가 질문 전체를 돌린 결과가 아니다"
    return result


@pytest.mark.model_eval
@model_eval
def test_ac_eval_01_accuracy_meets_target():
    summary = load_result()["summary"]
    assert summary["accuracy"] >= evaluate.MIN_ACCURACY, summary


@pytest.mark.model_eval
@model_eval
def test_ac_eval_02_no_hallucination():
    result = load_result()
    assert [row for row in result["rows"] if row["hallucination"]] == []
