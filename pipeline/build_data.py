"""검증된 에디션·아카이브·판도 JSON을 web/data/ 디렉터리에 발행하는 모듈."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pipeline.collect import RawItem
from pipeline.config import DATA_DIR
from pipeline.curate import summarize_github_repos
from pipeline.schema import (
    Edition,
    GithubTrendingRepo,
    MajorModelEntry,
    OnPremModelEntry,
    PickArchiveItem,
    PicksArchive,
    RadarData,
    RepoDetailSummary,
)
from pipeline.utils import read_json, write_json

logger = logging.getLogger(__name__)


def load_existing_index(data_dir: Path = DATA_DIR) -> List[str]:
    """기존에 발행된 과월호 날짜 목록(index.json)을 불러옵니다."""
    index_path = data_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        dates = read_json(index_path)
        return [d for d in dates if isinstance(d, str)]
    except Exception:
        return []


def compute_next_issue_no(date_str: str, data_dir: Path = DATA_DIR) -> int:
    """같은 날짜 재발행이면 기존 호수를 유지하고, 새 날짜면 가장 큰 호수 + 1을 반환합니다."""
    existing_path = data_dir / "editions" / f"{date_str}.json"
    if existing_path.exists():
        try:
            payload = read_json(existing_path)
            if isinstance(payload.get("issue_no"), int) and payload["issue_no"] >= 1:
                return payload["issue_no"]
        except Exception:
            pass

    max_no = 0
    editions_dir = data_dir / "editions"
    if editions_dir.exists():
        for file_path in editions_dir.glob("*.json"):
            try:
                payload = read_json(file_path)
                max_no = max(max_no, int(payload.get("issue_no", 0)))
            except Exception:
                continue
    return max_no + 1


def publish_edition(edition: Edition, data_dir: Path = DATA_DIR) -> Path:
    """에디션 JSON을 저장하고 index.json과 picks.json을 함께 최신 상태로 갱신합니다."""
    validated = Edition.model_validate(edition.model_dump())

    edition_path = data_dir / "editions" / f"{validated.date}.json"
    write_json(edition_path, validated.model_dump())

    # 발행 날짜 목록을 중복 없이 최신순으로 정렬해 저장합니다.
    dates = set(load_existing_index(data_dir))
    dates.add(validated.date)
    write_json(data_dir / "index.json", sorted(dates, reverse=True))

    # 오늘의 줍줍과 1줄 신조어를 누적 아카이브 상단에 추가합니다.
    update_picks_archive(validated, data_dir=data_dir)

    logger.info("Published edition %s (No.%02d) to %s", validated.date, validated.issue_no, edition_path)
    return edition_path


def update_picks_archive(edition: Edition, data_dir: Path = DATA_DIR) -> Path:
    """오늘 발행된 줍줍·신조어를 중복 ID 없이 picks.json 맨 위에 누적합니다."""
    picks_path = data_dir / "picks.json"
    existing_items: List[PickArchiveItem] = []

    if picks_path.exists():
        try:
            archive = PicksArchive.model_validate(read_json(picks_path))
            existing_items = archive.items
        except Exception as exc:
            logger.warning("Failed to load existing picks.json: %s", exc)

    pick = edition.brief.daily_pick
    term = edition.brief.term_of_day

    new_entries = [
        PickArchiveItem(
            id=pick.id,
            date=edition.date,
            tag="#프롬프트" if pick.type == "prompt" else "#툴",
            type=pick.type,
            title=pick.title,
            description=pick.description,
            copy_text=pick.copy_text,
            url=pick.url,
        ),
        PickArchiveItem(
            id=term.id,
            date=edition.date,
            tag="#신조어",
            type="term",
            title=term.term,
            description=term.analogy,
            office_line=term.office_line,
        ),
    ]

    new_ids = {entry.id for entry in new_entries}
    merged = new_entries + [item for item in existing_items if item.id not in new_ids]

    archive = PicksArchive(updated_at=edition.date, items=merged)
    write_json(picks_path, archive.model_dump())
    return picks_path


def _format_week_label(date_str: str) -> str:
    """YYYY-MM-DD 날짜를 '2026년 11월 4주차' 형식 라벨로 바꿉니다."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        week_of_month = (dt.day - 1) // 7 + 1
        return f"{dt.year}년 {dt.month}월 {week_of_month}주차"
    except ValueError:
        return date_str


def _compute_delta(name: str, new_rank: int, prev_rank_map: Dict[str, int]) -> Union[int, str]:
    prev_rank = prev_rank_map.get(name.lower())
    return "NEW" if prev_rank is None else (prev_rank - new_rank)


def update_radar(
    github_items: List[RawItem],
    major_models: Optional[List[Dict[str, Any]]] = None,
    onprem_models: Optional[List[Dict[str, Any]]] = None,
    date_str: Optional[str] = None,
    data_dir: Path = DATA_DIR,
) -> Optional[Path]:
    """주간 판도(radar.json)의 GitHub TOP 5(한국어 번역), 메이저 빅테크, 폐쇄망 오픈LLM을 갱신합니다."""
    radar_path = data_dir / "radar.json"
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing: Optional[RadarData] = None
    if radar_path.exists():
        try:
            existing = RadarData.model_validate(read_json(radar_path))
        except Exception as exc:
            logger.warning("Existing radar.json invalid: %s", exc)

    if existing is None:
        existing = RadarData(
            updated_at=today,
            week_label=_format_week_label(today),
            source="GitHub Search API · OpenRouter · HuggingFace Official Orgs",
            github_trending_top5=[],
            major_models=[],
            onprem_models=[],
        )

    # 1) GitHub 급상승 TOP 5 갱신 + README 기반 한국어 1줄 요약 및 아코디언 상세 요약 생성
    if len(github_items) >= 5:
        raw_repos = [
            {
                "name": item.extra.get("repo_name") or item.title.split(":")[0].strip(),
                "description": item.summary or item.title,
                "readme_snippet": (item.extra.get("readme_snippet") or "")[:2000],
            }
            for item in github_items[:5]
        ]
        ko_map = summarize_github_repos(raw_repos)
        existing.github_trending_top5 = []
        for rank, item in enumerate(github_items[:5], start=1):
            rname = raw_repos[rank - 1]["name"]
            curated = ko_map.get(rname) or {}
            desc_ko = curated.get("description") or raw_repos[rank - 1]["description"]
            detail_raw = curated.get("detail_summary")
            detail_obj = RepoDetailSummary.model_validate(detail_raw) if detail_raw else None
            existing.github_trending_top5.append(
                GithubTrendingRepo(
                    rank=rank,
                    name=rname,
                    description=desc_ko,
                    detail_summary=detail_obj,
                    weekly_stars=int(item.extra.get("stars") or item.score),
                    language=item.extra.get("language") or None,
                    url=item.url,
                )
            )

    # 2) 메이저 빅테크 최신 플래그십 모델 비교 목록 갱신 (최대 10개, OpenRouter 실데이터)
    if major_models:
        prev_major = {m.name.lower(): m.rank for m in existing.major_models}
        existing.major_models = [
            MajorModelEntry(
                rank=rank,
                name=m["name"],
                provider=m["provider"],
                context_window=m["context_window"],
                input_price=m["input_price"],
                output_price=m["output_price"],
                released_at=m.get("released_at"),
                delta=_compute_delta(m["name"], rank, prev_major),  # type: ignore[arg-type]
                url=m.get("url"),
            )
            for rank, m in enumerate(major_models[:10], start=1)
        ]

    # 3) 사내 폐쇄망 반입 가능 공식 오픈웨이트 LLM TOP 10 갱신 (HuggingFace 공식 조직 실시간)
    if onprem_models:
        prev_onprem = {m.name.lower(): m.rank for m in existing.onprem_models}
        existing.onprem_models = [
            OnPremModelEntry(
                rank=rank,
                name=m["name"],
                org=m["org"],
                license=m["license"],
                params_vram=m["params_vram"],
                vllm_supported=bool(m.get("vllm_supported", True)),
                trending_score=float(m.get("trending_score") or 0.0),
                likes=int(m.get("likes") or 0),
                delta=_compute_delta(m["name"], rank, prev_onprem),  # type: ignore[arg-type]
                url=m.get("url"),
            )
            for rank, m in enumerate(onprem_models[:10], start=1)
        ]

    existing.updated_at = today
    existing.week_label = _format_week_label(today)
    existing.source = "GitHub Search API · OpenRouter · HuggingFace Official Orgs"
    write_json(radar_path, existing.model_dump())
    logger.info("Updated radar.json for %s", today)
    return radar_path
