/* Morning AI Zine — 프론트엔드 공통 유틸리티 모음 */
(() => {
  // 요일 번호(0=일요일 ~ 6=토요일)를 한국어 한 글자로 변환하는 표
  const WEEKDAYS_KO = ["일", "월", "화", "수", "목", "금", "토"];

  // 브라우저 저장소(localStorage)에서 JSON 데이터를 안전하게 읽어옵니다.
  function loadJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  // 브라우저 저장소(localStorage)에 JSON 데이터를 안전하게 저장합니다.
  function saveJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // 저장 공간이 가득 찬 경우에도 앱이 멈추지 않도록 무시합니다.
    }
  }

  // HTML 특수문자를 안전한 문자로 바꿔 화면 깨짐과 보안 문제(XSS)를 막습니다.
  function escapeHTML(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // "2026-11-26" 형식의 날짜를 "11.26 목" 형태의 보기 좋은 라벨로 바꿉니다.
  function formatDateLabel(dateStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    const wd = WEEKDAYS_KO[dt.getDay()] || "";
    return `${String(m).padStart(2, "0")}.${String(d).padStart(2, "0")} ${wd}`;
  }

  // 지정된 경로에서 JSON 파일을 항상 최신 상태로 불러옵니다(iOS PWA 캐시 버스팅 적용).
  async function fetchJSON(path) {
    const sep = path.includes("?") ? "&" : "?";
    const bustUrl = `${path}${sep}_t=${Date.now()}`;
    const res = await fetch(bustUrl, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
    return res.json();
  }

  // 텍스트를 클립보드에 복사하고 성공 여부를 true/false로 돌려줍니다.
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      return true;
    } catch {
      return false;
    }
  }

  // 짧은 시간에 연속으로 호출되는 작업(예: 검색어 입력)을 마지막 입력 후 한 번만 실행합니다.
  function debounce(fn, delayMs = 120) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delayMs);
    };
  }

  // HTML 이스케이프 후 핵심 수치·단위·주요 AI 고유명사를 <strong>으로 자동 강조합니다(XSS 안전).
  const HIGHLIGHT_RE = new RegExp(
    [
      // 금액·비율·배수·분수·파라미터/메모리/수량 단위 (예: $3.00, 99.5%, 1/8, 32B, 1M, 80GB, 1,700만, 5종)
      "(?:\\$\\d+(?:[.,]\\d+)?[KMB]?)",
      "(?:\\d+(?:,\\d{3})*(?:\\.\\d+)?(?:%|배|만|억|종|개|초|분|시간|GB|TB|MB|K|M|B)(?![A-Za-z]))",
      "(?:\\d+\\/\\d+)",
      // 주요 AI 모델·프레임워크·표준 고유명사 및 버전명
      "(?:\\b(?:GPT-\\d+(?:\\.\\d+)?|Gemini\\s?\\d+(?:\\.\\d+)?|Claude\\s?[\\w.]+|Qwen[\\d.]*|DeepSeek-[\\w.-]+|Llama\\s?[\\d.]+|Gemma\\s?[\\d.]+|EXAONE[\\s\\d.]*|Grok\\s?\\d+(?:\\.\\d+)?|vLLM|SGLang|Apache\\s?2\\.0|MIT|MCP|WebGPU|RAG|SWE-bench|SDD|B200|H100)\\b)",
    ].join("|"),
    "g"
  );

  function highlightSummary(text) {
    const safe = escapeHTML(text);
    return safe.replace(HIGHLIGHT_RE, (match) => `<strong class="hl">${match}</strong>`);
  }

  // 다른 스크립트(app.js)에서 공통으로 꺼내 쓸 수 있도록 전역 객체에 등록합니다.
  window.ZineUtils = {
    loadJSON,
    saveJSON,
    escapeHTML,
    highlightSummary,
    formatDateLabel,
    fetchJSON,
    copyText,
    debounce,
  };
})();
