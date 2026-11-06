"""AC에 직접 대응하지 않는 보강 시험. 4단계 도구(smoke_test, calibrate)의 판정을 확인한다."""

import json
from datetime import datetime

import httpx
from fastapi.testclient import TestClient

import calibrate
import evaluate
import smoke_test
from helpers import API_KEY, ENV, QUOTE, FakeChat, hit, model_output
from providers import Providers, SearchHit


class QuestionAwareStore:
    """질문 종류를 벡터로 구분해 실제 배포와 비슷하게 응답한다."""

    def search(self, vector, k):
        return {
            "price": [hit("price", 0.8, "프로 요금제의 가격은 월 9,900원입니다.", "faq.md")],
            "refund": [hit("refund", 0.8, "연간 결제는 결제일로부터 30일 이내에 전액 환불됩니다.", "refund_policy.md")],
        }.get(vector[0], [hit("other", 0.3)])


class QuestionAwareEmbedder:
    def embed(self, texts):
        text = texts[0]
        return [["price" if "가격" in text else "refund" if "환불" in text else "none"]]


class QuestionAwareChat:
    def complete(self, system, user, schema):
        if "9,900" in user:
            return model_output("price", "프로 요금제의 가격은 월 9,900원입니다.", "월 9,900원입니다.")
        return model_output("refund", "결제일로부터 30일 이내에 전액 환불됩니다.", "결제일로부터 30일 이내입니다.")


def live_client(chat=None, store=None):
    from api import create_app
    from config import load_settings

    app = create_app(
        settings=load_settings(ENV),
        providers=Providers(
            embedder=QuestionAwareEmbedder(), chat=chat or QuestionAwareChat(), store=store or QuestionAwareStore()
        ),
    )
    return TestClient(app)


def by_name(checks):
    return {check.name: check for check in checks}


def test_smoke_passes_on_healthy_service():
    checks = smoke_test.run_checks(live_client(), API_KEY)
    assert len(checks) == 5
    assert all(check.passed for check in checks), [check.problems for check in checks]


def test_smoke_fails_when_answer_or_source_is_wrong():
    # 모델이 틀린 금액을 답하면 근거 인용이 맞아도 실패해야 한다.
    class WrongChat(QuestionAwareChat):
        def complete(self, system, user, schema):
            return model_output("price", "프로 요금제의 가격은 월 9,900원입니다.", "월 1,000원입니다.")

    checks = by_name(smoke_test.run_checks(live_client(chat=WrongChat()), API_KEY))
    price = checks[smoke_test.ANSWERED_CASES[0][0]]
    assert not price.passed
    assert any("기대값" in problem for problem in price.problems)


def test_smoke_fails_when_unanswerable_question_gets_answer():
    # 임계치가 너무 낮아 답 없는 질문에 답하면 실패해야 한다.
    class AlwaysHighStore(QuestionAwareStore):
        def search(self, vector, k):
            return [hit("c1", 0.9, content="엑셀에서 함수를 쓰는 법을 안내합니다.")]

    chat = FakeChat(model_output(quote="엑셀에서 함수를 쓰는 법을 안내합니다.", answer="엑셀에서 함수를 쓰는 법을 안내합니다."))
    checks = by_name(smoke_test.run_checks(live_client(chat=chat, store=AlwaysHighStore()), API_KEY))
    assert not checks[smoke_test.NOT_FOUND_QUESTION].passed


def test_smoke_fails_when_auth_is_missing_and_on_upstream_errors():
    assert not smoke_test.check_auth(_status_client(200)).passed
    check = smoke_test.check_answered(_status_client(502, {"error": {"code": "UPSTREAM_ERROR"}}), API_KEY, "q", "x", "y")
    assert not check.passed and "UPSTREAM_ERROR" in check.problems[0]


def test_smoke_reports_connection_failure():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="https://app.example", transport=httpx.MockTransport(refuse))
    checks = smoke_test.run_checks(client, API_KEY)
    assert not any(check.passed for check in checks)


def test_smoke_report_and_exit_code_exclude_key(tmp_path, monkeypatch):
    checks = smoke_test.run_checks(live_client(), API_KEY)
    report = smoke_test.render(checks, "https://app.example", datetime(2026, 11, 4))
    assert "통과 (5/5)" in report
    assert API_KEY not in report

    # 키나 주소가 없으면 호출하지 않고 실패한다.
    monkeypatch.setattr(smoke_test, "load_dotenv", lambda: None)
    for name in ("SMOKE_BASE_URL", "SMOKE_API_KEY", "APP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "result.md"
    assert smoke_test.main(["--output", str(output)]) == 1
    assert not output.exists()


def _status_client(status, body=None):
    return httpx.Client(
        base_url="https://app.example",
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=body or {})),
    )


def test_calibration_midpoint_when_separated():
    chosen = calibrate.choose_threshold([0.75, 0.80], [0.55, 0.61])
    assert chosen["threshold"] == 0.68
    assert chosen["overlap"] is False
    assert chosen["missed_answerable"] == 0
    assert chosen["cosine"] == round(2 - 1 / 0.68, 4)


def test_calibration_blocks_all_off_topic_when_overlapping():
    chosen = calibrate.choose_threshold([0.60, 0.75, 0.80], [0.55, 0.70])
    assert chosen["overlap"] is True
    assert chosen["threshold"] > 0.70
    assert chosen["missed_answerable"] == 1


class ScriptedProviders:
    """질문별 점수와 모델 출력을 정해 둔 가짜."""

    def __init__(self, scores, outputs=None):
        self.embedder = self
        self.store = self
        self.chat = self
        self.scores = scores
        self.outputs = outputs or {}
        self.chat_calls = 0

    def embed(self, texts):
        self.question = texts[0]
        return [[self.scores[texts[0]]]]

    def search(self, vector, k):
        return [SearchHit("c1", "s", f"{self.question} {QUOTE}", vector[0])]

    def complete(self, system, user, schema):
        self.chat_calls += 1
        return self.outputs.get(self.question, model_output(quote=f"{self.question} {QUOTE}"))


QUESTIONS = {
    "selection": {"answerable": ["a1"], "off_topic": ["o1"], "in_domain": ["d1"]},
    "validation": {"answerable": ["a2", "a3"], "off_topic": ["o2"], "in_domain": ["d2", "d3"]},
}
SCORES = {"a1": 0.8, "o1": 0.6, "d1": 0.95, "a2": 0.75, "a3": 0.65, "o2": 0.72, "d2": 0.9, "d3": 0.9}


def test_calibration_in_domain_scores_do_not_raise_threshold(tmp_path):
    # 같은 주제 질문의 점수가 아무리 높아도 임계치에 영향을 주면 안 된다. 실제로 답을 모두 막았던 결함이다.
    result = tmp_path / "r.json"
    assert calibrate.run_select(QUESTIONS, ScriptedProviders(SCORES), result) == 0
    saved = json.loads(result.read_text(encoding="utf-8"))["selection"]
    assert saved["threshold"] == 0.7
    assert saved["rows"]["in_domain"][0]["score"] == 0.95


def test_calibration_validates_full_flow_only_once(tmp_path):
    result = tmp_path / "r.json"
    refusal = model_output(answerable=False, answer="")
    outputs = {"d2": refusal, "d3": model_output(quote=f"d3 {QUOTE}", answer="d3 환불은 구매 후 14일 안에 지어낸 답입니다.")}  # d3은 막지 못한 경우다.
    providers = ScriptedProviders(SCORES, outputs)

    assert calibrate.run_validate(QUESTIONS, providers, 0.7, result) == 1, "선정 전에는 검증할 수 없다"
    assert calibrate.run_select(QUESTIONS, providers, result) == 0
    assert providers.chat_calls == 0, "선정은 채팅을 부르지 않는다"

    assert calibrate.run_validate(QUESTIONS, providers, 0.7, result) == 0
    outcome = json.loads(result.read_text(encoding="utf-8"))["validation"]
    assert outcome["answerable_answered"] == 1, "임계치 미만 a3은 답하지 못한다"
    assert outcome["off_topic_blocked"] == 0, "막지 못한 주제 밖 질문을 숨기지 않는다"
    assert outcome["in_domain_blocked"] == 1, "모델이 거절한 d2만 막힌다"

    # 결과를 본 뒤 다시 조정할 수 없다.
    assert calibrate.run_validate(QUESTIONS, providers, 0.6, result) == 1
    assert calibrate.run_select(QUESTIONS, providers, result) == 1
    assert json.loads(result.read_text(encoding="utf-8"))["validation"]["threshold"] == 0.7


def test_evaluate_applies_threshold_and_requires_it(tmp_path, monkeypatch):
    # 임계치 없이 돌린 평가가 주제 밖 질문까지 모델에 보냈던 결함의 재발을 막는다.
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps([{"question": "o2", "expect": "not_found"}, {"question": "a2", "expect": "answered"}]), encoding="utf-8")
    output = tmp_path / "r.json"
    providers = ScriptedProviders({"o2": 0.5, "a2": 0.9})
    monkeypatch.setattr(evaluate, "load_dotenv", lambda: None)
    monkeypatch.setattr(evaluate, "build_providers", lambda settings, ingest: providers)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    monkeypatch.delenv("MIN_SEARCH_SCORE", raising=False)
    assert evaluate.main(["--questions", str(questions), "--output", str(output)]) == 1
    assert not output.exists()

    monkeypatch.setenv("MIN_SEARCH_SCORE", "0.7")
    assert evaluate.main(["--questions", str(questions), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["min_search_score"] == 0.7
    assert result["rows"][0]["steps"] == ["below_threshold@0.5000"]
    assert result["rows"][0]["model_outputs"] == []
    assert len(result["rows"][1]["model_outputs"]) == 1


def test_evaluate_judges_expected_result_and_hallucination():
    documents = "프로 요금제의 가격은 월 9,900원입니다."
    answer = {"question": "가격은?", "expect": "answered", "must_include": ["9,900"], "forbidden": ["무료입니다"]}
    assert evaluate.judge(answer, "answered", "월 9,900원입니다.", documents) == {"correct": True, "hallucination": []}
    assert not evaluate.judge(answer, "not_found", "문서에서 찾지 못함", documents)["correct"]
    assert evaluate.judge(answer, "answered", "월 9,900원이고 무료입니다.", documents)["hallucination"]
    assert evaluate.judge(answer, "answered", "월 9,900원이고 RSA로 보호됩니다.", documents)["hallucination"]
    trap = {"question": "직원 수는?", "expect": "not_found"}
    assert evaluate.judge(trap, "not_found", "문서에서 찾지 못함", documents) == {"correct": True, "hallucination": []}
    assert evaluate.judge(trap, "answered", "네, 없습니다.", documents)["hallucination"]

    # 모순된 안내는 오답이지만 지어낸 내용은 아니므로 환각으로 세지 않는다.
    contradiction = {**answer, "must_exclude": ["확인되지 않"]}
    verdict = evaluate.judge(contradiction, "answered", "월 9,900원입니다. 할인은 확인되지 않습니다.", documents)
    assert verdict == {"correct": False, "hallucination": []}


def test_evaluate_summary_reports_categories():
    rows = [
        {"category": "base", "correct": True, "hallucination": []},
        {"category": "holdout", "correct": False, "hallucination": ["금지 표현: x"]},
        {"category": "holdout", "correct": True, "hallucination": []},
    ]
    summary = evaluate.summarize(rows)
    assert summary["total"] == 3 and summary["correct"] == 2 and summary["hallucinations"] == 1
    assert summary["by_category"]["holdout"] == {"total": 2, "correct": 1, "hallucinations": 1}


def test_calibration_records_invalid_model_output(tmp_path):
    providers = ScriptedProviders(SCORES, {"a2": "JSON이 아님"})
    row = calibrate.answer_status("a2", providers, 0.7)
    assert row["status"] == "model_output_invalid"
