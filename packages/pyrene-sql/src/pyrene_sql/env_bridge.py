"""Settings → os.environ 브릿지 (PRD-019 F-3 / PRD-033).

Pydantic AI 의 Anthropic/OpenAI provider 는 `os.getenv("ANTHROPIC_API_KEY")` 를
직접 호출한다. pydantic-settings 의 `Settings` 는 `.env` 를 자기 인스턴스로
로드하지만 `os.environ` 으로 export 하지 않는다. 따라서 *CLI / FastAPI 등의
진입점* 에서 명시적 브릿지가 필요하다.

`setdefault` 사용 — shell-exported 값이 있으면 우선권 보존, *.env*-only 사용자도
동작.

호출지점:
- `packages/pyrene-sql/src/pyrene_sql/cli.py:_run_ask` (PRD-019 F-3, 인라인)
- `deploy/api/app.py:_build` (PRD-033, 본 헬퍼 사용)
- 향후 신규 진입점 추가 시 같은 헬퍼 호출 권장.
"""

from __future__ import annotations

import os


def bridge_sql_settings_to_environ() -> None:
    """`pyrene_sql.settings.Settings` 의 `anthropic_api_key` 를 `os.environ` 으로 브릿지.

    Settings 인스턴스화 실패 (예: 필수 `PG_DSN` 미설정 환경) 시 조용히
    fallback — 호출자가 *shell-export 한 환경변수* 로 동작 가능. AuthSettings
    등 *다른 Settings 의 의무* 가 같은 DSN 의존하므로 *진짜* 실패는 그쪽에서
    raise.
    """
    try:
        from pyrene_sql.settings import Settings  # lazy import for circular safety
        settings = Settings()  # type: ignore[call-arg]
    except Exception:
        return
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)


__all__ = ["bridge_sql_settings_to_environ"]
