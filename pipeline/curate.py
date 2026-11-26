"""LLM 편집장 호출(Gemini/OpenAI 호환), 100% 한국어 번역·요약, 상세 에러 로깅 및 폴백 모듈."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests
from pydantic import ValidationError

from pipeline.collect import RawItem
from pipeline.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    REQUEST_TIMEOUT,
)
from pipeline.schema import (
    BriefSection,
    BuzzItem,
    DailyPick,
    Edition,
    HeroNews,
    HotIssue,
    LunchTalk,
    TermOfDay,
)
from pipeline.utils import truncate_text

logger = logging.getLogger(__name__)

# LLM 응답에 코드블록(```json ... ```)이 섞여 들어올 때 JSON 본문만 추출하는 정규식
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", flags=re.DOTALL)

# API 장애 시에도 매일 다른 신조어를 보여주기 위한 내장 풀
FALLBACK_TERMS = [
    (
        "MCP (Model Context Protocol)",
        "AI 모델에 외부 도구와 사내 데이터를 꽂아 쓰는 USB-C 표준 포트",
        "회의실 멘트: 앞으로 사내 시스템 연동은 개별 플러그인 대신 MCP 규격으로 통일하는 추세입니다.",
    ),
    (
        "Context Engineering (컨텍스트 엔지니어링)",
        "모델에게 말투를 가르치는 대신, 꼭 필요한 정보만 손질해 도마에 올려주는 설계 기술",
        "회의실 멘트: 프롬프트 문구보다 에이전트에 넣을 문맥을 설계하는 컨텍스트 엔지니어링이 핵심입니다.",
    ),
    (
        "Test-Time Compute (추론 시간 확장)",
        "모델 크기를 키우는 대신 답하기 전에 몇 초 더 생각하게 만들어 정확도를 끌어올리는 방식",
        "회의실 멘트: 단순 질의는 경량 모델로, 복잡한 설계는 추론 시간 확장 모델로 라우팅하는 게 비용 효율적입니다.",
    ),
    (
        "Spec-Driven Development (SDD)",
        "코드를 먼저 짜지 않고 명확한 명세서와 완료 기준(AC)을 먼저 합의한 뒤 에이전트에 구현을 맡기는 방식",
        "회의실 멘트: AI 코딩 시대에는 코드 타이핑보다 명세서와 검증 기준 작성 역량이 생산성을 가릅니다.",
    ),
]

# API 장애 시 점심 한 마디 카드에 채울 내장 인사이트 풀
FALLBACK_LUNCH_QUOTES = [
    "모델 성능 2% 올리는 것보다, 에이전트가 실수했을 때 되돌리는 'Undo 버튼' 설계가 실무 경쟁력입니다.",
    "RAG가 죽었다는 말이 매달 나오지만, 프로덕션에서 제일 먼저 터지는 건 컨텍스트에 불필요한 문서를 쏟아부은 에이전트입니다.",
    "AI 도구가 넘쳐날수록 팀의 차이는 '무슨 모델을 쓰느냐'가 아니라 '우리 업무용 평가셋 30개를 갖고 있느냐'에서 갈립니다.",
]


class LLMAPIError(RuntimeError):
    """LLM API 호출 실패 시 HTTP 상태코드와 원인 메시지를 함께 전달하는 예외."""

    def __init__(self, provider: str, status_code: Optional[int], detail: str) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        code_str = f"HTTP {status_code}" if status_code is not None else "NETWORK_ERROR"
        super().__init__(f"[{provider}] {code_str}: {detail}")


def _mask_secret(text: str, secret: str) -> str:
    """로그에 API 키가 절대 노출되지 않도록 마스킹합니다."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def _extract_json_payload(raw_text: str) -> Any:
    """LLM 응답 텍스트에서 마크다운 코드블록을 벗겨내고 JSON 객체/배열로 파싱합니다."""
    text = raw_text.strip()
    fence = JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    else:
        obj_start, arr_start = text.find("{"), text.find("[")
        starts = [i for i in (obj_start, arr_start) if i != -1]
        if starts:
            start = min(starts)
            end_char = "}" if text[start] == "{" else "]"
            end = text.rfind(end_char)
            if end > start:
                text = text[start : end + 1]
    return json.loads(text)


def _build_editor_prompt(items: List[RawItem], date_str: str, issue_no: int) -> str:
    """기사 원문 메타 설명과 HN 상위 댓글을 함께 담아 100% 한국어·구체적 요약을 지시합니다."""
    candidates = [
        {
            "title": it.title,
            "source": it.source,
            "kind": it.kind,
            "url": it.url,
            "score": it.score,
            "comments": it.comments,
            "summary": it.summary[:320],
            "meta_description": (it.extra.get("meta_description") or "")[:360],
            "top_comments": (it.extra.get("top_comments") or [])[:5],
        }
        for it in items[:20]
    ]
    compact = date_str.replace("-", "")
    return f"""당신은 한국 IT 직장인을 위한 출근길 3분 AI 매거진 'Morning AI Zine'의 수석 편집장입니다.
아래 수집된 최신 AI 기사·커뮤니티·도구 후보(원문 메타 설명 및 Hacker News 상위 댓글 포함)를 읽고, 반드시 유효한 JSON 객체 1개만 출력하세요(설명·마크다운 금지).

[절대 준수 규칙 — 100% 한국어 & 구체성]
1. **100% 자연스러운 한국어 번역·작성:** 원문이 영어인 기사·레포·댓글이라도 웹앱에 노출되는 모든 `title`, `summary`, `context`, `pro_side`, `con_side`, `description`, `analogy`, `office_line`, `quote`는 반드시 자연스러운 한국어 문장으로 번역·작성하세요. (고유명사·기술 약어 예: MCP, vLLM, Qwen, SWE-bench, WebGPU만 영문 병기 허용. 영어 문장 그대로 노출 절대 금지!)
2. **뻔한 상투어 금지 / 구체적 팩트 중심:** "가장 높은 화제성을 기록했습니다", "실무 적용 가능성이 주목받고 있습니다", "원문에서 확인할 수 있습니다" 같은 공허한 문장은 절대 쓰지 마세요. 반드시 제공된 `meta_description`, `summary`, `top_comments`에서 구체적인 기술 구조·성능 수치·비용 변화·보안 이슈·실제 개발자 댓글의 핵심 논거를 뽑아 작성하세요.
3. **카드별 분량 및 구조:**
   - `date`: "{date_str}", `issue_no`: {issue_no}, `read_time_min`: 3
   - `hero_news`: 가장 파급력 큰 1건. `title`은 35자 이내 한국어 헤드라인, `summary`는 구체적 팩트 3줄 배열(1줄=무엇이 나왔는지, 2줄=핵심 기술/수치, 3줄=실무 영향).
   - `hot_issue`: 두 번째로 중요한 1건. `title`은 한국어, `summary`는 구체적 팩트 2줄 배열.
   - `term_of_day`: 오늘 기사에서 등장한 핵심 AI 용어 1개 + 비개발자도 이해하는 1줄 비유 + 회의실에서 바로 쓸 멘트.
   - `daily_pick`: 실무에 바로 써먹을 무료 도구(`"tool"`) 또는 프롬프트(`"prompt"`) 1개 + 한국어 2줄 설명 + 복붙 가능한 `copy_text`.
   - `lunch_talk.quote`: 점심시간에 동료에게 툭 던지기 좋은 1~2줄 업계 인사이트.
   - `buzz`: `top_comments`나 커뮤니티 논쟁이 뜨거운 이슈 2~3건. 실제 댓글 논지를 바탕으로 `context`(사건 배경 2줄), `pro_side`(옹호파 핵심 논리), `con_side`(회의파 핵심 논리)를 100% 한국어로 정리.
4. **식별자 및 출처:** id는 "{compact}-n1", "{compact}-n2", "{compact}-t1", "{compact}-p1", "{compact}-b1" 형식, `url`과 `source`는 반드시 후보 목록의 실제 값을 사용하세요.

[출력 JSON 스키마]
{{
  "date": "{date_str}",
  "issue_no": {issue_no},
  "read_time_min": 3,
  "brief": {{
    "hero_news": {{"id": "{compact}-n1", "category": "BIG NEWS", "title": "...", "summary": ["...", "...", "..."], "source": "...", "url": "https://..."}},
    "hot_issue": {{"id": "{compact}-n2", "title": "...", "summary": ["...", "..."], "source": "...", "url": "https://..."}},
    "term_of_day": {{"id": "{compact}-t1", "term": "...", "analogy": "...", "office_line": "회의실 멘트: ..."}},
    "daily_pick": {{"id": "{compact}-p1", "type": "tool", "title": "...", "description": "...", "copy_text": "...", "url": "https://..."}},
    "lunch_talk": {{"quote": "..."}}
  }},
  "buzz": [
    {{"id": "{compact}-b1", "tag": "논쟁", "title": "...", "context": "...", "pro_side": "...", "con_side": "...", "comment_count": 120, "source": "...", "url": "https://..."}}
  ]
}}

[수집 후보]
{json.dumps(candidates, ensure_ascii=False)}
"""


def _call_gemini(prompt: str, api_key: str, model: str) -> str:
    """Google Gemini REST API(v1beta)를 호출하고 실패 시 정확한 HTTP 상태코드·에러 본문을 기록합니다."""
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        resp = requests.post(
            endpoint,
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.35, "responseMimeType": "application/json"},
            },
            timeout=REQUEST_TIMEOUT * 3,
        )
    except requests.RequestException as exc:
        raise LLMAPIError("Gemini", None, _mask_secret(str(exc), api_key)) from exc

    if not resp.ok:
        body_preview = _mask_secret(resp.text[:500].replace("\n", " "), api_key)
        raise LLMAPIError("Gemini", resp.status_code, f"model={model} body={body_preview}")

    try:
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMAPIError("Gemini", resp.status_code, f"unexpected response structure: {resp.text[:300]}") from exc


def _call_openai_compat(prompt: str, api_key: str, base_url: str, model: str) -> str:
    """OpenAI 호환 Chat Completions API를 호출하고 실패 시 정확한 HTTP 상태코드·에러 본문을 기록합니다."""
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.35,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "You are a Korean AI magazine editor. Output valid JSON in Korean only."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=REQUEST_TIMEOUT * 3,
        )
    except requests.RequestException as exc:
        raise LLMAPIError("OpenAI", None, _mask_secret(str(exc), api_key)) from exc

    if not resp.ok:
        body_preview = _mask_secret(resp.text[:500].replace("\n", " "), api_key)
        raise LLMAPIError("OpenAI", resp.status_code, f"model={model} body={body_preview}")

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMAPIError("OpenAI", resp.status_code, f"unexpected response structure: {resp.text[:300]}") from exc


def translate_github_descriptions(
    repos: List[Dict[str, str]],
    gemini_api_key: Optional[str] = None,
    openai_api_key: Optional[str] = None,
) -> Dict[str, str]:
    """GitHub 급상승 저장소 TOP 5의 영문 1줄 설명을 자연스러운 한국어로 일괄 번역합니다."""
    g_key = GEMINI_API_KEY if gemini_api_key is None else gemini_api_key
    o_key = OPENAI_API_KEY if openai_api_key is None else openai_api_key
    if not repos or (not g_key and not o_key):
        return {}

    prompt = (
        "아래 GitHub AI 오픈소스 저장소 목록의 영문 설명을 각각 40자 이내의 자연스러운 한국어 1줄 설명으로 번역해 "
        "{\"repo_name\": \"한국어 설명\"} 형식의 JSON 객체만 출력하세요.\n"
        + json.dumps(repos, ensure_ascii=False)
    )
    try:
        raw = (
            _call_gemini(prompt, g_key, GEMINI_MODEL)
            if g_key
            else _call_openai_compat(prompt, o_key, OPENAI_BASE_URL, OPENAI_MODEL)
        )
        parsed = _extract_json_payload(raw)
        if isinstance(parsed, dict):
            return {str(k): truncate_text(str(v), 80) for k, v in parsed.items() if v}
    except Exception as exc:
        logger.warning("GitHub description translation failed (keeping originals): %s", exc)
    return {}


def build_fallback_edition(items: List[RawItem], date_str: str, issue_no: int) -> Edition:
    """LLM API가 없거나 오류가 나도 파이프라인이 멈추지 않도록 수집된 메타·댓글 기반으로 폴백 에디션을 조립합니다."""
    compact = date_str.replace("-", "")
    day_seed = sum(ord(ch) for ch in compact)

    news_items = [it for it in items if it.kind in ("news", "paper")] or items
    tool_items = [it for it in items if it.kind == "tool"]
    buzz_items = [it for it in items if it.kind == "community"] or items[2:5]

    hero_src = news_items[0] if len(news_items) >= 1 else RawItem(
        title="오픈웨이트 AI 모델과 에이전트 도구 생태계 주간 핵심 동향",
        url="https://news.hada.io/",
        source="GeekNews",
        summary="로컬 추론 효율을 높인 32B급 오픈웨이트 모델과 표준 MCP 연동 도구가 빠르게 확산되고 있습니다.",
    )
    hot_src = news_items[1] if len(news_items) >= 2 else RawItem(
        title="개발자 커뮤니티에서 주목받는 AI 워크플로 자동화 실전 사례",
        url="https://news.ycombinator.com/",
        source="Hacker News",
        summary="코드 리뷰, 테스트 생성, 문서 요약 등 반복 업무를 경량 파이프라인으로 자동화하는 사례가 늘고 있습니다.",
    )
    pick_src = tool_items[0] if tool_items else RawItem(
        title="repomix — 레포지토리를 LLM 입력용 단일 파일로 패킹",
        url="https://github.com/yamadashy/repomix",
        source="GitHub Trending",
        kind="tool",
        summary="프로젝트 핵심 소스만 깔끔한 마크다운으로 묶어 토큰 낭비를 줄여주는 무료 오픈소스 CLI입니다.",
        extra={"repo_name": "yamadashy/repomix"},
    )

    term, analogy, office_line = FALLBACK_TERMS[day_seed % len(FALLBACK_TERMS)]
    lunch_quote = FALLBACK_LUNCH_QUOTES[day_seed % len(FALLBACK_LUNCH_QUOTES)]

    hero_meta = hero_src.extra.get("meta_description") or hero_src.summary
    hero_comments = hero_src.extra.get("top_comments") or []
    hot_meta = hot_src.extra.get("meta_description") or hot_src.summary

    hero_lines = [
        truncate_text(hero_meta or hero_src.title, 110),
        truncate_text(hero_comments[0], 110) if hero_comments else f"{hero_src.source} 기준 추천 {hero_src.score}·댓글 {hero_src.comments}건이 모인 핵심 기술 이슈입니다.",
        "LLM API 폴백 모드로 생성된 요약이며, 상세 기술 배경은 원문 링크에서 확인할 수 있습니다.",
    ]
    hot_lines = [
        truncate_text(hot_meta or hot_src.title, 110),
        f"{hot_src.source}에서 추천 {hot_src.score}·댓글 {hot_src.comments}건과 함께 실전 적용 논의가 이어지고 있습니다.",
    ]

    repo_name = pick_src.extra.get("repo_name")
    copy_text = (
        f"git clone https://github.com/{repo_name}.git"
        if repo_name
        else 'npx repomix --style markdown --ignore "**/*.test.*"'
    )

    built_buzz: List[BuzzItem] = []
    for idx, b in enumerate(buzz_items[:3], start=1):
        b_comments = b.extra.get("top_comments") or []
        b_context = b.extra.get("meta_description") or b.summary or f"{b.source}에서 댓글 {b.comments}건이 달린 화제 논쟁입니다."
        built_buzz.append(
            BuzzItem(
                id=f"{compact}-b{idx}",
                tag="커뮤니티 논쟁",
                title=truncate_text(b.title, 70),
                context=truncate_text(b_context, 140),
                pro_side=truncate_text(b_comments[0], 120) if len(b_comments) >= 1 else "생산성과 개발 속도를 실질적으로 끌어올리는 전환점이라는 긍정적 평가가 이어집니다.",
                con_side=truncate_text(b_comments[1], 120) if len(b_comments) >= 2 else "보안·신뢰성·비용 검증이 충분히 끝나기 전 성급한 도입은 경계해야 한다는 신중론도 팽팽합니다.",
                comment_count=max(b.comments, 50),
                source=b.source,
                url=b.url,
            )
        )

    if not built_buzz:
        built_buzz.append(
            BuzzItem(
                id=f"{compact}-b1",
                tag="논쟁",
                title="AI 코딩 에이전트 자율 권한, 어디까지 허용할 것인가",
                context="테스트 자동 수정과 배포 스크립트 실행까지 에이전트에 맡기는 팀이 늘며 권한 경계가 쟁점이 됐습니다.",
                pro_side="명확한 샌드박스와 테스트 스위트만 갖추면 반복 작업을 크게 줄일 수 있습니다.",
                con_side="프로덕션·DB에 영향을 줄 수 있는 쓰기 권한은 반드시 사람 승인을 거쳐야 합니다.",
                comment_count=128,
                source="Hacker News",
                url="https://news.ycombinator.com/",
            )
        )

    return Edition(
        date=date_str,
        issue_no=issue_no,
        read_time_min=3,
        brief=BriefSection(
            hero_news=HeroNews(
                id=f"{compact}-n1",
                category="BIG NEWS",
                title=truncate_text(hero_src.title, 60),
                summary=hero_lines,
                source=hero_src.source,
                url=hero_src.url,
            ),
            hot_issue=HotIssue(
                id=f"{compact}-n2",
                title=truncate_text(hot_src.title, 70),
                summary=hot_lines,
                source=hot_src.source,
                url=hot_src.url,
            ),
            term_of_day=TermOfDay(
                id=f"{compact}-t1",
                term=term,
                analogy=analogy,
                office_line=office_line,
            ),
            daily_pick=DailyPick(
                id=f"{compact}-p1",
                type="tool",
                title=truncate_text(pick_src.title, 70),
                description=truncate_text(
                    pick_src.summary or "오늘 AI 오픈소스 생태계에서 주목받은 실전 도구입니다.",
                    140,
                ),
                copy_text=copy_text,
                url=pick_src.url,
            ),
            lunch_talk=LunchTalk(quote=lunch_quote),
        ),
        buzz=built_buzz,
    )


def curate_edition(
    items: List[RawItem],
    date_str: str,
    issue_no: int,
    gemini_api_key: Optional[str] = None,
    openai_api_key: Optional[str] = None,
) -> Edition:
    """LLM으로 100% 한국어 에디션을 생성(실패 시 1회 재시도)하고, 폴백 시 정확한 HTTP 상태코드·원인을 로깅합니다."""
    g_key = GEMINI_API_KEY if gemini_api_key is None else gemini_api_key
    o_key = OPENAI_API_KEY if openai_api_key is None else openai_api_key

    if not items:
        logger.warning("FALLBACK_REASON: collected 0 candidate items; switching to rule-based fallback.")
        return build_fallback_edition(items, date_str, issue_no)

    if not g_key and not o_key:
        logger.warning("FALLBACK_REASON: neither GEMINI_API_KEY nor OPENAI_API_KEY is set; switching to rule-based fallback.")
        return build_fallback_edition(items, date_str, issue_no)

    provider = f"Gemini({GEMINI_MODEL})" if g_key else f"OpenAI({OPENAI_MODEL})"
    prompt = _build_editor_prompt(items, date_str, issue_no)
    last_error = "unknown error"

    for attempt in range(1, 3):
        try:
            raw = (
                _call_gemini(prompt, g_key, GEMINI_MODEL)
                if g_key
                else _call_openai_compat(prompt, o_key, OPENAI_BASE_URL, OPENAI_MODEL)
            )
            parsed = _extract_json_payload(raw)
            parsed["date"] = date_str
            parsed["issue_no"] = issue_no
            edition = Edition.model_validate(parsed)
            logger.info("LLM curation succeeded via %s on attempt %d", provider, attempt)
            return edition
        except LLMAPIError as exc:
            last_error = str(exc)
            logger.error(
                "LLM API call failed on attempt %d/2 (%s, status=%s): %s",
                attempt,
                exc.provider,
                exc.status_code,
                exc.detail,
            )
        except (KeyError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.error("LLM output parsing/schema validation failed on attempt %d/2 (%s): %s", attempt, provider, last_error)

    logger.error(
        "FALLBACK_TRIGGERED: All LLM attempts failed via %s. Last error: %s. Generating rule-based fallback edition.",
        provider,
        last_error,
    )
    return build_fallback_edition(items, date_str, issue_no)
