/* Morning AI Zine — 클라이언트 앱 (무백엔드 정적 PWA) */
(() => {
  // 공통 유틸리티(web/lib/utils.js)에서 자주 쓰는 도우미 함수를 가져옵니다.
  const { loadJSON, saveJSON, escapeHTML, formatDateLabel, fetchJSON, copyText, debounce } =
    window.ZineUtils;

  // 브라우저 저장소(localStorage)에 기록할 항목 이름 목록입니다.
  const STORAGE_KEYS = {
    THEME: "ai_zine_theme",
    SCRAPS: "ai_zine_scraps_v1",
    READ_CARDS: "ai_zine_read_cards_v1",
    COMPLETED_DATES: "ai_zine_completed_dates_v1",
  };

  // 한 번 불러온 날짜별 에디션을 메모리에 저장해 과월호 전환 속도를 높입니다.
  const editionCache = new Map();

  // 앱 전체의 현재 화면 상태와 데이터를 한곳에서 관리합니다.
  const state = {
    activeTab: "today",
    dates: [],
    currentDate: null,
    edition: null,
    picks: [],
    picksTag: "ALL",
    picksQuery: "",
    radar: null,
    radarCategory: "coding",
    scraps: loadJSON(STORAGE_KEYS.SCRAPS, []),
    readCardsByDate: loadJSON(STORAGE_KEYS.READ_CARDS, {}),
    completedDates: loadJSON(STORAGE_KEYS.COMPLETED_DATES, []),
    termFlipped: false,
  };

  let toastTimer = null;

  // 화면 하단에 짧은 안내 메시지(토스트)를 2.2초간 띄웁니다.
  function showToast(msg) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.hidden = true;
    }, 2200);
  }

  // 클립보드 복사 후 결과에 따라 사용자에게 토스트로 알려줍니다.
  async function handleCopy(text, successLabel = "클립보드에 복사했어요") {
    const ok = await copyText(text);
    showToast(ok ? successLabel : "복사에 실패했어요. 직접 선택해 주세요.");
  }

  /* ---------------- 공통 UI 조각 렌더러 (중복 마크업 제거) ---------------- */

  // 프롬프트 또는 CLI 명령어 복사 박스 HTML을 생성합니다.
  function renderCopyBox(type, copyTextValue) {
    if (!copyTextValue) return "";
    const label = type === "prompt" ? "Prompt" : "Command";
    return `
      <div class="copy-box">
        <div class="copy-box-head">
          <span>${label}</span>
          <button type="button" class="copy-btn" data-copy-raw="${escapeHTML(copyTextValue)}">복사</button>
        </div>
        <pre class="copy-code">${escapeHTML(copyTextValue)}</pre>
      </div>`;
  }

  // 저장(스크랩) 버튼 HTML을 공통 형식으로 생성합니다.
  function renderScrapButton(itemId, dataAttr, dataValue) {
    const saved = isScrapped(itemId);
    return `
      <button type="button" class="pill-btn ${saved ? "saved" : ""}" ${dataAttr}="${escapeHTML(dataValue)}">
        ${saved ? "저장됨 ★" : "저장"}
      </button>`;
  }

  // 카드 읽음 확인 버튼 HTML을 공통 형식으로 생성합니다.
  function renderReadButton(readSet, cardKey) {
    const checked = readSet.has(cardKey);
    return `
      <button type="button" class="pill-btn ${checked ? "checked" : ""}" data-read-card="${cardKey}">
        ${checked ? "읽음 ✓" : "확인"}
      </button>`;
  }

  /* ---------------- 테마 (라이트 / 다크 모드) ---------------- */

  // 저장된 테마 설정이나 기기 시스템 설정에 맞춰 초기 테마를 적용합니다.
  function initTheme() {
    const saved = localStorage.getItem(STORAGE_KEYS.THEME);
    const prefersDark =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(saved || (prefersDark ? "dark" : "light"));
  }

  // 선택한 테마(light/dark)를 문서와 상태바 색상에 반영하고 저장합니다.
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(STORAGE_KEYS.THEME, theme);
    const isDark = theme === "dark";
    const moon = document.getElementById("icon-moon");
    const sun = document.getElementById("icon-sun");
    if (moon) moon.hidden = isDark;
    if (sun) sun.hidden = !isDark;
    const metaTheme = document.getElementById("meta-theme-color");
    if (metaTheme) metaTheme.setAttribute("content", isDark ? "#121214" : "#F7F5F0");
  }

  /* ---------------- 스크랩 서랍 ---------------- */

  // 특정 ID의 항목이 이미 스크랩되어 있는지 확인합니다.
  function isScrapped(id) {
    return state.scraps.some((s) => s.id === id);
  }

  // 항목을 스크랩 서랍에 추가하거나 이미 있으면 제거합니다.
  function toggleScrap(item) {
    const idx = state.scraps.findIndex((s) => s.id === item.id);
    if (idx >= 0) {
      state.scraps.splice(idx, 1);
      showToast("스크랩에서 제거했어요");
    } else {
      state.scraps.unshift({
        id: item.id,
        kind: item.kind || "스크랩",
        title: item.title,
        subtitle: item.subtitle || "",
        url: item.url || "",
        copy_text: item.copy_text || "",
        saved_at: new Date().toISOString().slice(0, 10),
      });
      showToast("스크랩 서랍에 담았어요");
    }
    saveJSON(STORAGE_KEYS.SCRAPS, state.scraps);
    updateScrapBadge();
    renderScrapDrawer();
    renderActiveTab();
  }

  // 헤더 우측 스크랩 아이콘 위의 저장 개수 배지를 갱신합니다.
  function updateScrapBadge() {
    const badge = document.getElementById("scrap-count");
    if (!badge) return;
    const count = state.scraps.length;
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }

  // 스크랩 서랍을 열고 뒤쪽 배경 스크롤을 잠시 잠급니다.
  function openScrapDrawer() {
    renderScrapDrawer();
    document.getElementById("scrap-backdrop").hidden = false;
    document.getElementById("scrap-drawer").hidden = false;
    document.body.style.overflow = "hidden";
  }

  // 스크랩 서랍을 닫고 배경 스크롤을 원래대로 복원합니다.
  function closeScrapDrawer() {
    document.getElementById("scrap-backdrop").hidden = true;
    document.getElementById("scrap-drawer").hidden = true;
    document.body.style.overflow = "";
  }

  // 스크랩 서랍 안에 저장된 항목 목록을 그립니다.
  function renderScrapDrawer() {
    const list = document.getElementById("scrap-list");
    const sub = document.getElementById("drawer-sub");
    if (!list || !sub) return;
    sub.textContent = `저장한 항목 ${state.scraps.length}개`;

    if (state.scraps.length === 0) {
      list.innerHTML = `<div class="empty-state">아직 스크랩한 항목이 없어요.<br/>카드 우측 하단의 <strong>저장</strong>을 눌러 모아보세요.</div>`;
      return;
    }

    list.innerHTML = state.scraps
      .map(
        (item) => `
      <article class="scrap-item">
        <div class="scrap-item-head">
          <span class="kicker-pill neutral">${escapeHTML(item.kind)}</span>
          <button type="button" class="pill-btn" data-remove-scrap="${escapeHTML(item.id)}">삭제</button>
        </div>
        <h3 class="scrap-item-title">${escapeHTML(item.title)}</h3>
        ${item.subtitle ? `<p class="scrap-item-sub">${escapeHTML(item.subtitle)}</p>` : ""}
        <div class="card-footer" style="margin-top:10px;padding-top:8px;">
          ${
            item.url
              ? `<a class="source-link" href="${escapeHTML(item.url)}" target="_blank" rel="noopener noreferrer">원문 보기 ↗</a>`
              : `<span class="source-link">${escapeHTML(item.saved_at)}</span>`
          }
          ${
            item.copy_text
              ? `<button type="button" class="pill-btn" data-copy-raw="${escapeHTML(item.copy_text)}">복사</button>`
              : ""
          }
        </div>
      </article>`
      )
      .join("");
  }

  /* ---------------- 완독 진행률 & 연속 출석(Streak) ---------------- */

  // 특정 날짜에 읽음 표시한 카드 ID 집합을 돌려줍니다.
  function getReadSet(dateStr) {
    return new Set(state.readCardsByDate[dateStr] || []);
  }

  // 카드 1장을 읽음 처리하고, 5장을 모두 읽으면 해당 날짜를 완독 기록에 추가합니다.
  function markCardRead(dateStr, cardKey) {
    if (!dateStr) return;
    const set = getReadSet(dateStr);
    const prevSize = set.size;
    set.add(cardKey);
    if (set.size === prevSize) return;
    state.readCardsByDate[dateStr] = Array.from(set);
    saveJSON(STORAGE_KEYS.READ_CARDS, state.readCardsByDate);
    if (set.size >= 5) {
      markDateCompleted(dateStr);
    }
    updateReadProgressUI();
  }

  // 오늘의 5장 카드를 한 번에 모두 완독 처리합니다.
  function markAllCardsRead(dateStr) {
    if (!dateStr) return;
    state.readCardsByDate[dateStr] = ["c1", "c2", "c3", "c4", "c5"];
    saveJSON(STORAGE_KEYS.READ_CARDS, state.readCardsByDate);
    markDateCompleted(dateStr);
    updateReadProgressUI();
    renderToday();
    showToast("오늘치 3분 컷 완료 ☕");
  }

  // 완독한 날짜를 중복 없이 최신순으로 저장합니다.
  function markDateCompleted(dateStr) {
    if (!state.completedDates.includes(dateStr)) {
      state.completedDates.push(dateStr);
      state.completedDates.sort().reverse();
      saveJSON(STORAGE_KEYS.COMPLETED_DATES, state.completedDates);
    }
  }

  // 가장 최근 완독일부터 하루씩 거슬러 올라가며 연속 출석 일수를 계산합니다.
  function computeStreak() {
    if (state.completedDates.length === 0) return 0;
    const sorted = Array.from(new Set(state.completedDates)).sort().reverse();
    let streak = 1;
    for (let i = 1; i < sorted.length; i++) {
      const prev = new Date(sorted[i - 1] + "T00:00:00");
      const curr = new Date(sorted[i] + "T00:00:00");
      if (Math.round((prev - curr) / 86400000) === 1) {
        streak += 1;
      } else {
        break;
      }
    }
    return streak;
  }

  // 상단 진행률 바(0~5장)를 현재 읽음 상태에 맞게 갱신합니다.
  function updateReadProgressUI() {
    const wrap = document.getElementById("read-progress-wrap");
    const fill = document.getElementById("read-progress-fill");
    const label = document.getElementById("read-progress-label");
    if (!wrap || !fill || !label) return;
    wrap.hidden = state.activeTab !== "today";
    const count = Math.min(5, getReadSet(state.currentDate).size);
    const readMin = state.edition?.read_time_min ?? 3;
    fill.style.width = `${(count / 5) * 100}%`;
    label.textContent = count >= 5 ? "5 / 5 완료 ☕" : `${count} / 5 카드 · ${readMin}분 컷`;
  }

  /* ---------------- 날짜별 에디션 로딩 (메모리 캐시 적용) ---------------- */

  // 선택한 날짜의 에디션 JSON을 불러오고(캐시 우선), 오늘/불판 탭을 갱신합니다.
  async function loadEdition(dateStr) {
    state.currentDate = dateStr;
    state.termFlipped = false;

    let edition = editionCache.get(dateStr);
    if (!edition) {
      edition = await fetchJSON(`./data/editions/${dateStr}.json`);
      editionCache.set(dateStr, edition);
    }
    state.edition = edition;

    const issueBadge = document.getElementById("issue-badge");
    if (issueBadge) {
      issueBadge.textContent = `No.${String(edition.issue_no).padStart(2, "0")}`;
    }

    const select = document.getElementById("edition-select");
    if (select && select.value !== dateStr) {
      select.value = dateStr;
    }

    renderToday();
    renderBuzz();
    updateReadProgressUI();
  }

  /* ---------------- 1번 탭: 오늘 (5장 유한 피드) ---------------- */

  function renderToday() {
    const container = document.getElementById("today-feed");
    if (!container || !state.edition) return;

    const { date, brief } = state.edition;
    const readSet = getReadSet(date);
    const allDone = readSet.size >= 5;
    const streak = computeStreak();
    const { hero_news: hero, hot_issue: hot, term_of_day: term, daily_pick: pick, lunch_talk: lunch } = brief;

    container.innerHTML = `
      <!-- 1번 카드: 히어로 빅뉴스 -->
      <article class="card card-hero" data-card-key="c1">
        <div class="card-kicker-row">
          <span class="kicker-pill">${escapeHTML(hero.category || "BIG NEWS")}</span>
          <span class="card-num">01 / 05</span>
        </div>
        <h2 class="card-title">${escapeHTML(hero.title)}</h2>
        <ul class="summary-list">
          ${(hero.summary || []).map((line) => `<li>${escapeHTML(line)}</li>`).join("")}
        </ul>
        <div class="card-footer">
          <a class="source-link" href="${escapeHTML(hero.url)}" target="_blank" rel="noopener noreferrer">
            ${escapeHTML(hero.source)} ↗
          </a>
          <div class="card-actions">
            ${renderReadButton(readSet, "c1")}
            ${renderScrapButton(hero.id, "data-scrap-target", "hero")}
          </div>
        </div>
      </article>

      <!-- 2번 카드: 핫 이슈 -->
      <article class="card" data-card-key="c2">
        <div class="card-kicker-row">
          <span class="kicker-pill">HOT ISSUE</span>
          <span class="card-num">02 / 05</span>
        </div>
        <h2 class="card-title">${escapeHTML(hot.title)}</h2>
        <ul class="summary-list">
          ${(hot.summary || []).map((line) => `<li>${escapeHTML(line)}</li>`).join("")}
        </ul>
        <div class="card-footer">
          <a class="source-link" href="${escapeHTML(hot.url)}" target="_blank" rel="noopener noreferrer">
            ${escapeHTML(hot.source)} ↗
          </a>
          <div class="card-actions">
            ${renderReadButton(readSet, "c2")}
            ${renderScrapButton(hot.id, "data-scrap-target", "hot")}
          </div>
        </div>
      </article>

      <!-- 3번 카드: 1줄 신조어 플립 -->
      <article class="card term-card ${state.termFlipped ? "flipped" : ""}" data-card-key="c3" id="term-flip-card" aria-expanded="${state.termFlipped}">
        <div class="card-kicker-row">
          <span class="kicker-pill">1줄 신조어</span>
          <span class="card-num">03 / 05</span>
        </div>
        <h2 class="term-word">${escapeHTML(term.term)}</h2>
        <p class="term-analogy">${escapeHTML(term.analogy)}</p>
        <span class="term-flip-hint">
          ${state.termFlipped ? "회의실 실전 멘트 열림 ▴" : "탭해서 회의실 멘트 뒤집어 보기 ↻"}
        </span>
        <div class="term-office-box" ${state.termFlipped ? "" : "hidden"}>
          ${escapeHTML(term.office_line)}
        </div>
        <div class="card-footer" style="margin-top:14px;">
          <span class="source-link">오늘의 개념 비유</span>
          <div class="card-actions">
            ${renderReadButton(readSet, "c3")}
            ${renderScrapButton(term.id, "data-scrap-target", "term")}
          </div>
        </div>
      </article>

      <!-- 4번 카드: 오늘의 줍줍 -->
      <article class="card" data-card-key="c4">
        <div class="card-kicker-row">
          <span class="kicker-pill">오늘의 줍줍 · ${pick.type === "prompt" ? "#프롬프트" : "#툴"}</span>
          <span class="card-num">04 / 05</span>
        </div>
        <h2 class="card-title">${escapeHTML(pick.title)}</h2>
        <p class="pick-desc">${escapeHTML(pick.description)}</p>
        ${renderCopyBox(pick.type, pick.copy_text)}
        <div class="card-footer">
          <a class="source-link" href="${escapeHTML(pick.url)}" target="_blank" rel="noopener noreferrer">
            링크 열기 ↗
          </a>
          <div class="card-actions">
            ${renderReadButton(readSet, "c4")}
            ${renderScrapButton(pick.id, "data-scrap-target", "pick")}
          </div>
        </div>
      </article>

      <!-- 5번 카드: 점심 한 마디 -->
      <article class="card card-quote" data-card-key="c5">
        <div class="card-kicker-row">
          <span class="kicker-pill">점심 한 마디</span>
          <span class="card-num">05 / 05</span>
        </div>
        <div class="quote-mark">“</div>
        <p class="quote-text">${escapeHTML(lunch.quote)}</p>
        <div class="card-footer">
          <span class="source-link">동료와 가볍게 던질 인사이트</span>
          <div class="card-actions">
            ${renderReadButton(readSet, "c5")}
            <button type="button" class="pill-btn" data-copy-raw="${escapeHTML(lunch.quote)}">복사</button>
          </div>
        </div>
      </article>

      <!-- 하단 완독 도장 & 연속 출석 카운터 -->
      <div class="finish-card ${allDone ? "completed" : ""}">
        ${
          allDone
            ? `
          <div class="finish-stamp">오늘치 3분 컷 완료 ☕</div>
          <p class="finish-sub">5장의 카드를 모두 읽었습니다. 오늘도 가벼운 하루 되세요!</p>
          <div class="streak-pill">🔥 연속 출석 ${streak}일째</div>
        `
            : `
          <h3 class="finish-title">오늘치 5장 중 ${readSet.size}장 확인</h3>
          <p class="finish-sub">카드를 읽고 완료 도장을 찍어 연속 출석 기록을 쌓아보세요.</p>
          <button type="button" class="btn-primary" id="btn-complete-edition">오늘치 3분 컷 완료 찍기 ☕</button>
        `
        }
      </div>
    `;
  }

  /* ---------------- 2번 탭: 불판 (옹호파 vs 회의파) ---------------- */

  function renderBuzz() {
    const container = document.getElementById("buzz-feed");
    if (!container || !state.edition) return;
    const items = state.edition.buzz || [];

    if (items.length === 0) {
      container.innerHTML = `<div class="empty-state">오늘 등록된 불판 이슈가 없습니다.</div>`;
      return;
    }

    container.innerHTML = items
      .map(
        (item, idx) => `
      <article class="card">
        <div class="card-kicker-row">
          <span class="kicker-pill">${escapeHTML(item.tag)}</span>
          <span class="card-num">💬 ${Number(item.comment_count || 0).toLocaleString()}</span>
        </div>
        <h2 class="card-title">${escapeHTML(item.title)}</h2>
        <p class="buzz-context">${escapeHTML(item.context)}</p>
        <div class="vs-grid">
          <div class="vs-box pro">
            <span class="vs-label">옹호파 · 낙관론</span>
            <p class="vs-text">${escapeHTML(item.pro_side)}</p>
          </div>
          <span class="vs-badge" aria-hidden="true">VS</span>
          <div class="vs-box con">
            <span class="vs-label">회의파 · 비판론</span>
            <p class="vs-text">${escapeHTML(item.con_side)}</p>
          </div>
        </div>
        <div class="card-footer">
          <a class="source-link" href="${escapeHTML(item.url)}" target="_blank" rel="noopener noreferrer">
            ${escapeHTML(item.source)} 원문 ↗
          </a>
          <div class="card-actions">
            ${renderScrapButton(item.id, "data-scrap-buzz", idx)}
          </div>
        </div>
      </article>`
      )
      .join("");
  }

  /* ---------------- 3번 탭: 줍줍 (태그 필터 + 키워드 검색) ---------------- */

  function renderPicks() {
    const container = document.getElementById("picks-list");
    if (!container) return;

    const q = state.picksQuery.trim().toLowerCase();
    const filtered = state.picks.filter((item) => {
      if (state.picksTag !== "ALL" && item.tag !== state.picksTag) return false;
      if (!q) return true;
      const hay = `${item.title} ${item.description} ${item.tag} ${item.copy_text || ""} ${item.office_line || ""}`.toLowerCase();
      return hay.includes(q);
    });

    if (filtered.length === 0) {
      container.innerHTML = `<div class="empty-state">조건에 맞는 줍줍 항목이 없어요.</div>`;
      return;
    }

    container.innerHTML = filtered
      .map(
        (item) => `
      <article class="card">
        <div class="card-kicker-row">
          <span class="kicker-pill">${escapeHTML(item.tag)}</span>
          <span class="card-num">${escapeHTML(item.date)}</span>
        </div>
        <h2 class="card-title">${escapeHTML(item.title)}</h2>
        <p class="pick-desc">${escapeHTML(item.description)}</p>
        ${
          item.office_line
            ? `<div class="term-office-box" style="margin-bottom:12px;">${escapeHTML(item.office_line)}</div>`
            : ""
        }
        ${renderCopyBox(item.type, item.copy_text)}
        <div class="card-footer">
          ${
            item.url
              ? `<a class="source-link" href="${escapeHTML(item.url)}" target="_blank" rel="noopener noreferrer">바로가기 ↗</a>`
              : `<span class="source-link">아카이브 항목</span>`
          }
          <div class="card-actions">
            ${renderScrapButton(item.id, "data-scrap-pick-id", item.id)}
          </div>
        </div>
      </article>`
      )
      .join("");
  }

  /* ---------------- 4번 탭: 판도 (주간 GitHub & 모델 랭킹) ---------------- */

  // 전주 대비 순위 변동 값(숫자 또는 "NEW")을 상승/하락/신규 배지로 변환합니다.
  function renderDeltaBadge(delta) {
    if (delta === "NEW") return `<span class="delta-badge delta-new">NEW</span>`;
    const num = Number(delta);
    if (num > 0) return `<span class="delta-badge delta-up">▲${num}</span>`;
    if (num < 0) return `<span class="delta-badge delta-down">▼${Math.abs(num)}</span>`;
    return `<span class="delta-badge delta-same">—</span>`;
  }

  function renderRadar() {
    const container = document.getElementById("radar-content");
    const updatedEl = document.getElementById("radar-updated");
    if (!container || !state.radar) return;

    if (updatedEl) {
      updatedEl.textContent = `${state.radar.week_label || state.radar.updated_at} 기준`;
    }

    const repos = state.radar.github_trending_top5 || [];
    const rankings = state.radar.model_rankings?.[state.radarCategory] || [];

    container.innerHTML = `
      <section class="card">
        <h2 class="radar-block-title">
          <span>GitHub AI 급상승 TOP 5</span>
          <span class="card-num">주간 ★ 증가</span>
        </h2>
        <div class="repo-list">
          ${repos
            .map(
              (repo) => `
            <a class="repo-item" href="${escapeHTML(repo.url)}" target="_blank" rel="noopener noreferrer">
              <span class="rank-num ${repo.rank === 1 ? "top1" : ""}">${repo.rank}</span>
              <div class="repo-info">
                <span class="repo-name">${escapeHTML(repo.name)}</span>
                <p class="repo-desc">${escapeHTML(repo.description)}</p>
                <div class="repo-meta">
                  <span class="star-gain">★ +${Number(repo.weekly_stars || 0).toLocaleString()}</span>
                  ${repo.language ? `<span>· ${escapeHTML(repo.language)}</span>` : ""}
                </div>
              </div>
            </a>`
            )
            .join("")}
        </div>
      </section>

      <section class="card">
        <h2 class="radar-block-title">
          <span>부문별 LLM 순위 변동</span>
          <span class="card-num">전주 대비</span>
        </h2>
        <div class="seg-tabs" role="tablist" aria-label="모델 순위 부문">
          <button type="button" class="seg-btn ${state.radarCategory === "coding" ? "active" : ""}" data-radar-cat="coding">코딩</button>
          <button type="button" class="seg-btn ${state.radarCategory === "reasoning" ? "active" : ""}" data-radar-cat="reasoning">추론</button>
          <button type="button" class="seg-btn ${state.radarCategory === "local" ? "active" : ""}" data-radar-cat="local">로컬</button>
        </div>
        <div class="rank-list">
          ${rankings
            .map(
              (m) => `
            <div class="rank-row">
              <span class="rank-num ${m.rank === 1 ? "top1" : ""}">${m.rank}</span>
              <div class="rank-model">
                <span class="rank-model-name">${escapeHTML(m.name)}</span>
                <span class="rank-provider">${escapeHTML(m.provider)}</span>
              </div>
              <span class="rank-score">${Number(m.score).toFixed(1)}</span>
              ${renderDeltaBadge(m.delta)}
            </div>`
            )
            .join("")}
        </div>
      </section>
    `;
  }

  /* ---------------- 탭 전환 & 이벤트 연결 ---------------- */

  // 하단 4개 탭 중 선택한 탭만 화면에 표시하고 스크롤을 맨 위로 올립니다.
  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      const isActive = panel.dataset.tab === tab;
      panel.hidden = !isActive;
      panel.classList.toggle("active", isActive);
    });
    document.querySelectorAll(".bottom-nav .nav-item").forEach((btn) => {
      const isActive = btn.dataset.tab === tab;
      btn.classList.toggle("active", isActive);
      if (isActive) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
    });
    updateReadProgressUI();
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  // 현재 열려 있는 탭만 다시 그려 불필요한 화면 갱신을 줄입니다.
  function renderActiveTab() {
    if (state.activeTab === "today") renderToday();
    else if (state.activeTab === "buzz") renderBuzz();
    else if (state.activeTab === "picks") renderPicks();
    else if (state.activeTab === "radar") renderRadar();
  }

  // 오늘 탭 카드의 스크랩 데이터를 종류별로 깔끔하게 조립합니다.
  function buildTodayScrapItem(kindKey, brief) {
    if (kindKey === "hero") {
      return {
        id: brief.hero_news.id,
        kind: "빅뉴스",
        title: brief.hero_news.title,
        subtitle: brief.hero_news.summary?.[0] || "",
        url: brief.hero_news.url,
      };
    }
    if (kindKey === "hot") {
      return {
        id: brief.hot_issue.id,
        kind: "핫이슈",
        title: brief.hot_issue.title,
        subtitle: brief.hot_issue.summary?.[0] || "",
        url: brief.hot_issue.url,
      };
    }
    if (kindKey === "term") {
      return {
        id: brief.term_of_day.id,
        kind: "#신조어",
        title: brief.term_of_day.term,
        subtitle: brief.term_of_day.analogy,
      };
    }
    if (kindKey === "pick") {
      return {
        id: brief.daily_pick.id,
        kind: brief.daily_pick.type === "prompt" ? "#프롬프트" : "#툴",
        title: brief.daily_pick.title,
        subtitle: brief.daily_pick.description,
        url: brief.daily_pick.url,
        copy_text: brief.daily_pick.copy_text || "",
      };
    }
    return null;
  }

  // 모든 버튼·입력창·키보드 이벤트를 한곳에서 등록합니다.
  function bindEvents() {
    // 하단 4개 탭 전환
    document.querySelectorAll(".bottom-nav .nav-item").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // 상단 과월호 날짜 선택
    document.getElementById("edition-select").addEventListener("change", (e) => {
      loadEdition(e.target.value).catch(() => showToast("해당 호수를 불러오지 못했어요"));
    });

    // 다크/라이트 모드 토글
    document.getElementById("btn-theme-toggle").addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "light";
      applyTheme(current === "dark" ? "light" : "dark");
    });

    // 스크랩 서랍 열기 및 닫기 (버튼·배경·ESC 키)
    document.getElementById("btn-scrap-drawer").addEventListener("click", openScrapDrawer);
    document.getElementById("btn-close-drawer").addEventListener("click", closeScrapDrawer);
    document.getElementById("scrap-backdrop").addEventListener("click", closeScrapDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeScrapDrawer();
    });

    // 줍줍 검색 입력은 120ms 디바운스를 적용해 불필요한 반복 렌더링을 방지합니다.
    const handleSearchInput = debounce((value) => {
      state.picksQuery = value;
      renderPicks();
    }, 120);
    document.getElementById("picks-search").addEventListener("input", (e) => {
      handleSearchInput(e.target.value);
    });

    // 줍줍 태그 칩 필터
    document.getElementById("picks-chips").addEventListener("click", (e) => {
      const btn = e.target.closest(".chip");
      if (!btn) return;
      state.picksTag = btn.dataset.tag;
      document.querySelectorAll("#picks-chips .chip").forEach((c) => {
        c.classList.toggle("active", c === btn);
      });
      renderPicks();
    });

    // 동적으로 그려지는 카드 내부 버튼은 이벤트 위임으로 한 번에 처리합니다.
    document.body.addEventListener("click", (e) => {
      const copyBtn = e.target.closest("[data-copy-raw]");
      if (copyBtn) {
        e.stopPropagation();
        handleCopy(copyBtn.getAttribute("data-copy-raw"));
        return;
      }

      const removeBtn = e.target.closest("[data-remove-scrap]");
      if (removeBtn) {
        const id = removeBtn.getAttribute("data-remove-scrap");
        const found = state.scraps.find((s) => s.id === id);
        if (found) toggleScrap(found);
        return;
      }

      const readBtn = e.target.closest("[data-read-card]");
      if (readBtn) {
        e.stopPropagation();
        markCardRead(state.currentDate, readBtn.getAttribute("data-read-card"));
        renderToday();
        return;
      }

      if (e.target.closest("#btn-complete-edition")) {
        markAllCardsRead(state.currentDate);
        return;
      }

      const scrapTarget = e.target.closest("[data-scrap-target]");
      if (scrapTarget && state.edition) {
        e.stopPropagation();
        const item = buildTodayScrapItem(
          scrapTarget.getAttribute("data-scrap-target"),
          state.edition.brief
        );
        if (item) toggleScrap(item);
        return;
      }

      const buzzBtn = e.target.closest("[data-scrap-buzz]");
      if (buzzBtn && state.edition) {
        const idx = Number(buzzBtn.getAttribute("data-scrap-buzz"));
        const item = state.edition.buzz?.[idx];
        if (item) {
          toggleScrap({
            id: item.id,
            kind: "불판",
            title: item.title,
            subtitle: item.context,
            url: item.url,
          });
        }
        return;
      }

      const pickBtn = e.target.closest("[data-scrap-pick-id]");
      if (pickBtn) {
        const id = pickBtn.getAttribute("data-scrap-pick-id");
        const item = state.picks.find((p) => p.id === id);
        if (item) {
          toggleScrap({
            id: item.id,
            kind: item.tag,
            title: item.title,
            subtitle: item.description,
            url: item.url || "",
            copy_text: item.copy_text || "",
          });
        }
        return;
      }

      // 신조어 카드 본문을 탭하면 회의실 실전 멘트를 펼치거나 접습니다.
      const flipCard = e.target.closest("#term-flip-card");
      if (flipCard && !e.target.closest("button") && !e.target.closest("a")) {
        state.termFlipped = !state.termFlipped;
        markCardRead(state.currentDate, "c3");
        renderToday();
        return;
      }

      // 판도 탭의 모델 부문(코딩/추론/로컬) 전환
      const radarCatBtn = e.target.closest("[data-radar-cat]");
      if (radarCatBtn) {
        state.radarCategory = radarCatBtn.getAttribute("data-radar-cat");
        renderRadar();
      }
    });
  }

  /* ---------------- 앱 최초 구동 ---------------- */

  // 초기 데이터를 병렬로 불러와 첫 화면을 빠르게 렌더링합니다.
  async function init() {
    initTheme();
    updateScrapBadge();
    bindEvents();

    try {
      const [dates, picksData, radarData] = await Promise.all([
        fetchJSON("./data/index.json"),
        fetchJSON("./data/picks.json"),
        fetchJSON("./data/radar.json"),
      ]);

      state.dates = Array.isArray(dates) ? dates : [];
      state.picks = Array.isArray(picksData) ? picksData : picksData.items || [];
      state.radar = radarData;

      const select = document.getElementById("edition-select");
      select.innerHTML = state.dates
        .map((d) => `<option value="${escapeHTML(d)}">${escapeHTML(formatDateLabel(d))}</option>`)
        .join("");

      if (state.dates.length > 0) {
        await loadEdition(state.dates[0]);
      }
      renderPicks();
      renderRadar();
    } catch {
      const feed = document.getElementById("today-feed");
      if (feed) {
        feed.innerHTML = `<div class="empty-state">데이터를 불러오는 중 문제가 발생했습니다.<br/>로컬 정적 서버로 실행했는지 확인해 주세요.</div>`;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
