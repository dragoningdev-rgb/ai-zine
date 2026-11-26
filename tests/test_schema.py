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


def test_gemini_http_error_logs_status_code_and_falls_back(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Gemini API error logs exact HTTP status & masks key while falling back cleanly."""
    from unittest.mock import MagicMock

    import requests

    from pipeline.collect import RawItem
    from pipeline.curate import curate_edition

    fake_resp = MagicMock()
    fake_resp.ok = False
    fake_resp.status_code = 429
    fake_resp.text = '{"error": {"code": 429, "message": "Quota exceeded for key SECRET_KEY_123"}}'
    monkeypatch.setattr(requests, "post", lambda *a, **kw: fake_resp)

    items = [RawItem(title="Test AI News", url="https://example.com/ai", source="GeekNews", summary="Detailed summary")]
    with caplog.at_level("ERROR"):
        edition = curate_edition(items, date_str="2026-11-27", issue_no=3, gemini_api_key="SECRET_KEY_123", openai_api_key="")

    assert edition.date == "2026-11-27"
    assert "status=429" in caplog.text
    assert "SECRET_KEY_123" not in caplog.text
    assert "***REDACTED***" in caplog.text


def test_update_radar_computes_real_deltas(tmp_path: Path) -> None:
    """Verify update_radar writes real HF models and computes week-over-week rank deltas."""
    from pipeline.build_data import update_radar
    from pipeline.collect import RawItem

    gh_items = [
        RawItem(title=f"org/repo-{i}", url=f"https://github.com/org/repo-{i}", source="GitHub Trending", kind="tool", summary=f"AI tool {i}", score=1000 - i * 10, extra={"repo_name": f"org/repo-{i}", "stars": 1000 - i * 10, "language": "Python"})
        for i in range(1, 6)
    ]
    hf_week1 = {
        "coding": [{"name": "ModelA", "provider": "OrgA", "score": 300, "metric_label": "♥ 300", "url": "https://huggingface.co/OrgA/ModelA"}],
        "reasoning": [{"name": "ModelR", "provider": "OrgR", "score": 500, "metric_label": "♥ 500", "url": "https://huggingface.co/OrgR/ModelR"}],
        "local": [{"name": "ModelL", "provider": "OrgL", "score": 200, "metric_label": "♥ 200", "url": "https://huggingface.co/OrgL/ModelL"}],
    }
    update_radar(gh_items, hf_models=hf_week1, date_str="2026-11-23", data_dir=tmp_path)

    hf_week2 = {
        "coding": [
            {"name": "ModelB", "provider": "OrgB", "score": 400, "metric_label": "♥ 400", "url": "https://huggingface.co/OrgB/ModelB"},
            {"name": "ModelA", "provider": "OrgA", "score": 320, "metric_label": "♥ 320", "url": "https://huggingface.co/OrgA/ModelA"},
        ],
        "reasoning": hf_week1["reasoning"],
        "local": hf_week1["local"],
    }
    radar_path = update_radar(gh_items, hf_models=hf_week2, date_str="2026-11-30", data_dir=tmp_path)
    assert radar_path is not None

    radar = RadarData.model_validate(json.loads(radar_path.read_text(encoding="utf-8")))
    assert radar.model_rankings.coding[0].name == "ModelB"
    assert radar.model_rankings.coding[0].delta == "NEW"
    assert radar.model_rankings.coding[1].name == "ModelA"
    assert radar.model_rankings.coding[1].delta == -1
