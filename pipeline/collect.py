"""여러 AI 뉴스·커뮤니티·GitHub 소스를 병렬 수집하고 중복을 제거하는 모듈."""

from __future__ import annotations

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
    GITHUB_SEARCH_REPOS_URL,
    HF_DAILY_PAPERS_URL,
    HN_BEST_STORIES_URL,
    HN_ITEM_URL,
    HN_MAX_STORIES_TO_CHECK,
    HN_MIN_COMMENTS,
    HN_MIN_SCORE,
    HN_TOP_STORIES_URL,
    LOOKBACK_HOURS,
    MAX_WORKERS,
    REQUEST_TIMEOUT,
    RSS_SOURCES,
    TITLE_SIMILARITY_THRESHOLD,
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
SHORT_AI_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(kw)}\b")
    for kw in AI_KEYWORDS
    if len(kw) <= 3 and kw.isascii()
)
LONG_AI_KEYWORDS = tuple(
    kw for kw in AI_KEYWORDS if not (len(kw) <= 3 and kw.isascii())
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
    """URL에서 추적 파라미터·해시·끝 슬래시를 제거해 표준 주소로 정규화합니다."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
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
    """제목과 요약에 AI·LLM 관련 핵심 키워드가 포함되어 있는지 판별합니다."""
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
        summary = strip_html(entry.get("summary") or "")[:300]
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
            extra={"hn_url": f"https://news.ycombinator.com/item?id={item_id}"},
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
        summary = (paper.get("summary") or "").strip().replace("\n", " ")[:280]
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


def collect_all(offline: bool = False) -> List[RawItem]:
    """모든 소스를 병렬로 수집하고 중복을 제거한 최종 후보 목록을 반환합니다."""
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

    return deduplicate_items(results)
