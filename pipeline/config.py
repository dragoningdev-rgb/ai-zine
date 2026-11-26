"""Configuration for Morning AI Zine collection and curation pipeline."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = WEB_DIR / "data"
EDITIONS_DIR = DATA_DIR / "editions"

# HTTP / Networking
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
ARTICLE_META_TIMEOUT = int(os.getenv("ARTICLE_META_TIMEOUT", "5"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))
USER_AGENT = "MorningAIZine/1.0 (+https://github.com/morning-ai-zine)"
MAX_WORKERS = 6
LOOKBACK_HOURS = 24

# Hacker News thresholds (R1: score >= 100 or comments >= 50)
HN_MIN_SCORE = 100
HN_MIN_COMMENTS = 50
HN_MAX_STORIES_TO_CHECK = 60
HN_TOP_COMMENTS_LIMIT = 5
TOP_CANDIDATES_TO_ENRICH = 15

# Deduplication threshold (R1: >= 80% title similarity)
TITLE_SIMILARITY_THRESHOLD = 0.80

# RSS & API Sources
RSS_SOURCES = [
    {
        "name": "GeekNews",
        "url": "https://news.hada.io/rss/news",
        "kind": "news",
        "always_ai": False,
    },
    {
        "name": "r/LocalLLaMA",
        "url": "https://www.reddit.com/r/LocalLLaMA/hot/.rss?limit=25",
        "kind": "community",
        "always_ai": True,
    },
    {
        "name": "r/MachineLearning",
        "url": "https://www.reddit.com/r/MachineLearning/hot/.rss?limit=25",
        "kind": "community",
        "always_ai": True,
    },
]

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_BEST_STORIES_URL = "https://hacker-news.firebaseio.com/v0/beststories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"

HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"
HF_TRENDING_MODELS_URL = "https://huggingface.co/api/models"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GITHUB_SEARCH_REPOS_URL = "https://api.github.com/search/repositories"

# 사내 폐쇄망(vLLM/SGLang) 반입 가능한 공식 오픈웨이트 LLM 오가니제이션 화이트리스트
OFFICIAL_OPEN_LLM_ORGS = (
    "Qwen",
    "deepseek-ai",
    "meta-llama",
    "google",
    "LGAI-EXAONE",
    "mistralai",
    "microsoft",
    "nvidia",
)

# AI / LLM relevance filter keywords (case-insensitive)
AI_KEYWORDS = (
    "ai",
    "llm",
    "gpt",
    "claude",
    "gemini",
    "llama",
    "qwen",
    "deepseek",
    "mistral",
    "gemma",
    "openai",
    "anthropic",
    "deepmind",
    "huggingface",
    "hugging face",
    "transformer",
    "diffusion",
    "agent",
    "agentic",
    "mcp",
    "rag",
    "vllm",
    "ollama",
    "gguf",
    "lora",
    "fine-tun",
    "finetun",
    "inference",
    "reasoning",
    "benchmark",
    "prompt",
    "embedding",
    "vector",
    "multimodal",
    "copilot",
    "cursor",
    "codex",
    "neural",
    "machine learning",
    "deep learning",
    "인공지능",
    "에이전트",
    "모델",
    "추론",
    "프롬프트",
    "파인튜닝",
    "오픈웨이트",
)

# LLM Editor Configuration (Google AI Studio 무료 티어 표준 gemini-2.5-flash)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
