"""Tests for app/tools/ async synonym + syntax modules.

Phase 2 coverage — respx mocks httpx so no live network calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import respx
import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wordhippo"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# wordhippo_client — SpiderFetcher async
# ---------------------------------------------------------------------------

class TestSpiderFetcherAsync:
    @pytest.fixture(autouse=True)
    def _set_spider_env(self, monkeypatch):
        # wordhippo_client imports `from app import config` (the module object), then reads
        # config.settings.X at call time. Earlier tests reload app.config which creates a NEW
        # settings instance — wordhippo_client.config.settings is that new instance.
        # Patch through the module reference that wordhippo_client actually holds.
        import app.tools.wordhippo_client as wc_mod
        monkeypatch.setattr(wc_mod.config.settings, "spider_api_key", "test-key-sentinel")
        monkeypatch.setattr(wc_mod.config.settings, "spider_fetch_url", "https://api.spider.cloud/scrape")
        monkeypatch.setattr(wc_mod.config.settings, "wordhippo_mode", "spider")

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_returns_html_from_content_key(self):
        html = load_fixture("saw.html")
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": html, "url": "https://www.wordhippo.com/..."}])
        )
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        result = await fetcher.fetch("saw")
        assert result == html

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_falls_back_to_html_key(self):
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"html": "<html>fallback</html>", "url": "..."}])
        )
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        result = await fetcher.fetch("saw")
        assert result == "<html>fallback</html>"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_empty_content_returns_empty_string(self):
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": "", "url": "..."}])
        )
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        result = await fetcher.fetch("saw")
        assert result == ""

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_raises_on_bad_json(self):
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, text="not-json-at-all")
        )
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        with pytest.raises(RuntimeError, match="not valid JSON"):
            await fetcher.fetch("saw")

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_raises_on_http_error(self):
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.fetch("saw")

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, monkeypatch):
        import app.tools.wordhippo_client as wc_mod
        monkeypatch.setattr(wc_mod.config.settings, "spider_api_key", "")
        from app.tools.wordhippo_client import SpiderFetcher
        fetcher = SpiderFetcher()
        with pytest.raises(RuntimeError, match="SPIDER_API_KEY"):
            await fetcher.fetch("saw")

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_fetcher_async_returns_spider_fetcher(self):
        from app.tools.wordhippo_client import SpiderFetcher, get_fetcher_async
        fetcher = await get_fetcher_async("spider")
        assert isinstance(fetcher, SpiderFetcher)

    @pytest.mark.asyncio
    async def test_get_fetcher_async_invalid_mode_raises(self):
        from app.tools.wordhippo_client import get_fetcher_async
        with pytest.raises(ValueError, match="direct"):
            await get_fetcher_async("direct")


# ---------------------------------------------------------------------------
# synonym_tools — wordhippo_lookup (async, respx-mocked)
# ---------------------------------------------------------------------------

class TestWordhippoLookup:
    @pytest.fixture(autouse=True)
    def _set_spider_env(self, monkeypatch):
        import app.tools.wordhippo_client as wc_mod
        monkeypatch.setattr(wc_mod.config.settings, "spider_api_key", "test-key-sentinel")
        monkeypatch.setattr(wc_mod.config.settings, "spider_fetch_url", "https://api.spider.cloud/scrape")
        monkeypatch.setattr(wc_mod.config.settings, "wordhippo_mode", "spider")

    @respx.mock
    @pytest.mark.asyncio
    async def test_lookup_returns_definitions(self):
        html = load_fixture("saw.html")
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": html}])
        )
        from app.tools.tool_impls import wordhippo_lookup_impl
        result = await wordhippo_lookup_impl({"word": "saw"})
        assert result["word"] == "saw"
        assert result["definition_count"] > 0
        assert isinstance(result["definitions"], list)
        assert result["warnings"] == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_lookup_empty_html_returns_warning(self):
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": ""}])
        )
        from app.tools.tool_impls import wordhippo_lookup_impl
        result = await wordhippo_lookup_impl({"word": "unknownword"})
        assert result["definition_count"] == 0
        assert len(result["warnings"]) > 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_lookup_synonyms_found_context_id(self):
        html = load_fixture("saw.html")
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": html}])
        )
        from app.tools.tool_impls import wordhippo_lookup_impl
        result = await wordhippo_lookup_impl({"word": "saw", "context_id": "C0-1"})
        assert result["word"] == "saw"
        assert result["context_id"] == "C0-1"
        assert isinstance(result["synonyms"], list)
        assert result["synonym_count"] >= 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_lookup_synonyms_missing_context_id_returns_warning(self):
        html = load_fixture("saw.html")
        respx.post("https://api.spider.cloud/scrape").mock(
            return_value=httpx.Response(200, json=[{"content": html}])
        )
        from app.tools.tool_impls import wordhippo_lookup_impl
        result = await wordhippo_lookup_impl({"word": "saw", "context_id": "C0-999"})
        assert result["synonyms"] == []
        assert any("C0-999" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Pure module smoke tests (no network)
# ---------------------------------------------------------------------------

class TestSyntaxToolsPure:
    def test_classify_syntax_family_cta_permission(self):
        from app.tools.syntax_family_classifier import classify_syntax_family
        r = classify_syntax_family("Want me to send you the breakdown?", role="cta")
        assert r["family"] == "cta_permission"

    def test_classify_syntax_family_opener_observation(self):
        from app.tools.syntax_family_classifier import classify_syntax_family
        r = classify_syntax_family("I noticed your recent review on Google.", role="opener")
        assert r["family"] == "evidence_first_observation"

    def test_classify_sentence_blocks_opener(self):
        from app.tools.syntax_block_classifier import classify_sentence_blocks
        r = classify_sentence_blocks("I noticed your 5-star Google review.", role="opener")
        labels = [b["label"] for b in r["blocks"]]
        assert "observation_verb" in labels

    def test_classify_sentence_blocks_cta_fallback(self):
        from app.tools.syntax_block_classifier import classify_sentence_blocks
        r = classify_sentence_blocks("Let me know if you want to chat.", role="cta")
        assert len(r["blocks"]) > 0

    def test_reshape_blocks_cta_curiosity(self):
        from app.tools.syntax_reshuffler import reshape_blocks
        r = reshape_blocks("Would it hurt to see if this works for you?", role="cta")
        assert isinstance(r["variants"], list)

    def test_lint_structure_repetition_low_risk(self):
        from app.tools.fingerprint_lint import lint_structure_repetition
        lines = [
            "I noticed your review.",
            "Want me to show you more?",
            "We helped a similar company grow.",
        ]
        r = lint_structure_repetition(lines, role="opener")
        assert "risk_level" in r
        assert r["line_count"] == 3

    def test_lint_structure_repetition_direct(self):
        from app.tools.fingerprint_lint import lint_structure_repetition
        r = lint_structure_repetition(["Same line.", "Same line."], role="opener")
        assert r["line_count"] == 2


class TestSynonymScorerPure:
    def test_score_synonym_candidates_returns_shape(self):
        from app.tools.synonym_scorer import score_synonym_candidates
        r = score_synonym_candidates(
            source_word="saw",
            sentence="I saw your review online.",
            candidates=["noticed", "observed", "found"],
            role="opener",
            sense_label="visual_observation",
        )
        assert r["source_word"] == "saw"
        assert len(r["results"]) == 3
        for res in r["results"]:
            assert "candidate" in res
            assert "final_score" in res
            assert "status" in res

    def test_approved_candidate_score_at_least_0_8(self):
        from app.tools.synonym_scorer import score_synonym_candidates
        r = score_synonym_candidates(
            source_word="saw",
            sentence="I saw your Google review.",
            candidates=["noticed"],
            role="opener",
            sense_label="visual_observation",
        )
        assert r["results"][0]["final_score"] >= 0.8

    def test_formal_rejection_score_at_most_0_35(self):
        from app.tools.synonym_scorer import score_synonym_candidates
        r = score_synonym_candidates(
            source_word="saw",
            sentence="I saw your Google review.",
            candidates=["observed"],
            role="opener",
            sense_label="visual_observation",
        )
        assert r["results"][0]["final_score"] <= 0.35

    def test_lookup_approved_lexicon_returns_three_buckets(self):
        from app.tools.synonym_scorer import lookup_approved_lexicon
        r = lookup_approved_lexicon("saw")
        assert "approved" in r
        assert "candidate_review" in r
        assert "rejected" in r
        assert "noticed" in r["approved"]

    def test_lookup_approved_lexicon_unknown_word(self):
        from app.tools.synonym_scorer import lookup_approved_lexicon
        r = lookup_approved_lexicon("unknownXYZ")
        assert r["approved"] == []

    def test_lexicon_store_get_approved_synonyms(self):
        from app.tools.lexicon_store import get_approved_synonyms
        r = get_approved_synonyms("saw")
        assert "noticed" in r["approved"]

    def test_lexicon_store_has_entry(self):
        from app.tools.lexicon_store import has_entry
        assert has_entry("saw") is True
        assert has_entry("unknownXYZ") is False


class TestSenseClassifierPure:
    def test_visual_observation_sense(self):
        from app.tools.sense_classifier import classify_word_sense_for_sentence
        r = classify_word_sense_for_sentence("saw", "I saw your Google review.", role="opener")
        assert r["sense_label"] == "visual_observation"

    def test_send_cta_sense(self):
        from app.tools.sense_classifier import classify_word_sense_for_sentence
        r = classify_word_sense_for_sentence("send", "Want me to send you the details?", role="cta")
        assert r["sense_label"] == "send_share_cta"

    def test_unknown_sense_returns_warning(self):
        from app.tools.sense_classifier import classify_word_sense_for_sentence
        r = classify_word_sense_for_sentence("blorp", "blorp blorp", role="unknown")
        assert r["sense_label"] == "unknown"
        assert len(r["warnings"]) > 0
