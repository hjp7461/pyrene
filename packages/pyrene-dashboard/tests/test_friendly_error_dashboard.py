"""PRD-020 회귀 가드: friendly_error 헬퍼.

영문 raw exception 메시지 (httpx.ConnectError / ReadTimeout /
HTTPStatusError 401|403|404|5xx | fallback)를 사용자 언어 + 다음 행동
안내 + 원인 hint (HTTP status 또는 type name) 로 매핑하는지 검증.
"""

from __future__ import annotations

import httpx
import pytest

from pyrene_dashboard.api_client import friendly_error


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """헬퍼: 가짜 응답으로 HTTPStatusError 인스턴스 생성."""
    request = httpx.Request("GET", "http://localhost:8000/test")
    response = httpx.Response(status_code=status_code, request=request)
    return httpx.HTTPStatusError(
        f"server returned {status_code}", request=request, response=response
    )


def test_connect_error_maps_to_korean_friendly_dashboard() -> None:
    """httpx.ConnectError → "서버에 연결할 수 없습니다" + 원인 노출."""
    exc = httpx.ConnectError("connection refused")
    msg = friendly_error(exc, context="RBAC 매트릭스")
    assert "RBAC 매트릭스" in msg
    assert "서버에 연결할 수 없습니다" in msg
    assert "ConnectError" in msg


def test_read_timeout_maps_to_korean_friendly_dashboard() -> None:
    """httpx.ReadTimeout → "지연" 메시지."""
    exc = httpx.ReadTimeout("read timeout")
    msg = friendly_error(exc, context="감사 이벤트")
    assert "감사 이벤트" in msg
    assert "지연" in msg
    assert "ReadTimeout" in msg


def test_write_timeout_maps_to_korean_friendly_dashboard() -> None:
    """httpx.WriteTimeout → "지연" 메시지 (ReadTimeout과 동일 카테고리)."""
    exc = httpx.WriteTimeout("write timeout")
    msg = friendly_error(exc, context="사용량 레코드")
    assert "사용량 레코드" in msg
    assert "지연" in msg
    assert "WriteTimeout" in msg


def test_http_401_suggests_relogin_dashboard() -> None:
    """401 → 재로그인 안내 + HTTP status 노출."""
    exc = _make_http_status_error(401)
    msg = friendly_error(exc, context="인증")
    assert "인증" in msg
    assert "만료" in msg or "로그인" in msg
    assert "HTTP 401" in msg


def test_http_403_suggests_permission_request_dashboard() -> None:
    """403 → 관리자 권한 요청 안내."""
    exc = _make_http_status_error(403)
    msg = friendly_error(exc, context="데이터 권한")
    assert "데이터 권한" in msg
    assert "권한" in msg or "관리자" in msg
    assert "HTTP 403" in msg


def test_http_404_suggests_input_check_dashboard() -> None:
    """404 → 입력값 확인 안내."""
    exc = _make_http_status_error(404)
    msg = friendly_error(exc, context="트레이스 데이터")
    assert "트레이스 데이터" in msg
    assert "찾을 수 없" in msg or "확인" in msg
    assert "HTTP 404" in msg


def test_http_500_suggests_admin_contact_dashboard() -> None:
    """5xx → 관리자 호출 안내."""
    exc = _make_http_status_error(503)
    msg = friendly_error(exc, context="예산 차단 데이터")
    assert "예산 차단 데이터" in msg
    assert "서버 오류" in msg
    assert "관리자" in msg
    assert "HTTP 503" in msg


def test_http_4xx_other_suggests_input_check_dashboard() -> None:
    """4xx (401/403/404 외) → 입력값 확인 안내."""
    exc = _make_http_status_error(422)
    msg = friendly_error(exc, context="감사 타임라인")
    assert "감사 타임라인" in msg
    assert "처리할 수 없" in msg or "입력값" in msg
    assert "HTTP 422" in msg


def test_unknown_exception_falls_back_with_type_hint_dashboard() -> None:
    """매핑되지 않는 예외 → fallback 메시지 + 원인 타입 노출."""
    exc = RuntimeError("unexpected oops")
    msg = friendly_error(exc, context="RBAC 매트릭스")
    assert "RBAC 매트릭스" in msg
    assert "불러올 수 없습니다" in msg
    assert "RuntimeError" in msg


def test_context_default_is_data_dashboard() -> None:
    """context 미지정 → "데이터" 기본값."""
    exc = httpx.ConnectError("any")
    msg = friendly_error(exc)
    assert "데이터을(를)" in msg


@pytest.mark.parametrize(
    "status_code,expected_phrase",
    [
        (401, "재로그인 안내"),
        (403, "권한 요청 안내"),
        (404, "입력값 확인 안내"),
        (500, "관리자 호출 안내"),
        (502, "관리자 호출 안내"),
        (504, "관리자 호출 안내"),
    ],
)
def test_http_status_categories_complete_coverage_dashboard(
    status_code: int, expected_phrase: str
) -> None:
    """주요 HTTP status 카테고리가 모두 friendly 메시지로 매핑되는지 일괄 검증."""
    exc = _make_http_status_error(status_code)
    msg = friendly_error(exc, context="테스트")
    # 모든 케이스에서 한국어 메시지 + 원인 hint 가 포함되어야 한다
    assert "테스트" in msg
    assert any(
        keyword in msg
        for keyword in ("로그인", "권한", "확인", "관리자", "처리할 수 없")
    ), f"status={status_code} expected friendly phrase, got: {msg}"
    assert f"HTTP {status_code}" in msg
