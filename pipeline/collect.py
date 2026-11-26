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


def fetch_hf_trending_models(
    session: Optional[requests.Session] = None,
    limit: int = 80,
) -> Dict[str, List[Dict[str, Any]]]:
    """HuggingFace 공개 API에서 실시간 인기 텍스트 생성 LLM을 가져와 코딩·추론·로컬 부문 TOP 5로 분류합니다."""
    sess = session or _build_session()
    params = {
        "pipeline_tag": "text-generation",
        "sort": "trendingScore",
        "direction": "-1",
        "limit": limit,
    }
    try:
        resp = sess.get(HF_TRENDING_MODELS_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        models = resp.json()
    except Exception as exc:
        logger.warning("HuggingFace Trending Models API fetch failed: %s", exc)
        return {}

    if not isinstance(models, list) or not models:
        return {}

    coding: List[Dict[str, Any]] = []
    reasoning: List[Dict[str, Any]] = []
    local: List[Dict[str, Any]] = []

    def _to_entry(m: Dict[str, Any]) -> Dict[str, Any]:
        model_id = m.get("id") or ""
        parts = model_id.split("/", 1)
        provider = parts[0] if len(parts) == 2 else "Community"
        name = parts[1] if len(parts) == 2 else model_id
        likes = int(m.get("likes") or 0)
        trending = float(m.get("trendingScore") or likes)
        return {
            "id": model_id,
            "name": name,
            "provider": provider,
            "score": round(trending, 1),
            "likes": likes,
            "metric_label": f"♥ {likes:,}" if likes > 0 else f"트렌드 {int(trending)}",
            "url": f"https://huggingface.co/{model_id}",
        }

    for m in models:
        model_id = (m.get("id") or "").strip()
        if not model_id:
            continue
        tags = " ".join(m.get("tags") or []).lower()
        hay = f"{model_id.lower()} {tags}"
        entry = _to_entry(m)

        if any(k in hay for k in ("coder", "code", "codestral", "starcoder", "devstral", "swe")):
            if len(coding) < 5:
                coding.append(entry)
        elif any(k in hay for k in ("reason", "think", "r1", "qwq", "o1", "o3", "math", "distill")):
            if len(reasoning) < 5:
                reasoning.append(entry)
        elif any(k in hay for k in ("gguf", "mlx", "awq", "gptq", "4bit", "8bit", "bnb", "3b", "7b", "8b", "9b", "14b", "27b", "32b")):
            if len(local) < 5:
                local.append(entry)

    # 특정 부문이 5개 미만이면 전체 상위 트렌딩 모델로 자연스럽게 채웁니다.
    all_entries = [_to_entry(m) for m in models if (m.get("id") or "").strip()]
    for bucket in (coding, reasoning, local):
        seen_ids = {e["id"] for e in bucket}
        for cand in all_entries:
            if len(bucket) >= 5:
                break
            if cand["id"] not in seen_ids:
                bucket.append(cand)
                seen_ids.add(cand["id"])

    return {"coding": coding[:5], "reasoning": reasoning[:5], "local": local[:5]}


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
