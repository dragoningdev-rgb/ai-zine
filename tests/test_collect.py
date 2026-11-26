"""Unit tests for collection, normalization, AI filtering, and deduplication (AC1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from pipeline.collect import (
    RawItem,
    deduplicate_items,
    extract_meta_description,
    fetch_github_readme_snippet,
    fetch_github_repo_community_signals,
    fetch_hn_top_comments,
    fetch_major_bigtech_models,
    fetch_onprem_open_llms,
    fetch_rss_source,
    is_ai_related,
    is_official_onprem_model,
    is_recent,
    normalize_url,
    title_similarity,
)


def test_normalize_url_strips_tracking_and_trailing_slash() -> None:
    url1 = "https://www.Example.com/post/ai-news/?utm_source=hn&utm_medium=social&id=42#top"
    url2 = "https://example.com/post/ai-news?id=42&ref=geeknews"
    assert normalize_url(url1) == "https://example.com/post/ai-news?id=42"
    assert normalize_url(url1) == normalize_url(url2)


def test_deduplicate_removes_duplicate_urls_zero_remaining() -> None:
    items = [
        RawItem(
            title="New Open-Weight LLM Released",
            url="https://example.com/llm?utm_source=rss",
            source="GeekNews",
            score=60,
        ),
        RawItem(
            title="Completely Different Title Same Link",
            url="https://www.example.com/llm/",
            source="Hacker News",
            score=220,
            comments=80,
        ),
        RawItem(
            title="MCP Server Security Guide",
            url="https://blog.example.org/mcp-security",
            source="Hacker News",
            score=150,
            comments=60,
        ),
    ]

    deduped = deduplicate_items(items)
    urls = [item.url for item in deduped]
    assert len(urls) == len(set(urls)) == 2
    # Higher-ranked item (HN with score 220 + 80*1.5) should win for duplicate URL
    assert any(item.source == "Hacker News" and item.url == "https://example.com/llm" for item in deduped)


def test_deduplicate_removes_similar_titles_above_threshold() -> None:
    low = RawItem(
        title="Open-weight 32B reasoning model beats flagship on SWE-bench",
        url="https://site-a.com/story-1",
        source="r/LocalLLaMA",
        score=40,
    )
    high = RawItem(
        title="Open-weight 32B reasoning model beats flagship on SWE-bench!",
        url="https://site-b.com/story-2",
        source="Hacker News",
        score=310,
        comments=120,
    )
    assert title_similarity(low.title, high.title) >= 0.80

    deduped = deduplicate_items([low, high])
    assert len(deduped) == 1
    assert deduped[0].source == "Hacker News"


def test_deduplicate_keeps_distinct_items() -> None:
    items = [
        RawItem(title="vLLM 0.9 adds speculative decoding", url="https://a.com/1", source="GeekNews", score=80),
        RawItem(title="WebGPU local inference in Chrome", url="https://b.com/2", source="Hacker News", score=180),
    ]
    deduped = deduplicate_items(items)
    assert len(deduped) == 2


def test_is_ai_related_filters_non_ai_noise() -> None:
    assert is_ai_related("MCP server for PostgreSQL and Claude")
    assert is_ai_related("로컬 추론 모델 양자화 벤치마크")
    assert not is_ai_related("Best sourdough bread recipe for winter")
    assert not is_ai_related("CSS Grid layout tips for responsive cards")
    assert not is_ai_related("Ask HN: Who is hiring? (AI Engineers, November 2026)")
    assert not is_ai_related("[채용] 생성형 AI LLM 백엔드 개발자 모집")


def test_normalize_url_unifies_http_and_mobile_subdomains() -> None:
    assert normalize_url("http://m.example.com/ai-post/") == normalize_url("https://www.example.com/ai-post")


def test_is_recent_window() -> None:
    now = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
    assert is_recent(now - timedelta(hours=3), now=now, hours=24)
    assert not is_recent(now - timedelta(hours=30), now=now, hours=24)


def test_fetch_rss_source_handles_network_failure_gracefully() -> None:
    mock_session = MagicMock()
    mock_session.get.side_effect = TimeoutError("connection timed out")

    items = fetch_rss_source(
        {"name": "GeekNews", "url": "https://news.hada.io/rss/news", "kind": "news", "always_ai": False},
        session=mock_session,
    )
    assert items == []


def test_fetch_rss_source_parses_and_filters_ai_entries() -> None:
    now_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Test Feed</title>
      <item>
        <title>New LLM Agent Framework with MCP Support</title>
        <link>https://example.com/agent-mcp</link>
        <description>Open source agentic workflow toolkit</description>
        <pubDate>{now_rfc}</pubDate>
      </item>
      <item>
        <title>Gardening tips for spring tomatoes</title>
        <link>https://example.com/tomatoes</link>
        <description>Soil and sunlight guide</description>
        <pubDate>{now_rfc}</pubDate>
      </item>
    </channel></rss>""".encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.content = rss_xml
    mock_resp.raise_for_status.return_value = None
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    items = fetch_rss_source(
        {"name": "GeekNews", "url": "https://news.hada.io/rss/news", "kind": "news", "always_ai": False},
        session=mock_session,
    )
    assert len(items) == 1
    assert items[0].url == "https://example.com/agent-mcp"


def test_extract_meta_description_prefers_og_description() -> None:
    html_doc = """
    <html><head>
      <meta name="description" content="Fallback description">
      <meta property="og:description" content="Benchmark shows 3.2x faster inference on 4-bit quantized 32B models.">
    </head></html>
    """
    assert "3.2x faster inference" in extract_meta_description(html_doc)


def test_fetch_hn_top_comments_filters_deleted_and_strips_html() -> None:
    mock_session = MagicMock()
    resp_deleted = MagicMock()
    resp_deleted.json.return_value = {"deleted": True}
    resp_deleted.raise_for_status.return_value = None

    resp_valid = MagicMock()
    resp_valid.json.return_value = {
        "text": "<p>Running 32B models locally with vLLM cut our monthly API spend by 70%.</p>"
    }
    resp_valid.raise_for_status.return_value = None

    mock_session.get.side_effect = [resp_deleted, resp_valid]
    comments = fetch_hn_top_comments([101, 102], session=mock_session, limit=3)
    assert len(comments) == 1
    assert "cut our monthly API spend by 70%" in comments[0]


def test_is_official_onprem_model_filters_gguf_abliterated_and_unofficial() -> None:
    assert is_official_onprem_model("Qwen/Qwen2.5-Coder-32B-Instruct", ["license:apache-2.0"])
    assert is_official_onprem_model("LGAI-EXAONE/EXAONE-3.5-32B-Instruct", [])
    assert not is_official_onprem_model("bartowski/Qwen2.5-32B-GGUF", ["gguf"])
    assert not is_official_onprem_model("Qwen/Qwen2.5-32B-Instruct-GGUF", [])
    assert not is_official_onprem_model("deepseek-ai/DeepSeek-R1-abliterated", [])
    assert not is_official_onprem_model("randomuser/uncensored-merge-7B", [])


def test_fetch_onprem_open_llms_keeps_only_official_models() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [
        {"id": "randomuser/Model-GGUF-abliterated", "likes": 99999, "trendingScore": 999, "tags": ["gguf"]},
        {"id": "google/gemma-3-1b-it", "likes": 3000, "trendingScore": 800, "tags": ["license:gemma"]},
        {"id": "Qwen/Qwen3.8-27B", "likes": 1800, "trendingScore": 450, "tags": ["license:apache-2.0"]},
        {"id": "deepseek-ai/DeepSeek-V4-Pro", "likes": 2500, "trendingScore": 420, "tags": ["license:mit"]},
        {"id": "meta-llama/Llama-3.3-70B-Instruct", "likes": 3600, "trendingScore": 310, "tags": ["license:llama3.3"]},
        {"id": "LGAI-EXAONE/EXAONE-3.5-32B-Instruct", "likes": 1200, "trendingScore": 220, "tags": ["license:exaone"]},
        {"id": "mistralai/Mistral-Small-3.1-24B-Instruct-2503", "likes": 2100, "trendingScore": 180, "tags": ["license:apache-2.0"]},
    ]
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    models = fetch_onprem_open_llms(session=mock_session)
    names = [m["name"] for m in models]
    assert "Model-GGUF-abliterated" not in names
    # 7B 미만 초경량(1B)은 7B+ 실무급 모델이 5개 이상일 때 제외되어야 함
    assert "gemma-3-1b-it" not in names
    assert models[0]["name"] == "Qwen3.8-27B"
    assert models[0]["license"] == "Apache 2.0"
    assert "27B" in models[0]["params_vram"]
    assert models[0]["vllm_supported"] is True
    # image-text-to-text 쿼리가 실제로 호출되었는지 검증
    called_tags = {call.kwargs.get("params", {}).get("pipeline_tag") for call in mock_session.get.call_args_list}
    assert "text-generation" in called_tags
    assert "image-text-to-text" in called_tags


def test_fetch_major_bigtech_models_parses_openrouter() -> None:
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "data": [
            {
                "id": "google/gemini-3-pro",
                "name": "Google: Gemini 3 Pro",
                "created": 1760000000,
                "context_length": 1000000,
                "pricing": {"prompt": "0.00000125", "completion": "0.00001"},
            },
            {
                "id": "google/gemini-2.5-flash-lite",
                "name": "Google: Gemini 2.5 Flash Lite",
                "created": 1765000000,
                "context_length": 1000000,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
            },
            {
                "id": "anthropic/claude-4.5-sonnet",
                "name": "Anthropic: Claude 4.5 Sonnet",
                "created": 1759000000,
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
        ]
    }
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    majors = fetch_major_bigtech_models(session=mock_session)
    assert len(majors) == 2
    assert majors[0]["name"] == "Gemini 3 Pro"
    assert majors[0]["context_window"] == "1M"
    assert majors[0]["input_price"] == "$1.25"
    assert majors[0]["output_price"] == "$10.0"
    assert majors[1]["name"] == "Claude 4.5 Sonnet"


def test_fetch_github_readme_snippet_truncates_to_limit() -> None:
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.text = "<h1>Demo Repo</h1> " + ("AI agent architecture details. " * 200)
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    snippet = fetch_github_readme_snippet("org/demo-repo", session=mock_session, max_chars=200)
    assert len(snippet) <= 200
    assert snippet.startswith("Demo Repo")


def test_fetch_github_repo_community_signals_collects_hn_and_issues() -> None:
    mock_session = MagicMock()

    resp_algolia = MagicMock(ok=True)
    resp_algolia.json.return_value = {"hits": [{"objectID": "555", "num_comments": 42}]}

    resp_hn_story = MagicMock(ok=True)
    resp_hn_story.json.return_value = {"kids": [901]}

    resp_hn_comment = MagicMock(ok=True)
    resp_hn_comment.raise_for_status.return_value = None
    resp_hn_comment.json.return_value = {
        "text": "This agent harness cut our token usage in half on large monorepos."
    }

    resp_gh_issues = MagicMock(ok=True)
    resp_gh_issues.json.return_value = [
        {"title": "Support air-gapped vLLM endpoints", "comments": 31},
        {"title": "Memory leak in long sessions", "comments": 19},
    ]

    mock_session.get.side_effect = [resp_algolia, resp_hn_story, resp_hn_comment, resp_gh_issues]
    signals = fetch_github_repo_community_signals("org/demo-repo", session=mock_session)
    assert len(signals["hn_comments"]) == 1
    assert "cut our token usage" in signals["hn_comments"][0]
    assert len(signals["gh_top_issues"]) == 2
    assert "air-gapped vLLM" in signals["gh_top_issues"][0]
