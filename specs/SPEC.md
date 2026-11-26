# SPEC: Morning AI Zine (출근길 3분 AI 매거진)

## 1. 프로젝트 목표 및 핵심 원칙
- **목표:** 매일 아침 07:00(KST) 최신 AI 뉴스·커뮤니티 논쟁·실전 팁·트렌드 랭킹을 자동 수집·편집하여, 스마트폰 출근길 3분 만에 완독할 수 있는 개인용 PWA 웹 매거진을 무인 운영한다.
- **운영 비용:** 0원 (GitHub Actions 무료 티어 + GitHub Pages 정적 호스팅 + 무료 RSS/공개 API + 무료 티어 LLM API).
- **설계 원칙:**
  1. **무백엔드 정적 아키텍처:** 런타임 DB나 API 서버 없이, 배치 파이프라인이 생성한 정적 JSON을 프론트엔드가 읽어 렌더링한다(CORS·서버 장애 원천 차단).
  2. **유한 피드(Finite Feed):** 1번 탭(오늘)은 정확히 5장 카드로 완결되어 "오늘치 끝"의 개운함을 제공한다.
  3. **장애 내성(Graceful Fallback):** LLM API 호출 실패나 특정 RSS 타임아웃 시에도 파이프라인이 중단되지 않고 규칙 기반 폴백 또는 직전 데이터 유지로 정상 빌드한다.

---

## 2. 시스템 아키텍처 및 데이터 흐름

```text
[GitHub Actions: 매일 06:30 KST / 매주 월 06:00 KST]
        │
        ▼
[1. Collector (pipeline/collect.py)]
  ├── 뉴스/썰 RSS: GeekNews, Hacker News(Top/Best), Reddit(r/LocalLLaMA, r/MachineLearning)
  ├── 데모/도구: GitHub Trending(AI/LLM), HuggingFace Daily Papers
  └── 랭킹/지표: GitHub API(주간 급상승 AI 레포), 공개 LLM 리더보드 스냅샷
        │
        ▼ (중복 제거 · 점수화 · 상위 후보 선별)
[2. Editor (pipeline/curate.py)]
  ├── LLM 편집장 호출 (Gemini API 무료 티어 / OpenAI 호환 API)
  │    - 한국어 3줄 매거진 문체 요약 + 옹호파 vs 회의파 양론 정리
  │    - 오늘의 1줄 신조어 비유 + 점심시간 대화용 한 마디 생성
  └── JSON Schema 검증 (실패 시 재시도 1회 → 규칙 기반 폴백)
        │
        ▼
[3. Publisher (pipeline/build_data.py)]
  ├── web/data/editions/YYYY-MM-DD.json  (당일 에디션 + 불판)
  ├── web/data/picks.json                (줍줍 아카이브 누적, 최신순)
  ├── web/data/radar.json                (주간 판도 랭킹, 월요일 갱신)
  └── web/data/index.json                (발행된 과월호 날짜 목록)
        │
        ▼ (Git Auto Commit & Push → GitHub Pages 자동 배포)
[4. Mobile PWA Viewer (web/index.html)]
  └── 스마트폰 홈 화면 아이콘으로 실행 · localStorage(읽음/스크랩/다크모드)
```

---

## 3. 디렉터리 구조

```text
morning-ai-zine/
├── .github/workflows/
│   ├── daily-edition.yml      # 매일 06:30 KST 수집·편집·커밋 자동화
│   └── deploy-pages.yml       # web/ 변경 시 GitHub Pages 배포
├── pipeline/
│   ├── config.py              # RSS 소스 목록, 필터 키워드, 모델 설정
│   ├── collect.py             # 병렬 수집기 (중복 URL/유사 제목 제거)
│   ├── curate.py              # LLM 편집 및 구조화 JSON 생성
│   ├── schema.py              # 데이터 스키마 검증기
│   └── main.py                # 파이프라인 엔트리포인트 (--dry-run 지원)
├── web/
│   ├── index.html             # 메인 앱 셸 (시맨틱 마크업)
│   ├── manifest.json          # PWA 홈 화면 추가 설정
│   ├── styles.css             # 모바일 퍼스트 에디토리얼 디자인 토큰
│   ├── app.js                 # 상태 관리, 라우팅(4탭), 과월호 전환, 스크랩
│   └── data/
│       ├── index.json         # 발행 호수 목록 ["2026-11-26", "2026-11-25", ...]
│       ├── picks.json         # 누적 줍줍 무기고
│       ├── radar.json         # 주간 판도 데이터
│       └── editions/
│           └── 2026-11-26.json
├── tests/
│   ├── test_collect.py        # 수집·정규화·중복 제거 단위 테스트
│   └── test_schema.py         # 생성 JSON 스키마 무결성 검증
├── specs/
│   └── SPEC.md                # 본 설계서
└── requirements.txt           # 경량 의존성 (feedparser, requests, pydantic 등)
```

---

## 4. 데이터 계약 (JSON Schema)

### 4-1. 일일 에디션 (`web/data/editions/YYYY-MM-DD.json`)

```json
{
  "date": "2026-11-26",
  "issue_no": 1,
  "read_time_min": 3,
  "brief": {
    "hero_news": {
      "id": "20261126-n1",
      "category": "BIG NEWS",
      "title": "헤드라인 (35자 이내)",
      "summary": ["핵심 1줄", "핵심 2줄", "핵심 3줄"],
      "source": "GeekNews",
      "url": "https://..."
    },
    "hot_issue": {
      "id": "20261126-n2",
      "title": "핫 이슈 제목",
      "summary": ["핵심 1줄", "핵심 2줄"],
      "source": "Hacker News",
      "url": "https://..."
    },
    "term_of_day": {
      "id": "20261126-t1",
      "term": "MCP (Model Context Protocol)",
      "analogy": "AI에게 외부 도구를 꽂아 쓰는 USB-C 표준 포트",
      "office_line": "회의실 멘트: 앞으로 사내 시스템 연동은 다 MCP 규격으로 통일되는 추세입니다."
    },
    "daily_pick": {
      "id": "20261126-p1",
      "type": "tool",
      "title": "도구 또는 프롬프트 제목",
      "description": "2줄 설명",
      "copy_text": "복붙용 프롬프트 또는 CLI 명령어 (선택)",
      "url": "https://..."
    },
    "lunch_talk": {
      "quote": "점심시간에 툭 던지기 좋은 1~2줄 업계 인사이트"
    }
  },
  "buzz": [
    {
      "id": "20261126-b1",
      "tag": "사고 실화 | 논쟁",
      "title": "이슈 제목",
      "context": "사건/논쟁 배경 2줄 요약",
      "pro_side": "옹호파/낙관론 핵심 논리 1~2줄",
      "con_side": "회의파/비판론 핵심 논리 1~2줄",
      "comment_count": 342,
      "source": "Hacker News",
      "url": "https://..."
    }
  ]
}
```

### 4-2. 누적 무기고 (`web/data/picks.json`) & 주간 판도 (`web/data/radar.json`)
- `picks.json`: 매일 발행된 `daily_pick`과 `term_of_day`를 중복 없이 상단에 누적 저장. 태그(`#툴`, `#프롬프트`, `#신조어`) 필터링 지원.
- `radar.json`: `updated_at`, `github_trending_top5`(이름, 1줄 설명, 주간 스타 증가수, URL), `model_rankings`(코딩/추론/로컬 부문별 1~5위 및 전주 대비 순위 변동 `delta`).

---

## 5. 핵심 기능 명세 (Requirements)

- **R1. [파이프라인] 다중 소스 수집 및 노이즈 제거**
  - GeekNews, Hacker News(점수 100+ 또는 댓글 50+), Reddit, GitHub Trending에서 최근 24시간 AI 관련 항목을 병렬 수집한다.
  - 동일 URL 및 제목 유사도 80% 이상 중복 항목을 제거한다.
- **R2. [파이프라인] LLM 편집 및 스키마 검증**
  - 수집된 원본 후보 중 가장 화제성 높은 항목을 선별해 4-1 스키마의 한국어 JSON으로 변환한다.
  - Pydantic 스키마 검증을 통과한 파일만 `web/data/`에 기록한다.
- **R3. [UI] 1번 탭 — 오늘 (Edition)**
  - 상단 헤더에 호수(`No.01`)와 날짜 선택 드롭다운(`11.26 목 ▾`)을 두어 과월호로 즉시 전환한다.
  - 정확히 5장 카드(히어로 빅뉴스 → 핫 이슈 → 1줄 신조어 플립 → 오늘의 줍줍 → 점심 한 마디)를 강약 있는 잡지 레이아웃으로 표시한다.
  - 5장을 모두 확인하면 하단에 "오늘치 3분 컷 완료 ☕" 완료 도장과 연속 출석(Streak) 카운터를 표시한다.
- **R4. [UI] 2번 탭 — 불판 (Buzz)**
  - 커뮤니티 댓글 폭발 이슈 및 사고 실화를 **'옹호파 vs 회의파' 2분할 VS 카드**로 시각화한다.
- **R5. [UI] 3번 탭 — 줍줍 (Archive)**
  - 누적된 무료 툴·프롬프트·신조어를 태그 칩 필터 + 실시간 키워드 검색으로 탐색한다.
  - 프롬프트/명령어 카드는 원터치 `[복사]` 버튼과 토스트 피드백을 제공한다.
- **R6. [UI] 4번 탭 — 판도 (Radar)**
  - 매주 월요일 갱신되는 GitHub AI 급상승 TOP 5와 부문별 모델 순위 변동(`▲`, `▼`, `NEW`)을 표시한다.
- **R7. [UI] 개인화 및 PWA 지원**
  - 우측 상단 아이콘으로 **스크랩 서랍**과 **다크/라이트 모드** 토글을 제공한다(하단 탭은 4개 유지).
  - `manifest.json`을 포함해 iOS Safari / Android Chrome에서 '홈 화면에 추가' 시 앱처럼 전체 화면으로 구동된다.

---

## 6. 범위 밖 (Out of Scope — v1 제외)
- 사용자 회원가입, 로그인, 백엔드 DB 동기화 (개인용 `localStorage`로 충분).
- 다중 사용자 투표/댓글 서버 기능.
- 유료 뉴스 매체 페이월 우회 크롤링.

---

## 7. 완료 기준 (Acceptance Criteria)
- **AC1 (수집 테스트):** `pytest tests/test_collect.py` 실행 시 100% 통과하며, 중복 URL이 0건이어야 한다.
- **AC2 (파이프라인 검증):** `python pipeline/main.py --dry-run` 실행 시 에러 없이 유효한 `YYYY-MM-DD.json`이 생성되고 `pytest tests/test_schema.py`를 통과해야 한다.
- **AC3 (폴백 내성):** LLM API 키가 없거나 잘못된 상태에서 `python pipeline/main.py --dry-run`을 실행해도 크래시 없이 폴백 에디션 JSON이 생성되어야 한다.
- **AC4 (프론트엔드 렌더링):** 로컬 정적 서버(`python -m http.server 8000 -d web`) 접속 시 콘솔 에러 0건으로 4개 탭, 과월호 전환, 스크랩 저장, 다크모드가 모두 정상 동작해야 한다.
- **AC5 (모바일 뷰포트):** 390×844(iPhone 기준) 뷰포트에서 가로 스크롤(overflow-x)이 전혀 발생하지 않아야 한다.
- **AC6 (자동화 배포):** GitHub Actions 워크플로 수동 트리거(`workflow_dispatch`) 시 데이터 생성 → 자동 커밋 → GitHub Pages 반영까지 무인 완료되어야 한다.

---

## 8. 단계별 구현 로드맵 (SDD 작업 분할)
에이전트에게 한 번에 다 시키지 않고 아래 4단계로 끊어 구현·검증한다.

1. **Phase 1 (데이터 계약 & 프론트엔드 v2):** `web/data/`에 샘플 에디션 2일치(오늘·어제)와 `picks.json`, `radar.json`을 먼저 만들고, `web/` PWA 앱(R3~R7)을 완성해 AC4·AC5를 검증한다.
2. **Phase 2 (수집기 구현):** `pipeline/config.py`, `collect.py`, `schema.py`와 단위 테스트를 구현해 AC1을 통과시킨다.
3. **Phase 3 (LLM 편집장 & 폴백):** `pipeline/curate.py`, `main.py`를 구현해 실제 RSS → 한국어 매거진 JSON 자동 생성 및 AC2·AC3를 검증한다.
4. **Phase 4 (GitHub Actions 무인화):** `.github/workflows/` 2종을 작성하고 GitHub Secrets(`GEMINI_API_KEY` 등)를 연동해 AC6을 최종 검증한다.
