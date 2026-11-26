"""파이프라인 전역에서 공통으로 사용하는 날짜·텍스트·JSON 유틸리티 모음."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 자주 쓰이는 정규식은 미리 컴파일해 반복 호출 성능을 높입니다.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def is_valid_date(value: str) -> bool:
    """날짜 문자열이 YYYY-MM-DD 형식인지 검사합니다."""
    return bool(DATE_RE.match(value))


def strip_html(text: str) -> str:
    """HTML 태그와 과도한 공백을 제거해 순수 텍스트만 남깁니다."""
    cleaned = HTML_TAG_RE.sub(" ", text or "")
    return WHITESPACE_RE.sub(" ", cleaned).strip()


def truncate_text(text: str, max_len: int) -> str:
    """긴 문장을 최대 글자 수에 맞춰 자연스럽게 말줄임표(…)로 줄입니다."""
    cleaned = WHITESPACE_RE.sub(" ", (text or "").strip())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def read_json(path: Path) -> Any:
    """UTF-8 JSON 파일을 읽어 파이썬 객체로 반환합니다."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    """상위 폴더가 없으면 자동 생성한 뒤 보기 좋게 정렬된 UTF-8 JSON으로 저장합니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
