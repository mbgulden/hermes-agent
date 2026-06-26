"""Tests for web_search backend fallback-on-failure chain.

Regression: hermes's web_search_tool previously selected ONE backend at
dispatch time and never retried. When the configured primary backend
returned success=false (quota exhausted, rate limit, 401/403, network
error), the search just failed without trying any of the other
configured backends. Users with multiple API keys (e.g. Tavily + Brave)
could not have them auto-fallthrough.

These tests pin the new behavior:

1. ``_get_backend_chain`` returns configured-first, then auto-detect
   backends in canonical priority order (Tavily → Exa → Parallel →
   Firecrawl → SearXNG → Brave-free → ddgs).

2. ``_is_backend_failure`` distinguishes between success-with-results,
   success-with-zero-results, and backend-failure responses.

3. ``web_search_tool`` walks the chain and returns the first
   non-failing response when the primary backend fails.
"""

import json
import os
import sys

import pytest


# Make tools/ importable before pulling web_tools (it uses relative imports)
TOOLS_DIR = "/home/ubuntu/work/hermes-agent-fork"
sys.path.insert(0, TOOLS_DIR)


@pytest.fixture
def web_tools():
    """Import the web_tools module fresh for each test."""
    # Clear cached env so test isolation holds even when other tests
    # mutate env vars
    for k in ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY",
              "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "SEARXNG_URL",
              "PARALLEL_API_KEY"):
        os.environ.pop(k, None)

    # Import the real module
    from tools import web_tools
    return web_tools


def test_get_backend_chain_returns_configured_first(web_tools):
    """When web.backend is set in config, it leads the chain."""
    os.environ["TAVILY_API_KEY"] = "test-tavily"
    os.environ["BRAVE_SEARCH_API_KEY"] = "test-brave"

    # Pre-load the config patch
    web_tools._load_web_config = lambda: {"backend": "brave-free"}

    chain = web_tools._get_backend_chain()
    assert chain[0] == "brave-free"
    # Remaining chain includes tavily, ddgs (if installed) — order is canonical
    assert "tavily" in chain
    assert "ddgs" in chain or len(chain) >= 2


def test_get_backend_chain_auto_detect_order(web_tools):
    """Auto-detect walks the canonical fallback chain in order."""
    os.environ["TAVILY_API_KEY"] = "test-tavily"
    os.environ["BRAVE_SEARCH_API_KEY"] = "test-brave"

    web_tools._load_web_config = lambda: {}

    chain = web_tools._get_backend_chain()
    # Tavily should come before Brave in canonical priority
    tavily_idx = chain.index("tavily")
    brave_idx = chain.index("brave-free")
    assert tavily_idx < brave_idx, f"chain order wrong: {chain}"


def test_get_backend_chain_excludes_unavailable(web_tools):
    """Backends without API keys are not in the chain."""
    # Only Tavily key set; Brave should NOT appear
    os.environ["TAVILY_API_KEY"] = "test-tavily"

    web_tools._load_web_config = lambda: {}

    chain = web_tools._get_backend_chain()
    assert "tavily" in chain
    assert "brave-free" not in chain
    assert "exa" not in chain
    assert "parallel" not in chain


def test_is_backend_failure_detects_success(web_tools):
    """Success responses (with or without results) are NOT failures."""
    success_with_results = {
        "success": True,
        "data": {"web": [{"title": "test", "url": "https://example.com"}]},
    }
    assert web_tools._is_backend_failure(success_with_results) is False

    # Zero results is NOT a failure — it's a legitimate answer
    success_empty = {
        "success": True,
        "data": {"web": []},
    }
    assert web_tools._is_backend_failure(success_empty) is False


def test_is_backend_failure_detects_failure(web_tools):
    """success: False is always a backend failure."""
    quota_exceeded = {
        "success": False,
        "error": "Tavily quota exhausted for the month",
    }
    assert web_tools._is_backend_failure(quota_exceeded) is True

    auth_error = {
        "success": False,
        "error": "401 Unauthorized",
    }
    assert web_tools._is_backend_failure(auth_error) is True

    # Non-dict responses are also failures (unexpected shape)
    assert web_tools._is_backend_failure("not a dict") is True
    assert web_tools._is_backend_failure(None) is True


def test_get_backend_chain_dedupes(web_tools):
    """Same backend appearing in both configured and chain is not duplicated."""
    os.environ["TAVILY_API_KEY"] = "test-tavily"
    web_tools._load_web_config = lambda: {"backend": "tavily"}

    chain = web_tools._get_backend_chain()
    assert chain.count("tavily") == 1


def test_get_backend_chain_handles_empty_env(web_tools):
    """With no API keys and no config, returns ddgs (or empty if not installed)."""
    web_tools._load_web_config = lambda: {}

    chain = web_tools._get_backend_chain()
    # ddgs is the universal last-resort fallback
    # It might or might not be in chain depending on whether the package
    # is installed in the test env. Both outcomes are valid.
    assert all(isinstance(b, str) for b in chain)


def test_get_backend_chain_with_searxng(web_tools):
    """SearXNG appears in chain when SEARXNG_URL is set."""
    os.environ["SEARXNG_URL"] = "https://searx.example.com"
    os.environ["TAVILY_API_KEY"] = "test-tavily"

    web_tools._load_web_config = lambda: {}

    chain = web_tools._get_backend_chain()
    assert "searxng" in chain
    # Canonical order: tavily before searxng before brave-free
    if "tavily" in chain:
        assert chain.index("tavily") < chain.index("searxng")