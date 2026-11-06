"""AC-AUTH: 호출자 인증. AUTH-06은 배포 환경에서만 의미가 있어 SMOKE_BASE_URL이 없으면 건너뛴다."""

import logging
import os
import re
import secrets
from pathlib import Path

import httpx
import pytest

from api import create_app
from config import ConfigError, load_settings
from helpers import API_KEY, ENV, HEADERS, Fakes

ROOT = Path(__file__).resolve().parent.parent
ASK = {"question": "환불 기간은?"}
UNAUTHORIZED = "UNAUTHORIZED"

deployed = pytest.mark.skipif(not os.environ.get("SMOKE_BASE_URL"), reason="SMOKE_BASE_URL 없음: 배포 후 실행")


def test_ac_auth_01_missing_or_wrong_api_key():
    fakes = Fakes()
    wrong_headers = [{}, {"X-API-Key": ""}, {"X-API-Key": API_KEY + "x"}, {"X-API-Key": "wrong"}]
    for headers in wrong_headers:
        response = fakes.client.post("/ask", json=ASK, headers=headers)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == UNAUTHORIZED
    assert fakes.external_calls == 0


def test_ac_auth_02_auth_checked_before_body():
    fakes = Fakes()
    for body in (b"{not json", b"{}", b'{"question": "   "}'):
        response = fakes.client.post(
            "/ask", content=body, headers={"X-API-Key": "wrong", "Content-Type": "application/json"}
        )
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == UNAUTHORIZED
    assert fakes.external_calls == 0


def test_ac_auth_03_constant_time_comparison(monkeypatch):
    compared = []
    original = secrets.compare_digest

    def spy(left, right):
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(secrets, "compare_digest", spy)
    fakes = Fakes()
    assert fakes.client.post("/ask", json=ASK, headers=HEADERS).status_code == 200
    assert fakes.client.post("/ask", json=ASK, headers={"X-API-Key": "wrong"}).status_code == 401
    assert compared == [(API_KEY.encode(), API_KEY.encode()), (b"wrong", API_KEY.encode())]


def test_ac_auth_04_unusable_key_fails_startup():
    bad_keys = (
        "",
        "   ",
        "a" * 31,
        "@Microsoft.KeyVault(VaultName=kv;SecretName=api-key)",
        "@microsoft.keyvault(SecretUri=https://kv.vault.azure.net/secrets/api-key)",
    )
    for bad in bad_keys:
        with pytest.raises(ConfigError, match="API_KEY") as error:
            create_app(env={**ENV, "API_KEY": bad})
        if bad.strip():
            assert bad not in str(error.value)

    assert load_settings({**ENV, "API_KEY": "a" * 32}).api_key == "a" * 32
    # 인덱싱은 API 키를 쓰지 않으므로 검사하지 않는다.
    assert load_settings({"LLM_PROVIDER": "ollama"}, for_api=False).api_key == ""


def test_ac_auth_05_key_not_exposed(caplog):
    caplog.set_level(logging.DEBUG)
    fakes = Fakes()
    responses_seen = [
        fakes.client.post("/ask", json=ASK, headers=HEADERS),
        fakes.client.post("/ask", json=ASK, headers={"X-API-Key": "wrong"}),
        fakes.client.post("/ask", content=b"{not json", headers=HEADERS),
    ]
    assert [response.status_code for response in responses_seen] == [200, 401, 422]
    for response in responses_seen:
        assert API_KEY not in response.text
        assert API_KEY not in str(dict(response.headers))
    assert API_KEY not in caplog.text

    # 소스에는 키 값 대신 변수 이름과 참조만 있어야 한다. "ollama"는 로컬 Ollama용 자리표시자이며 호출자 키가 아니다.
    literal_key = re.compile(r"""(?i)\bapi[_-]?key["']?\s*[:=]\s*["'](?![@$]|ollama["']|\s*["'])[^"']+["']""")
    files = [p for folder in ("src", "infra") for p in (ROOT / folder).rglob("*") if p.is_file()]
    files = [p for p in files if "__pycache__" not in p.parts]
    assert files
    violations = {str(p): found for p in files if (found := literal_key.findall(p.read_text(encoding="utf-8")))}
    assert violations == {}


@pytest.mark.deployed
@deployed
def test_ac_auth_06_deployed_requires_key():
    base_url = os.environ["SMOKE_BASE_URL"].rstrip("/")
    with httpx.Client(timeout=60) as client:
        assert client.post(f"{base_url}/ask", json=ASK).status_code == 401
        allowed = client.post(f"{base_url}/ask", json=ASK, headers={"X-API-Key": os.environ["SMOKE_API_KEY"]})
    assert allowed.status_code == 200, allowed.text
