"""검증된 에디션·아카이브·판도 JSON을 web/data/ 디렉터리에 발행하는 모듈."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pipeline.collect import RawItem
from pipeline.config import DATA_DIR
from pipeline.schema import (
    Edition,
    GithubTrendingRepo,
    PickArchiveItem,
    PicksArchive,
    RadarData,
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


def update_radar(
    github_items: List[RawItem],
    date_str: Optional[str] = None,
    data_dir: Path = DATA_DIR,
) -> Optional[Path]:
    """주간 판도(radar.json)의 GitHub 급상승 TOP 5만 최신화하고 기존 모델 순위는 보존합니다."""
    radar_path = data_dir / "radar.json"
    if not radar_path.exists():
        logger.warning("radar.json does not exist; skipping weekly radar update.")
        return None

    try:
        existing = RadarData.model_validate(read_json(radar_path))
    except Exception as exc:
        logger.warning("Existing radar.json invalid: %s", exc)
        return None

    if len(github_items) >= 5:
        existing.github_trending_top5 = [
            GithubTrendingRepo(
                rank=rank,
                name=item.extra.get("repo_name") or item.title.split(":")[0].strip(),
                description=item.summary or item.title,
                weekly_stars=int(item.extra.get("stars") or item.score),
                language=item.extra.get("language") or None,
                url=item.url,
            )
            for rank, item in enumerate(github_items[:5], start=1)
        ]

    existing.updated_at = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    write_json(radar_path, existing.model_dump())
    return radar_path
