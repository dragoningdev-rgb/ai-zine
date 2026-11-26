"""Morning AI Zine 일일 발행 파이프라인 실행 진입점 (--dry-run 지원)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# `python pipeline/main.py` 형태로 직접 실행할 때도 프로젝트 루트를 찾을 수 있게 경로를 추가합니다.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline.build_data import compute_next_issue_no, publish_edition, update_radar
from pipeline.collect import (
    collect_all,
    fetch_github_trending_ai,
    fetch_major_bigtech_models,
    fetch_onprem_open_llms,
)
from pipeline.config import DATA_DIR
from pipeline.curate import curate_edition
from pipeline.schema import Edition
from pipeline.utils import write_json

KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("morning-ai-zine")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """명령줄 옵션(--date, --dry-run, --offline, --update-radar)을 해석합니다."""
    parser = argparse.ArgumentParser(description="Morning AI Zine daily editorial pipeline")
    parser.add_argument(
        "--date",
        default=datetime.now(KST).strftime("%Y-%m-%d"),
        help="발행 날짜 YYYY-MM-DD (기본값: 한국시간 오늘)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="web/data/를 건드리지 않고 임시 폴더에 검증된 YYYY-MM-DD.json만 생성합니다.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="외부 네트워크 수집을 건너뛰고 폴백 데이터로 실행합니다.",
    )
    parser.add_argument(
        "--update-radar",
        action="store_true",
        help="주간 GitHub AI 급상승 TOP 5도 함께 갱신합니다.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "pipeline" / ".dry_run",
        help="--dry-run 결과 JSON을 저장할 경로",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """수집 → 편집 → 스키마 검증 → 발행 순서로 전체 파이프라인을 실행합니다."""
    args = parse_args(argv)
    date_str: str = args.date

    logger.info("Starting Morning AI Zine pipeline for %s (dry_run=%s, offline=%s)", date_str, args.dry_run, args.offline)

    # 1단계: 다중 소스에서 최근 AI 기사·도구·토론 후보를 병렬 수집합니다.
    items = collect_all(offline=args.offline)
    logger.info("Collected %d deduplicated AI candidate items", len(items))

    # 2단계: LLM 편집장(실패 시 규칙 기반 폴백)으로 한국어 매거진 에디션을 생성합니다.
    issue_no = compute_next_issue_no(date_str, data_dir=DATA_DIR)
    edition = curate_edition(items, date_str=date_str, issue_no=issue_no)

    # 3단계: 파일에 쓰기 전 Pydantic 스키마 무결성을 최종 검증합니다.
    validated = Edition.model_validate(edition.model_dump())

    if args.dry_run:
        out_path = args.output_dir / f"{date_str}.json"
        write_json(out_path, validated.model_dump())
        logger.info("[DRY-RUN] Validated edition written to %s", out_path)
        return 0

    # 4단계: web/data/에 당일 에디션, 과월호 목록, 줍줍 아카이브를 발행합니다.
    publish_edition(validated, data_dir=DATA_DIR)

    if args.update_radar and not args.offline:
        gh_items = fetch_github_trending_ai()
        major_models = fetch_major_bigtech_models()
        onprem_models = fetch_onprem_open_llms()
        update_radar(
            gh_items,
            major_models=major_models,
            onprem_models=onprem_models,
            date_str=date_str,
            data_dir=DATA_DIR,
        )

    logger.info("Pipeline completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
