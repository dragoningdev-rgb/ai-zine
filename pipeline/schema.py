"""생성되는 JSON 데이터의 형식을 엄격하게 검사하는 Pydantic 스키마 정의."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from pipeline.utils import is_valid_date


# 1번 카드: 오늘의 히어로 빅뉴스 구조
class HeroNews(BaseModel):
    id: str = Field(..., min_length=1)
    category: str = Field(default="BIG NEWS", min_length=1)
    title: str = Field(..., min_length=1, max_length=80)
    summary: List[str] = Field(..., min_length=1, max_length=3)
    source: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


# 2번 카드: 핫 이슈 구조
class HotIssue(BaseModel):
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=100)
    summary: List[str] = Field(..., min_length=1, max_length=3)
    source: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


# 3번 카드: 오늘의 1줄 신조어 구조
class TermOfDay(BaseModel):
    id: str = Field(..., min_length=1)
    term: str = Field(..., min_length=1)
    analogy: str = Field(..., min_length=1)
    office_line: str = Field(..., min_length=1)


# 4번 카드: 오늘의 줍줍(무료 툴 또는 프롬프트) 구조
class DailyPick(BaseModel):
    id: str = Field(..., min_length=1)
    type: Literal["tool", "prompt"] = "tool"
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    copy_text: Optional[str] = None
    url: str = Field(..., min_length=1)


# 5번 카드: 점심시간 대화용 한 마디 구조
class LunchTalk(BaseModel):
    quote: str = Field(..., min_length=1)


# 오늘 탭에 들어가는 5장 카드 묶음
class BriefSection(BaseModel):
    hero_news: HeroNews
    hot_issue: HotIssue
    term_of_day: TermOfDay
    daily_pick: DailyPick
    lunch_talk: LunchTalk


# 불판 탭의 찬반 논쟁(옹호파 vs 회의파) 카드 구조
class BuzzItem(BaseModel):
    id: str = Field(..., min_length=1)
    tag: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    context: str = Field(..., min_length=1)
    pro_side: str = Field(..., min_length=1)
    con_side: str = Field(..., min_length=1)
    comment_count: int = Field(default=0, ge=0)
    source: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


# 하루치 에디션 전체 문서 구조 (editions/YYYY-MM-DD.json)
class Edition(BaseModel):
    date: str
    issue_no: int = Field(..., ge=1)
    read_time_min: int = Field(default=3, ge=1, le=15)
    brief: BriefSection
    buzz: List[BuzzItem] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, value: str) -> str:
        # 날짜가 YYYY-MM-DD 형식이 아니면 저장을 차단합니다.
        if not is_valid_date(value):
            raise ValueError(f"date must match YYYY-MM-DD, got: {value}")
        return value


# 줍줍 아카이브에 누적되는 개별 항목 구조
class PickArchiveItem(BaseModel):
    id: str = Field(..., min_length=1)
    date: str
    tag: Literal["#툴", "#프롬프트", "#신조어"]
    type: Literal["tool", "prompt", "term"]
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    copy_text: Optional[str] = None
    office_line: Optional[str] = None
    url: Optional[str] = None

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, value: str) -> str:
        if not is_valid_date(value):
            raise ValueError(f"date must match YYYY-MM-DD, got: {value}")
        return value


# 누적 줍줍 아카이브 전체 구조 (picks.json)
class PicksArchive(BaseModel):
    updated_at: str
    items: List[PickArchiveItem] = Field(default_factory=list)


# 판도 탭의 GitHub 급상승 저장소 1건 구조
class GithubTrendingRepo(BaseModel):
    rank: int = Field(..., ge=1, le=10)
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    weekly_stars: int = Field(..., ge=0)
    language: Optional[str] = None
    url: str = Field(..., min_length=1)


# 판도 탭 — 1) 메이저 빅테크 플래그십 모델 비교 항목
class MajorModelEntry(BaseModel):
    rank: int = Field(..., ge=1, le=10)
    name: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    overall: float = Field(..., ge=0)
    coding: float = Field(..., ge=0)
    reasoning: float = Field(..., ge=0)
    context_window: Optional[str] = None
    delta: Union[int, Literal["NEW"]] = 0
    url: Optional[str] = None


# 판도 탭 — 2) 사내 폐쇄망 반입 가능 공식 오픈웨이트 LLM 항목
class OnPremModelEntry(BaseModel):
    rank: int = Field(..., ge=1, le=10)
    name: str = Field(..., min_length=1)
    org: str = Field(..., min_length=1)
    license: str = Field(..., min_length=1)
    params_vram: str = Field(..., min_length=1)
    vllm_supported: bool = True
    likes: int = Field(default=0, ge=0)
    delta: Union[int, Literal["NEW"]] = 0
    url: Optional[str] = None


# 주간 판도 데이터 전체 구조 (radar.json)
class RadarData(BaseModel):
    updated_at: str
    week_label: Optional[str] = None
    source: Optional[str] = None
    github_trending_top5: List[GithubTrendingRepo] = Field(default_factory=list)
    major_models: List[MajorModelEntry] = Field(default_factory=list)
    onprem_models: List[OnPremModelEntry] = Field(default_factory=list)
