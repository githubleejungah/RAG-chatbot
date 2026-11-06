"""AC-NF: 찾지 못함. 임계치를 넘지 못하거나 인용을 믿을 수 없으면 모델 답변을 내보내지 않는다."""

import json
import logging
import os

import httpx
import pytest

from helpers import MODEL_ANSWER, QUOTE, THRESHOLD, FakeChat, Fakes, hit, model_output

NOT_FOUND = "문서에서 찾지 못함"

deployed = pytest.mark.skipif(not os.environ.get("SMOKE_BASE_URL"), reason="SMOKE_BASE_URL 없음: 배포 후 실행")


@pytest.mark.deployed
@deployed
def test_ac_nf_06_deployed_unanswerable_questions():
    base_url = os.environ["SMOKE_BASE_URL"].rstrip("/")
    questions = ("엑셀에서 VLOOKUP 함수를 쓰는 법을 알려줘", "구름노트 직원은 몇 명인가요?")
    with httpx.Client(timeout=60) as client:
        for question in questions:
            response = client.post(
                f"{base_url}/ask", json={"question": question}, headers={"X-API-Key": os.environ["SMOKE_API_KEY"]}
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "not_found", question
            assert body["answer"] == NOT_FOUND
            assert body["sources"] == []


def assert_not_found(response, top_score):
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_found"
    assert body["answer"] == NOT_FOUND
    assert body["sources"] == []
    assert body["retrieved"] == []
    assert body["top_score"] == top_score
    assert MODEL_ANSWER not in response.text


def test_ac_nf_01_below_threshold_skips_generation():
    fakes = Fakes(hits=[hit("c1", THRESHOLD - 0.001), hit("c2", 0.5)])
    assert_not_found(fakes.ask(), THRESHOLD - 0.001)
    assert fakes.chat.calls == []


def test_ac_nf_02_no_results():
    fakes = Fakes(hits=[])
    assert_not_found(fakes.ask(), None)
    assert fakes.chat.calls == []


def test_ac_nf_03_score_equal_to_threshold_proceeds():
    fakes = Fakes(hits=[hit("c1", THRESHOLD)])
    body = fakes.ask().json()
    assert len(fakes.chat.calls) == 1
    assert body["status"] == "answered"


def test_ac_nf_04_model_says_not_answerable():
    # 모델이 답변을 함께 보내도 내보내지 않는다.
    fakes = Fakes(output=model_output(answerable=False))
    assert_not_found(fakes.ask(question="대표 이름은 무엇인가요?"), 0.8)


def test_ac_nf_07_fact_missing_from_quotes_fails_closed():
    content = "구름노트 사용법 안내입니다."
    cases = {
        "인용문에 없는 숫자": "최신 버전은 4.5.0.3입니다.",
        "인용문에 없는 영문": "메모는 RSA로 암호화됩니다.",
        "숫자 일부만 같음": "구름노트 사용법은 100단계입니다. 안내는 10단계입니다.",
        "근거에도 질문에도 없는 숫자": "환불 기간은 20일입니다.",
    }
    for name, answer in cases.items():
        fakes = Fakes(hits=[hit(content=content + " 안내는 10단계입니다.")], output=model_output(quote=content, answer=answer))
        response = fakes.ask()
        assert response.json()["status"] == "not_found", name
        assert_not_found(response, 0.8)


def test_ac_nf_08_impolite_answer_without_usable_quote_fails_closed():
    polite_quote = "프로 요금제의 가격은 월 9,900원입니다."
    cases = {
        "질문과 겹치지 않는 문장뿐임": ("9,900원", "월 9,900원"),
        "대조할 사실이 없는 단답": ("지어낸 답", polite_quote),
        "한글 없는 단답에 정중하지 않은 인용": ("9,900", "월 9,900원"),
        "대조할 사실이 없는 영문": ("price", polite_quote),
        "'~함' 문체": ("가격은 9,900원임.", "월 9,900원"),
        "확인 불가 문장이 섞인 단답": ("9,900원. 직원 수는 문서에서 확인되지 않습니다.", polite_quote),
    }
    for name, (answer, quote) in cases.items():
        fakes = Fakes(hits=[hit(content=polite_quote)], output=model_output(quote=quote, answer=answer))
        response = fakes.ask()
        assert response.json()["status"] == "not_found", name
        assert_not_found(response, 0.8)

    # 숫자가 근거에 있어도 그 문장이 질문과 무관하면 답으로 내보내지 않는다.
    content = "월 9,900원입니다. 환불은 가능합니다."
    fakes = Fakes(hits=[hit(content=content)], output=model_output(quote="환불은 가능합니다.", answer="9,900원"))
    assert_not_found(fakes.ask(), 0.8)


def test_ac_nf_09_compound_with_no_answer_is_not_found():
    fakes = Fakes(output=model_output(answerable=False))
    response = fakes.ask(question="대표 이름은 무엇이고, 직원은 몇 명인가요?")
    assert_not_found(response, 0.8)
    assert len(fakes.chat.calls) == 2


def test_ac_nf_10_answer_unrelated_to_quote_fails_closed():
    # 실제 실패 사례: 인용문은 문서에 있지만 "1년 계약 할인" 질문과 무관하다.
    annual = "연간 결제는 결제일로부터 30일 이내에 전액 환불됩니다."
    answer = f"네, 1년 계약은 프로 요금제의 연간 결제로 제공됩니다. {annual}"
    fakes = Fakes(hits=[hit(content=annual)], output=model_output(quote=annual, answer=answer))
    assert_not_found(fakes.ask(question="1년 계약 할인은 있나요?"), 0.8)

    # 인용문은 질문과 관련 있어도, 답변 문장이 인용문과 질문 어느 쪽에도 기대지 않으면 막는다.
    price = "프로 요금제의 가격은 월 9,900원입니다."
    made_up = "다음 분기부터 학생 전용 혜택이 별도 신청 없이 자동 적용됩니다."
    fakes = Fakes(hits=[hit(content=price)], output=model_output(quote=price, answer=f"{price} {made_up}"))
    assert_not_found(fakes.ask(question="프로 요금제 가격은 얼마인가요?"), 0.8)

    # 실제 실패 사례: 질문을 되풀이한 답은 인용문에 흔한 말 하나가 겹쳐도 막는다.
    intro = "구름노트는 메모를 작성하고 여러 기기에서 동기화하는 서비스입니다."
    security = "모든 메모는 서버에 저장될 때 AES-256으로 암호화됩니다."
    for question, answer, quote in (
        ("구름노트에는 가족 요금제가 없나요?", "네, 구름노트에는 가족 요금제가 없습니다.", intro),
        ("메모 첨부 파일에는 크기 제한이 없나요?", "네, 메모 첨부 파일에는 크기 제한이 없습니다.", security),
        ("구름노트 본사는 어디인가요?", "구름노트 본사는 서울에 있습니다.", intro),
    ):
        assert_not_found(Fakes(hits=[hit(content=quote)], output=model_output(quote=quote, answer=answer)).ask(question=question), 0.8)

    # 실제 실패 사례: "끊기면"과 "끊겨도"처럼 활용형이 달라도, 답변이 새로 말한 내용이 인용문에 있으면 통과한다.
    guide = "메모는 입력하는 동안 자동으로 저장됩니다. 인터넷이 끊겨도 기기에 저장되고, 다시 연결되면 자동으로 동기화됩니다."
    answer = "인터넷이 끊기면 작성 중인 메모는 자동으로 저장됩니다. 다시 연결되면 자동으로 동기화됩니다."
    body = Fakes(hits=[hit(content=guide)], output=model_output(quote=guide, answer=answer)).ask(question="인터넷이 끊기면 작성 중인 메모는 어떻게 되나요?").json()
    assert body["answer"] == answer

    # 실제 실패 사례: 단답의 사실이 인용문에 있으면 질문 핵심어가 덜 맞아도 인용문으로 바꾼다.
    shortcut = "새 메모를 만들려면 더하기 버튼이나 Ctrl+N을 누르세요."
    body = Fakes(hits=[hit(content=shortcut)], output=model_output(quote=shortcut, answer="Ctrl+N")).ask(question="새 메모를 만드는 단축키는 무엇인가요?").json()
    assert body["answer"] == shortcut

    # 실제 실패 사례: 의문사 앞의 대상("주소")이 인용문에 없으면, 같은 주제의 다른 내용(운영 시간)으로 답한 것이므로 막는다.
    hours = "고객센터 운영 시간은 평일 오전 9시부터 오후 6시까지이며, 주말과 공휴일에는 쉽니다."
    fakes = Fakes(hits=[hit(content=hours)], output=model_output(quote=hours, answer=hours))
    assert_not_found(fakes.ask(question="구름노트 고객센터 주소는 어디인가요?"), 0.8)

    # 그 예외로 함정 질문이 풀리면 안 된다. 예/아니요 질문과, 제품명만 겹친 소개 문장, 질문을 되풀이한 답은 계속 막는다.
    plans = "프로 요금제는 해지하면 남은 기간까지 서비스를 계속 이용할 수 있습니다."
    for question, answer, quote in (
        ("프로 요금제를 중간에 해지해도 위약금은 내지 않나요?", plans, plans),
        ("구름노트 본사는 어디인가요?", intro, intro),
        ("구름노트 메모 첨부 크기는 얼마인가요?", "구름노트 메모 첨부 크기는 얼마입니다.", intro),
    ):
        assert_not_found(Fakes(hits=[hit(content=quote)], output=model_output(quote=quote, answer=answer)).ask(question=question), 0.8)

    # 한 글자 단어로 대상을 가른 질문은, 그 글자가 없는 인용문이나 근거 문장으로 답하지 않는다.
    price = "프로 요금제의 가격은 월 9,900원입니다."
    for answer in ("팀 요금제의 가격은 월 9,900원입니다.", "9,900원"):
        fakes = Fakes(hits=[hit(content=price)], output=model_output(quote=price, answer=answer))
        assert_not_found(fakes.ask(question="팀 요금제의 가격은 얼마인가요?"), 0.8)
    # 실제 실패 사례: 답변이 되풀이한 질문 핵심어("음성")가 인용문에 없으면, 다른 핵심어가 겹쳐도 막는다.
    autosave = "메모는 입력하는 동안 자동으로 저장됩니다."
    fakes = Fakes(hits=[hit(content=autosave)], output=model_output(quote=autosave, answer="네, 구름노트에서는 음성으로 메모를 입력할 수 있습니다."))
    assert_not_found(fakes.ask(question="메모를 음성으로 입력할 수 있나요?"), 0.8)
    # "몇 대까지"의 "대까"는 답변의 "2대까지"와 같은 핵심어가 아니므로, 긍정 답변이어도 막지 않는다.
    devices = "한 계정에 연결할 수 있는 기기는 무료 요금제에서 2대, 프로 요금제에서 10대입니다."
    answer = "네, 무료 요금제에서는 2대까지 연결할 수 있습니다."
    body = Fakes(hits=[hit(content=devices)], output=model_output(quote=devices, answer=answer)).ask(question="무료 요금제에서는 기기를 몇 대까지 연결할 수 있나요?").json()
    assert body["answer"] == answer
    # 실제 회귀 사례: "볼 수"의 "볼"은 동사이므로 인용문에 없어도 막지 않는다.
    staff = "직원은 사용자가 고객 지원을 위해 동의한 경우에만 메모를 열람할 수 있습니다."
    body = Fakes(hits=[hit(content=staff)], output=model_output(quote=staff, answer=staff)).ask(question="직원이 내 메모를 볼 수 있나요?").json()
    assert body["answer"] == staff

    # 실제 실패 사례: 숫자 없는 답이 맞고 인용만 잘못 골랐으면, 질문과 답이 모두 맞는 근거 문장으로 출처를 바꾼다.
    device = "새로운 기기에서 로그인하면 가입한 이메일로 알림이 발송됩니다."
    wrong = "연결된 기기는 설정의 기기 관리 화면에서 해제할 수 있습니다."
    fakes = Fakes(hits=[hit(content=f"{wrong} {device}")], output=model_output(quote=wrong, answer=device))
    body = fakes.ask(question="새로운 기기에서 로그인하면 알림은 어디로 오나요?").json()
    assert body["answer"] == device
    assert [row["quote"] for row in body["sources"]] == [device]

    # 실제 실패 사례: 답의 표기가 근거 문장에 그대로 있고 인용만 잘못 골랐으면 출처만 바꾼다.
    guide = "메모는 폴더와 태그로 정리할 수 있습니다. PDF와 마크다운 형식을 지원합니다."
    answer = "PDF와 마크다운 형식을 지원합니다."
    body = Fakes(hits=[hit(content=guide)], output=model_output(quote="메모는 폴더와 태그로 정리할 수 있습니다.", answer=answer)).ask(
        question="메모를 어떤 형식으로 내보낼 수 있나요?"
    ).json()
    assert body["answer"] == answer
    assert [row["quote"] for row in body["sources"]] == [answer]

    # 알려진 비용: 유의어를 쓴 짧은 질문은 숫자 표기로 출처를 찾지 못하면 막힌다.
    plan = "환불은 구매 후 안내에 따라 신청합니다."
    assert_not_found(Fakes(hits=[hit(content=plan)], output=model_output(quote=plan, answer=plan)).ask(question="환불 기간은?"), 0.8)

    # 표현을 바꾼 답과 "확인되지 않" 문장은 막지 않는다.
    missing = "직원 수는 문서에서 확인되지 않습니다."
    body = Fakes(hits=[hit(content=price)], output=model_output(quote=price, answer=f"프로 요금제 월 요금은 9,900원입니다. {missing}")).ask(
        question="프로 요금제 요금은 얼마인가요?"
    ).json()
    assert body["status"] == "answered"
    assert missing in body["answer"]


def test_ac_nf_11_failure_reason_logged_per_sub_question(caplog):
    question = "로그에남으면안되는 환불 기간은 얼마이고, 로그에남으면안되는 대표 이름은 무엇인가요?"
    cases = {
        "no_hits": Fakes(hits=[]),
        "below_threshold@0.1000": Fakes(hits=[hit(score=0.1)]),
        "model_refused@0.8000": Fakes(hits=[hit(content="가격은 무료입니다.")], output=model_output(answerable=False, answer="")),
        "citation_invalid@0.8000": Fakes(output=model_output(quote="원문에 없는 문장")),
        "unsupported_fact@0.8000": Fakes(output=model_output(answer="환불은 구매 후 99일 안에 신청합니다.")),
        "off_topic_quote@0.8000": Fakes(hits=[hit(content="가격은 무료입니다.")], output=model_output(quote="가격은 무료입니다.", answer="가격은 무료입니다.")),
        "unsupported_sentence@0.8000": Fakes(output=model_output(answer="다음 분기부터 학생 혜택이 별도 신청 없이 적용됩니다.")),
        "impolite@0.8000": Fakes(output=model_output(answer="환불은 구매 후 신청함.")),
        "answered@0.8000": Fakes(),
    }
    for step, fakes in cases.items():
        caplog.clear()
        with caplog.at_level(logging.INFO):
            response = fakes.ask()
        assert f"steps={step}" in caplog.text, step
        assert "steps" not in response.json(), "응답 본문은 바꾸지 않는다"

    # 복합 질문은 하위 질문 순서대로 남고 질문 원문은 남지 않는다.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        Fakes(hits=[hit(score=0.1)]).ask(question=question)
    assert "steps=below_threshold@0.1000,below_threshold@0.1000" in caplog.text
    assert "로그에남으면안되는" not in caplog.text

    # 모델 출력 오류도 사유를 남긴다.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert Fakes(output="JSON이 아님").ask().status_code == 502
    assert "steps=model_output_invalid" in caplog.text


def test_ac_nf_05_unverified_citation_fails_closed():
    good = {"chunk_id": "c1", "quote": QUOTE}
    cases = {
        "근거 후보에 없는 ID와 바꿔 옮긴 인용문": [{"chunk_id": "없는ID", "quote": "환불은 구매하고 14일 안에"}],
        "원문에 없는 인용문": [{"chunk_id": "c1", "quote": "환불은 90일까지 가능합니다"}],
        "인용 0개": [],
        "빈 인용문": [{"chunk_id": "c1", "quote": ""}],
        "공백뿐인 인용문": [{"chunk_id": "c1", "quote": " \n "}],
        "일부만 올바른 인용": [good, {"chunk_id": "c1", "quote": "지어낸 문장"}],
    }
    for name, citations in cases.items():
        output = json.dumps({"answerable": True, "answer": MODEL_ANSWER, "citations": citations})
        fakes = Fakes(chat=FakeChat(output))
        response = fakes.ask()
        assert response.json()["status"] == "not_found", name
        assert_not_found(response, 0.8)
