"""Schema integrity tests for generated web/data JSON artifacts (AC2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.config import DATA_DIR, EDITIONS_DIR
from pipeline.schema import Edition, PicksArchive, RadarData


def test_index_json_valid_and_sorted() -> None:
    index_path = DATA_DIR / "index.json"
    assert index_path.exists(), "web/data/index.json is missing"

    dates = json.loads(index_path.read_text(encoding="utf-8"))
    assert isinstance(dates, list) and len(dates) >= 1
    for date_str in dates:
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", date_str)
        assert (EDITIONS_DIR / f"{date_str}.json").exists(), f"Missing edition for {date_str}"
    assert dates == sorted(dates, reverse=True)


def test_all_editions_match_schema() -> None:
    edition_files = sorted(EDITIONS_DIR.glob("*.json"))
    assert len(edition_files) >= 1, "No edition files found"

    for path in edition_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        edition = Edition.model_validate(payload)
        assert edition.date == path.stem
        assert 1 <= len(edition.brief.hero_news.summary) <= 3
        assert 1 <= len(edition.brief.hot_issue.summary) <= 3


def test_picks_archive_schema_and_unique_ids() -> None:
    picks_path = DATA_DIR / "picks.json"
    assert picks_path.exists(), "web/data/picks.json is missing"

    payload = json.loads(picks_path.read_text(encoding="utf-8"))
    archive = PicksArchive.model_validate(payload)
    ids = [item.id for item in archive.items]
    assert len(ids) == len(set(ids)), "Duplicate item IDs in picks.json"


def test_radar_json_schema() -> None:
    radar_path = DATA_DIR / "radar.json"
    assert radar_path.exists(), "web/data/radar.json is missing"

    payload = json.loads(radar_path.read_text(encoding="utf-8"))
    radar = RadarData.model_validate(payload)
    assert len(radar.github_trending_top5) == 5
    assert len(radar.model_rankings.coding) >= 1
    assert len(radar.model_rankings.reasoning) >= 1
    assert len(radar.model_rankings.local) >= 1


def test_invalid_edition_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        Edition.model_validate({"date": "2026/11/26", "issue_no": 0, "brief": {}})


def test_fallback_curate_and_publish_without_api_key(tmp_path: Path) -> None:
    """Verify AC3: missing/invalid API keys gracefully produce valid Edition + picks + index."""
    from pipeline.build_data import publish_edition
    from pipeline.collect import RawItem
    from pipeline.curate import curate_edition

    sample_items = [
        RawItem(
            title="Open-Weight Coding Model Hits New SOTA",
            url="https://example.com/sota",
            source="GeekNews",
            kind="news",
            summary="A lightweight 32B model matches frontier coding accuracy.",
            score=150,
            comments=60,
        ),
        RawItem(
            title="Agent Sandbox Debate on HN",
            url="https://news.ycombinator.com/item?id=999",
            source="Hacker News",
            kind="community",
            summary="Developers debate read-only defaults for coding agents.",
            score=210,
            comments=140,
        ),
    ]

    edition = curate_edition(
        sample_items,
        date_str="2026-11-27",
        issue_no=3,
        gemini_api_key="",
        openai_api_key="",
    )
    out_path = publish_edition(edition, data_dir=tmp_path)
    assert out_path.exists()

    saved_edition = Edition.model_validate(json.loads(out_path.read_text(encoding="utf-8")))
    assert saved_edition.date == "2026-11-27"
    assert saved_edition.issue_no == 3

    saved_picks = PicksArchive.model_validate(
        json.loads((tmp_path / "picks.json").read_text(encoding="utf-8"))
    )
    assert len(saved_picks.items) == 2

    saved_index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert saved_index == ["2026-11-27"]
