"""AC-RET: 검색."""

import json
from pathlib import Path

import responses

import rag
from helpers import HEADERS, SEARCH_URL, Fakes, azure_client, hit, search_json

ROOT = Path(__file__).resolve().parent.parent


def test_ac_ret_01_embeds_once_and_searches_with_k():
    fakes = Fakes()
    assert fakes.ask(question="  환불 기간은?  ", top_k=3).status_code == 200
    assert fakes.embedder.calls == [["환불 기간은?"]]
    assert fakes.store.calls == [([0.1, 0.1, 0.1], 3)]


def test_ac_ret_02_default_top_k_is_5():
    fakes = Fakes()
    fakes.ask()
    assert fakes.store.calls[0][1] == 5


def test_ac_ret_03_retrieved_sorted_and_limited():
    # 저장소가 순서를 어기고 k보다 많이 돌려줘도 응답은 지켜야 한다.
    hits = [hit("low", 0.70), hit("c1", 0.90), hit("mid", 0.80), hit("extra", 0.65)]
    body = Fakes(hits=hits).ask(top_k=3).json()
    assert [row["chunk_id"] for row in body["retrieved"]] == ["c1", "mid", "low"]
    assert all(set(row) == {"source", "chunk_id", "score"} for row in body["retrieved"])


def test_ac_ret_05_compound_question_searched_per_sub_question():
    fakes = Fakes()
    fakes.ask(question="자동 로그아웃 시간은 얼마이고, 직원은 몇 명인가요?", top_k=3)
    assert fakes.embedder.calls == [["자동 로그아웃 시간은 얼마인가요?"], ["직원은 몇 명인가요?"]]
    assert [k for _, k in fakes.store.calls] == [3, 3]

    expected = {
        "비밀번호는 몇 자 이상이어야 하고, 대표 이름은 무엇인가요?": ["비밀번호는 몇 자 이상이어야 하나요?", "대표 이름은 무엇인가요?"],
        "환불은 며칠 안에 돌아오고, 할인은 있나요?": ["환불은 며칠 안에 돌아오나요?", "할인은 있나요?"],
        "요금은 얼마인가요? 환불은 되나요?": ["요금은 얼마인가요?", "환불은 되나요?"],
        "요금은 얼마이고 직원은 몇 명인가요?": ["요금은 얼마인가요?", "직원은 몇 명인가요?"],
    }
    for question, parts in expected.items():
        assert rag.split_question(question) == parts, question

    # 나누면 안 되는 질문은 그대로 둔다.
    for question in (
        "프로 요금제를 결제하고 20일이 지났으면 환불받을 수 있나요?",
        "메모를 공유하고 싶은데 기본 권한은 무엇인가요?",
        "무료 요금제와 프로 요금제의 용량은 각각 얼마인가요?",
        "A이고, B이고, C이고, D는 무엇인가요?",
    ):
        assert rag.split_question(question) == [question], question

    # 보정과 검증에 쓰는 단일 질문은 나뉘지 않아야 한다.
    questions = json.loads((ROOT / "data" / "calibration_questions.json").read_text(encoding="utf-8"))
    for group in questions.values():
        for items in group.values():
            assert all(rag.split_question(question) == [question] for question in items)


@responses.activate
def test_ac_ret_04_vector_only_query(monkeypatch):
    responses.add(responses.POST, SEARCH_URL, json=search_json())
    response = azure_client(monkeypatch).post("/ask", json={"question": "환불 기간은?", "top_k": 4}, headers=HEADERS)
    assert response.status_code == 200, response.text

    sent = json.loads(responses.calls[0].request.body)
    assert not sent.get("search"), "키워드 질의가 있으면 점수가 RRF로 바뀌어 임계치를 쓸 수 없다"
    assert len(sent["vectorQueries"]) == 1
    assert sent["vectorQueries"][0]["kind"] == "vector"
    assert sent["vectorQueries"][0]["k"] == 4
    assert sent["vectorQueries"][0]["fields"] == "content_vector"
    assert sent["vectorQueries"][0]["vector"] == [0.1, 0.2]
