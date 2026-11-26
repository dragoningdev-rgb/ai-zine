"""여러 AI 뉴스·커뮤니티·GitHub·HuggingFace 소스를 병렬 수집하고 본문 메타·댓글까지 보강하는 모듈."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import feedparser
except ImportError:  # pragma: no cover - feedparser 미설치 시 표준 XML 파서로 대체
    feedparser = None  # type: ignore[assignment]

import requests
from requests.adapters import HTTPAdapter

from pipeline.config import (
    AI_KEYWORDS,
    ARTICLE_META_TIMEOUT,
    GITHUB_SEARCH_REPOS_URL,
    HF_DAILY_PAPERS_URL,
    HF_TRENDING_MODELS_URL,
    HN_BEST_STORIES_URL,
    HN_ITEM_URL,
    HN_MAX_STORIES_TO_CHECK,
    HN_MIN_COMMENTS,
    HN_MIN_SCORE,
    HN_TOP_COMMENTS_LIMIT,
    HN_TOP_STORIES_URL,
    LOOKBACK_HOURS,
    MAX_WORKERS,
    OFFICIAL_OPEN_LLM_ORGS,
    OPENROUTER_MODELS_URL,
    REQUEST_TIMEOUT,
    RSS_SOURCES,
    TITLE_SIMILARITY_THRESHOLD,
    TOP_CANDIDATES_TO_ENRICH,
    USER_AGENT,
)
from pipeline.utils import WHITESPACE_RE, strip_html

logger = logging.getLogger(__name__)

# 추적용 쿼리 파라미터는 제거해 같은 기사의 URL을 하나로 통일합니다.
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "ref_src",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

# 반복 호출 성능을 위해 정규식과 AI 짧은 키워드 패턴을 미리 컴파일합니다.
TRAILING_SLASH_RE = re.compile(r"/+$")
BRACKET_TAG_RE = re.compile(r"\[[^\]]+\]")
NON_ALNUM_RE = re.compile(r"[^0-9a-z가-힣\s]+")
META_TAG_RE = re.compile(r"<meta\s+[^>]*>", flags=re.IGNORECASE)
META_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", flags=re.IGNORECASE)

SHORT_AI_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(kw)}\b")
    for kw in AI_KEYWORDS
    if len(kw) <= 3 and kw.isascii()
)
LONG_AI_KEYWORDS = tuple(
    kw for kw in AI_KEYWORDS if not (len(kw) <= 3 and kw.isascii())
)
# 구인·채용·단순 홍보 등 AI 뉴스 매거진에 맞지 않는 노이즈 제목을 걸러냅니다.
NOISE_TITLE_RE = re.compile(
    r"(who is hiring|we are hiring|now hiring|job opening|career advice|looking for a job|\[구인\]|\[채용\]|개발자 채용|이직 고민)",
    flags=re.IGNORECASE,
)


@dataclass
class RawItem:
    """수집 단계에서 통일된 형식으로 다루는 기사/도구 후보 1건."""

    title: str
    url: str
    source: str
    kind: str = "news"  # "news" | "community" | "tool" | "paper"
    summary: str = ""
    score: int = 0
    comments: int = 0
    published_at: Optional[datetime] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.published_at is not None:
            data["published_at"] = self.published_at.isoformat()
        return data


def normalize_url(url: str) -> str:
    """URL에서 추적 파라미터·해시·모바일 서브도메인·끝 슬래시를 제거하고 https로 표준화합니다."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = "https" if (parsed.scheme or "https").lower() in ("http", "https") else parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    for prefix in ("www.", "m.", "mobile."):
        if netloc.startswith(prefix):
            netloc = netloc[len(prefix) :]
            break
    path = TRAILING_SLASH_RE.sub("", parsed.path or "")
    filtered_query = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    )
    return urlunparse((scheme, netloc, path, "", urlencode(filtered_query), ""))


def normalize_title(title: str) -> str:
    """제목 유사도 비교를 위해 대괄호 태그·특수문자·여분 공백을 정리합니다."""
    cleaned = BRACKET_TAG_RE.sub(" ", title.lower())
    cleaned = NON_ALNUM_RE.sub(" ", cleaned)
    return WHITESPACE_RE.sub(" ", cleaned).strip()


def title_similarity(title_a: str, title_b: str) -> float:
    """두 제목이 얼마나 비슷한지 0.0~1.0 점수로 계산합니다."""
    na = normalize_title(title_a)
    nb = normalize_title(title_b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def is_ai_related(title: str, summary: str = "") -> bool:
    """채용·구직 노이즈를 먼저 제외한 뒤, 제목과 요약에 AI·LLM 핵심 키워드가 있는지 판별합니다."""
    if NOISE_TITLE_RE.search(title or ""):
        return False
    haystack = f"{title} {summary}".lower()
    if any(kw in haystack for kw in LONG_AI_KEYWORDS):
        return True
    return any(pattern.search(haystack) for pattern in SHORT_AI_PATTERNS)


def is_recent(
    published_at: Optional[datetime],
    now: Optional[datetime] = None,
    hours: int = LOOKBACK_HOURS,
) -> bool:
    """게시 시각이 최근 지정 시간(기본 24시간) 이내인지 확인합니다."""
    if published_at is None:
        return True
    ref = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return (ref - published_at) <= timedelta(hours=hours)


def compute_rank_score(item: RawItem) -> float:
    """추천 수·댓글 수·출처 가중치를 합산해 화제성 우선순위 점수를 매깁니다."""
    base = float(item.score) + float(item.comments) * 1.5
    source_bonus = {
        "GeekNews": 40.0,
        "Hacker News": 30.0,
        "GitHub Trending": 35.0,
        "HuggingFace Daily Papers": 25.0,
    }.get(item.source, 15.0)
    return base + source_bonus


def deduplicate_items(
    items: Iterable[RawItem],
    similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> List[RawItem]:
    """화제성 순으로 정렬한 뒤 중복 URL과 80% 이상 유사한 제목을 제거합니다."""
    ordered = sorted(items, key=compute_rank_score, reverse=True)
    kept: List[RawItem] = []
    seen_urls: set[str] = set()

    for item in ordered:
        canon_url = normalize_url(item.url)
        if not canon_url or canon_url in seen_urls:
            continue

        if any(title_similarity(item.title, existing.title) >= similarity_threshold for existing in kept):
            continue

        item.url = canon_url
        seen_urls.add(canon_url)
        kept.append(item)

    return kept


def extract_meta_description(html_text: str) -> str:
    """HTML head 영역에서 og:description, twitter:description, meta description을 추출합니다."""
    if not html_text:
        return ""
    found: Dict[str, str] = {}
    for tag_match in META_TAG_RE.finditer(html_text[:32768]):
        attrs: Dict[str, str] = {}
        for k, v1, v2 in META_ATTR_RE.findall(tag_match.group(0)):
            attrs[k.lower()] = html.unescape(v1 or v2).strip()
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if key in ("og:description", "twitter:description", "description") and content:
            found[key] = strip_html(content)

    for preferred in ("og:description", "description", "twitter:description"):
        if found.get(preferred):
            return found[preferred][:400]
    return ""


def fetch_article_meta_description(url: str, session: Optional[requests.Session] = None) -> str:
    """원문 페이지의 앞부분만 가볍게 읽어 요약용 메타 설명(og:description 등)을 가져옵니다."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    sess = session or _build_session()
    try:
        resp = sess.get(url, timeout=ARTICLE_META_TIMEOUT, stream=True)
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "xml" not in ctype and ctype != "":
            resp.close()
            return ""
        chunks: List[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=4096):
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= 32768:
                break
        resp.close()
        decoded = b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
        return extract_meta_description(decoded)
    except Exception:
        return ""


def fetch_hn_top_comments(
    kid_ids: List[int],
    session: Optional[requests.Session] = None,
    limit: int = HN_TOP_COMMENTS_LIMIT,
) -> List[str]:
    """Hacker News 상위 댓글 ID 목록에서 실제 개발자 댓글 3~5개를 추출합니다."""
    if not kid_ids:
        return []
    sess = session or _build_session()
    comments: List[str] = []
    for cid in kid_ids[: limit + 3]:
        if len(comments) >= limit:
            break
        try:
            resp = sess.get(HN_ITEM_URL.format(item_id=cid), timeout=ARTICLE_META_TIMEOUT)
            resp.raise_for_status()
            data = resp.json() or {}
            if data.get("deleted") or data.get("dead"):
                continue
            text = strip_html(html.unescape(data.get("text") or ""))
            if len(text) >= 25:
                comments.append(text[:320])
        except Exception:
            continue
    return comments


def enrich_candidates(items: List[RawItem], session: Optional[requests.Session] = None) -> List[RawItem]:
    """상위 후보 기사들에 원문 meta/og:description과 HN 상위 댓글을 병렬로 보강합니다."""
    sess = session or _build_session()
    targets = items[:TOP_CANDIDATES_TO_ENRICH]

    def _enrich_one(item: RawItem) -> None:
        # 1) 기사 원문 메타 설명 수집 (기존 요약이 짧거나 HN 기본 요약일 때 우선 보강)
        if not item.extra.get("meta_description") and item.kind in ("news", "community"):
            meta_desc = fetch_article_meta_description(item.url, session=sess)
            if meta_desc:
                item.extra["meta_description"] = meta_desc
                if not item.summary or item.summary.startswith("HN score "):
                    item.summary = meta_desc

        # 2) Hacker News 글이면 상위 댓글 3~5개를 추가로 수집해 쟁점 파악에 활용합니다.
        kid_ids = item.extra.get("hn_kids")
        if kid_ids and not item.extra.get("top_comments"):
            top_comments = fetch_hn_top_comments(kid_ids, session=sess, limit=HN_TOP_COMMENTS_LIMIT)
            if top_comments:
                item.extra["top_comments"] = top_comments

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        list(pool.map(_enrich_one, targets))

    return items


def _build_session() -> requests.Session:
    """병렬 HTTP 요청을 재사용 연결 풀로 빠르게 처리하는 세션을 생성합니다."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=MAX_WORKERS * 2, pool_maxsize=MAX_WORKERS * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _parse_entry_time(entry: Any) -> Optional[datetime]:
    """RSS 항목에서 발행 시각을 UTC datetime으로 안전하게 추출합니다."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _parse_rss_xml_fallback(content: bytes) -> List[Dict[str, Any]]:
    """feedparser가 없는 환경에서도 기본 RSS 2.0 XML을 해석하는 비상 파서입니다."""
    entries: List[Dict[str, Any]] = []
    root = ET.fromstring(content)
    for item in root.findall(".//item"):
        pub_raw = item.findtext("pubDate") or ""
        pub_dt = None
        if pub_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
            except Exception:
                pub_dt = None
        entries.append(
            {
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "summary": item.findtext("description") or "",
                "published_dt": pub_dt,
            }
        )
    return entries


def fetch_rss_source(source_cfg: Dict[str, Any], session: Optional[requests.Session] = None) -> List[RawItem]:
    """단일 RSS 피드를 내려받아 최근 AI 관련 항목만 걸러냅니다."""
    sess = session or _build_session()
    name = source_cfg["name"]
    url = source_cfg["url"]
    kind = source_cfg.get("kind", "news")
    always_ai = bool(source_cfg.get("always_ai", False))

    try:
        resp = sess.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if feedparser is not None:
            entries = feedparser.parse(resp.content).entries[:40]
        else:
            entries = _parse_rss_xml_fallback(resp.content)[:40]
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", name, exc)
        return []

    items: List[RawItem] = []
    for entry in entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = strip_html(entry.get("summary") or "")[:360]
        published = entry.get("published_dt") or _parse_entry_time(entry)

        if not title or not link or not is_recent(published):
            continue
        if not always_ai and not is_ai_related(title, summary):
            continue

        items.append(
            RawItem(
                title=title,
                url=link,
                source=name,
                kind=kind,
                summary=summary,
                score=50 if name == "GeekNews" else 30,
                comments=0,
                published_at=published,
            )
        )
    return items


def fetch_hacker_news(session: Optional[requests.Session] = None) -> List[RawItem]:
    """Hacker News 인기 글 중 점수 100+ 또는 댓글 50+인 AI 이슈를 병렬 수집합니다."""
    sess = session or _build_session()
    story_ids: List[int] = []
    for endpoint in (HN_TOP_STORIES_URL, HN_BEST_STORIES_URL):
        try:
            resp = sess.get(endpoint, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            ids = resp.json()
            if isinstance(ids, list):
                story_ids.extend(ids[:HN_MAX_STORIES_TO_CHECK])
        except Exception as exc:
            logger.warning("HN list fetch failed (%s): %s", endpoint, exc)

    unique_ids = list(dict.fromkeys(story_ids))[:HN_MAX_STORIES_TO_CHECK]
    if not unique_ids:
        return []

    def _fetch_one(item_id: int) -> Optional[RawItem]:
        try:
            resp = sess.get(HN_ITEM_URL.format(item_id=item_id), timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception:
            return None

        title = (data.get("title") or "").strip()
        url = (data.get("url") or f"https://news.ycombinator.com/item?id={item_id}").strip()
        score = int(data.get("score") or 0)
        comments = int(data.get("descendants") or 0)
        ts = data.get("time")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        kids = data.get("kids") if isinstance(data.get("kids"), list) else []

        if not title or (score < HN_MIN_SCORE and comments < HN_MIN_COMMENTS):
            return None
        if not is_recent(published, hours=48) or not is_ai_related(title):
            return None

        return RawItem(
            title=title,
            url=url,
            source="Hacker News",
            kind="community" if comments >= 80 else "news",
            summary=f"HN score {score}, {comments} comments",
            score=score,
            comments=comments,
            published_at=published,
            extra={
                "hn_url": f"https://news.ycombinator.com/item?id={item_id}",
                "hn_kids": kids[: HN_TOP_COMMENTS_LIMIT + 3],
            },
        )

    items: List[RawItem] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for fut in as_completed([pool.submit(_fetch_one, sid) for sid in unique_ids]):
            res = fut.result()
            if res is not None:
                items.append(res)
    return items


def fetch_github_trending_ai(session: Optional[requests.Session] = None, days: int = 7) -> List[RawItem]:
    """최근 1주일간 스타가 빠르게 늘어난 AI/LLM 오픈소스 저장소를 조회합니다."""
    sess = session or _build_session()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "q": f"(llm OR ai OR agent OR mcp) pushed:>={since} stars:>50",
        "sort": "stars",
        "order": "desc",
        "per_page": 10,
    }
    try:
        resp = sess.get(GITHUB_SEARCH_REPOS_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("GitHub API fetch failed: %s", exc)
        return []

    items: List[RawItem] = []
    for repo in payload.get("items", [])[:10]:
        full_name = repo.get("full_name") or ""
        html_url = repo.get("html_url") or ""
        desc = (repo.get("description") or "").strip()
        stars = int(repo.get("stargazers_count") or 0)
        if not full_name or not html_url:
            continue
        items.append(
            RawItem(
                title=f"{full_name}: {desc}" if desc else full_name,
                url=html_url,
                source="GitHub Trending",
                kind="tool",
                summary=desc[:260],
                score=min(stars, 5000),
                comments=int(repo.get("open_issues_count") or 0),
                extra={
                    "repo_name": full_name,
                    "stars": stars,
                    "language": repo.get("language") or "",
                },
            )
        )
    return items


def fetch_hf_daily_papers(session: Optional[requests.Session] = None) -> List[RawItem]:
    """HuggingFace Daily Papers에서 오늘 주목받는 AI 논문을 가져옵니다."""
    sess = session or _build_session()
    try:
        resp = sess.get(HF_DAILY_PAPERS_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        papers = resp.json()
    except Exception as exc:
        logger.warning("HuggingFace Daily Papers fetch failed: %s", exc)
        return []

    if not isinstance(papers, list):
        return []

    items: List[RawItem] = []
    for entry in papers[:10]:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id") or ""
        title = (paper.get("title") or entry.get("title") or "").strip()
        summary = (paper.get("summary") or "").strip().replace("\n", " ")[:320]
        upvotes = int(paper.get("upvotes") or 0)
        if not title or not paper_id:
            continue
        items.append(
            RawItem(
                title=title,
                url=f"https://huggingface.co/papers/{paper_id}",
                source="HuggingFace Daily Papers",
                kind="paper",
                summary=summary,
                score=upvotes * 2,
                comments=int(entry.get("numComments") or 0),
            )
        )
    return items


# 폐쇄망 오픈LLM 수집 시 개인 양자화·검열제거·병합·비LLM 파생 모델을 엄격히 차단하는 정규식
OPEN_LLM_EXCLUDE_RE = re.compile(
    r"(gguf|abliterated|uncensored|heretic|merged|merge|exl2|exl3|mlx|awq|gptq|bnb|4bit|8bit|fp4|lora|adapter|base$|-base\b|embed|rerank|reward|guard|audio|tts|asr|ocr|tokenizer|classifier)",
    flags=re.IGNORECASE,
)
PARAM_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")

# 5대 메이저 빅테크 OpenRouter prefix 매핑 (하드코딩된 모델명·임의 점수 없이 API에서 실시간 선별)
MAJOR_BIGTECH_PROVIDERS = (
    ("google/", "Google"),
    ("anthropic/", "Anthropic"),
    ("openai/", "OpenAI"),
    ("deepseek/", "DeepSeek"),
    ("x-ai/", "xAI"),
)

# OpenRouter 모델 중 경량/특수/비플래그십(음악·이미지·임베딩 등) 변형을 제외하고 주력 LLM 플래그십을 고르기 위한 필터
NON_FLAGSHIP_RE = re.compile(
    r"(:free|:beta|:extended|embed|whisper|tts|audio|image|vision|\bnano\b|\bmini\b|\blite\b|haiku|gemma|lyria|imagen|veo|moderation|search|online|realtime|guard|distill|r1-zero|prover|chat-v2|coder-v2|gpt-3\.5|flash-lite|flash-8b|\blive\b|preview-tts|safeguard|codex-mini)",
    flags=re.IGNORECASE,
)


def _format_context_window(ctx_len: int) -> str:
    if ctx_len >= 1_000_000:
        val = ctx_len / 1_000_000
        return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
    if ctx_len >= 1000:
        return f"{ctx_len // 1000}K"
    return str(ctx_len) if ctx_len > 0 else "—"


def _format_price_per_m(raw_price: Any) -> str:
    """OpenRouter 토큰당 단가 문자열을 1M(백만) 토큰당 달러 표기로 변환합니다."""
    try:
        per_token = float(raw_price)
    except (TypeError, ValueError):
        return "—"
    if per_token < 0:
        return "—"
    per_m = per_token * 1_000_000
    if per_m == 0:
        return "$0"
    if per_m < 1:
        return f"${per_m:.2f}"
    if per_m < 10:
        return f"${per_m:.2f}"
    return f"${per_m:.1f}"


def _clean_openrouter_name(raw_name: str, provider: str) -> str:
    """OpenRouter 모델명 앞의 'Google: ' 같은 중복 제공사 접두사를 제거합니다."""
    cleaned = (raw_name or "").strip()
    prefix = f"{provider}:"
    if cleaned.lower().startswith(prefix.lower()):
        cleaned = cleaned[len(prefix) :].strip()
    return cleaned


def fetch_major_bigtech_models(session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    """OpenRouter 공개 API에서 5대 빅테크별로 created 타임스탬프가 가장 최신인 플래그십 모델을 실시간 선별합니다."""
    sess = session or _build_session()
    try:
        resp = sess.get(OPENROUTER_MODELS_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        all_models = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("OpenRouter public API fetch failed: %s", exc)
        return []

    if not isinstance(all_models, list):
        return []

    selected: List[Dict[str, Any]] = []
    for prefix, provider_label in MAJOR_BIGTECH_PROVIDERS:
        lab_models = [
            m
            for m in all_models
            if str(m.get("id") or "").lower().startswith(prefix)
            and int(m.get("created") or 0) > 0
        ]
        if not lab_models:
            continue

        # 경량·특수 변형을 제외한 주력 플래그십 후보를 우선하고, 없으면 전체에서 최신 모델을 고릅니다.
        flagships = [
            m
            for m in lab_models
            if not NON_FLAGSHIP_RE.search(str(m.get("id") or ""))
            and float((m.get("pricing") or {}).get("completion") or 0) >= 0
        ]
        pool = flagships or lab_models

        # created(최신 출시순) 우선, 동일 출시일이면 컨텍스트/플래그십 단가 순으로 정렬
        def _flagship_sort_key(m: Dict[str, Any]) -> tuple:
            created = int(m.get("created") or 0)
            try:
                out_p = float((m.get("pricing") or {}).get("completion") or 0)
            except (TypeError, ValueError):
                out_p = 0.0
            ctx = int(m.get("context_length") or 0)
            return (created, out_p, ctx)

        best = max(pool, key=_flagship_sort_key)
        model_id = str(best.get("id") or "")
        created_ts = int(best.get("created") or 0)
        released_at = datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%d") if created_ts else None
        pricing = best.get("pricing") or {}

        selected.append(
            {
                "name": _clean_openrouter_name(str(best.get("name") or model_id), provider_label),
                "provider": provider_label,
                "context_window": _format_context_window(int(best.get("context_length") or 0)),
                "input_price": _format_price_per_m(pricing.get("prompt")),
                "output_price": _format_price_per_m(pricing.get("completion")),
                "released_at": released_at,
                "created_ts": created_ts,
                "url": f"https://openrouter.ai/{model_id}",
            }
        )

    # 가장 최근 출시된 플래그십 모델이 1위에 오도록 정렬합니다.
    selected.sort(key=lambda x: x.get("created_ts", 0), reverse=True)
    return selected[:5]


def _infer_license_label(org: str, tags: List[str]) -> str:
    """모델 태그와 공식 조직 정보를 바탕으로 라이선스 배지 문구를 추출합니다."""
    for tag in tags:
        if tag.lower().startswith("license:"):
            lic = tag.split(":", 1)[1].strip()
            mapping = {
                "apache-2.0": "Apache 2.0",
                "mit": "MIT",
                "gemma": "Gemma",
                "llama3.3": "Llama 3.3",
                "llama3.1": "Llama 3.1",
                "llama4": "Llama 4",
                "exaone": "EXAONE",
            }
            return mapping.get(lic.lower(), lic.upper() if len(lic) <= 5 else lic.title())
    org_defaults = {
        "qwen": "Apache 2.0",
        "deepseek-ai": "MIT",
        "mistralai": "Apache 2.0",
        "microsoft": "MIT",
        "google": "Gemma",
        "meta-llama": "Llama",
        "lgai-exaone": "EXAONE",
        "nvidia": "NVIDIA Open",
    }
    return org_defaults.get(org.lower(), "Open Weight")


def _extract_param_billions(model_name: str) -> Optional[float]:
    """모델명에서 파라미터 수(B 단위)를 추출합니다. 대형 MoE는 추정치를 반환합니다."""
    match = PARAM_SIZE_RE.search(model_name)
    if match:
        return float(match.group(1))
    lower = model_name.lower()
    if any(k in lower for k in ("deepseek-r1", "deepseek-v3", "deepseek-v4")):
        return 671.0
    if "llama-4-maverick" in lower:
        return 400.0
    if "llama-4-scout" in lower:
        return 109.0
    return None


def _estimate_params_vram(model_name: str) -> str:
    """모델명에서 파라미터 규모(예: 32B, 35B-A3B)를 읽어 예상 VRAM 배지를 계산합니다."""
    params_b = _extract_param_billions(model_name)
    if params_b is None:
        return "오픈웨이트 · vLLM 권장"
    if params_b >= 400:
        return f"{int(params_b)}B MoE · FP8 ~670GB"
    vram_gb = max(4, int(round(params_b * 2)))
    p_str = f"{int(params_b)}B" if params_b.is_integer() else f"{params_b}B"
    return f"{p_str} · VRAM ~{vram_gb}GB"


def _enterprise_size_tier(model_name: str) -> int:
    """B200 폐쇄망 실무 체급 우선순위: 2(14B~대형 MoE) > 1(7B~13B) > 0(7B 미만 초경량)."""
    params_b = _extract_param_billions(model_name)
    if params_b is None:
        return 1
    if params_b >= 14.0:
        return 2
    if params_b >= 7.0:
        return 1
    return 0


def is_official_onprem_model(model_id: str, tags: Optional[List[str]] = None) -> bool:
    """공식 오가니제이션 화이트리스트에 속하고 GGUF·abliterated·병합 모델이 아닌지 검사합니다."""
    if "/" not in (model_id or ""):
        return False
    org, name = model_id.split("/", 1)
    allowed_orgs = {o.lower() for o in OFFICIAL_OPEN_LLM_ORGS}
    if org.lower() not in allowed_orgs:
        return False
    hay = f"{name} {' '.join(tags or [])}"
    if OPEN_LLM_EXCLUDE_RE.search(hay):
        return False
    return True


def fetch_onprem_open_llms(
    session: Optional[requests.Session] = None,
    limit: int = 100,
    max_items: int = 8,
) -> List[Dict[str, Any]]:
    """HuggingFace API(text-generation + image-text-to-text)에서 공식 조직의 실무급(14B+ 우선) 폐쇄망 오픈LLM TOP 8을 수집합니다."""
    sess = session or _build_session()
    collected: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    # text-generation과 네이티브 멀티모달(image-text-to-text)을 함께 조회해 Qwen3.8·Gemma 등 플래그십 누락을 방지합니다.
    pipeline_tags = ("text-generation", "image-text-to-text")
    queries: List[Dict[str, Any]] = [
        {"pipeline_tag": ptag, "sort": "trendingScore", "direction": "-1", "limit": limit}
        for ptag in pipeline_tags
    ]
    for org in OFFICIAL_OPEN_LLM_ORGS:
        for ptag in pipeline_tags:
            queries.append(
                {"author": org, "pipeline_tag": ptag, "sort": "trendingScore", "direction": "-1", "limit": 8}
            )

    for params in queries:
        try:
            resp = sess.get(HF_TRENDING_MODELS_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            models = resp.json()
        except Exception as exc:
            logger.warning("HuggingFace on-prem query failed (%s/%s): %s", params.get("author", "trending"), params.get("pipeline_tag"), exc)
            continue

        if not isinstance(models, list):
            continue

        for m in models:
            model_id = (m.get("id") or "").strip()
            tags = [str(t) for t in (m.get("tags") or [])]
            if not model_id or model_id in seen_ids:
                continue
            if not is_official_onprem_model(model_id, tags):
                continue
            org, name = model_id.split("/", 1)
            likes = int(m.get("likes") or 0)
            trending = float(m.get("trendingScore") or 0.0)
            tier = _enterprise_size_tier(name)
            seen_ids.add(model_id)
            collected.append(
                {
                    "id": model_id,
                    "name": name,
                    "org": org,
                    "license": _infer_license_label(org, tags),
                    "params_vram": _estimate_params_vram(name),
                    "vllm_supported": True,
                    "trending_score": round(trending, 1),
                    "likes": likes,
                    "size_tier": tier,
                    "url": f"https://huggingface.co/{model_id}",
                }
            )

    # 7B 이상 실무급 모델이 충분하면 7B 미만 초경량(1B~4B)은 제외합니다.
    enterprise_pool = [item for item in collected if item["size_tier"] >= 1]
    pool = enterprise_pool if len(enterprise_pool) >= 5 else collected

    # 실무 체급(14B+ / MoE 우선) → 실시간 트렌딩 점수 → likes 순으로 정렬 후 조직별 대표 모델을 1개씩 먼저 선정합니다.
    pool.sort(key=lambda x: (x["size_tier"], x["trending_score"], x["likes"]), reverse=True)
    diversified: List[Dict[str, Any]] = []
    seen_orgs: set[str] = set()
    for item in pool:
        if len(diversified) >= max_items:
            break
        org_key = item["org"].lower()
        if org_key not in seen_orgs:
            diversified.append(item)
            seen_orgs.add(org_key)
    for item in pool:
        if len(diversified) >= max_items:
            break
        if item not in diversified:
            diversified.append(item)

    # 최종 노출 리스트는 실시간 인기도(trending_score) 기준으로 보기 좋게 정렬합니다.
    diversified.sort(key=lambda x: (x["trending_score"], x["size_tier"], x["likes"]), reverse=True)
    return diversified[:max_items]


def collect_all(offline: bool = False) -> List[RawItem]:
    """모든 소스를 병렬 수집·중복 제거한 뒤, 상위 후보의 원문 메타 설명과 HN 댓글을 보강합니다."""
    if offline:
        return []

    session = _build_session()
    results: List[RawItem] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        tasks = [pool.submit(fetch_rss_source, src, session) for src in RSS_SOURCES]
        tasks.append(pool.submit(fetch_hacker_news, session))
        tasks.append(pool.submit(fetch_github_trending_ai, session))
        tasks.append(pool.submit(fetch_hf_daily_papers, session))

        for fut in as_completed(tasks):
            try:
                results.extend(fut.result())
            except Exception as exc:
                logger.warning("Collector worker error: %s", exc)

    deduped = deduplicate_items(results)
    if deduped:
        enrich_candidates(deduped, session=session)
    return deduped
