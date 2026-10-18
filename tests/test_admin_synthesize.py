"""
POST /api/horizon/synthesize: article generation (synchronous, minutes in llm mode) runs in a
worker thread, so the event loop of the daemon (bot polling, autopilot, admin) keeps answering;
the lead magnet is a background task in the loop whose failure is logged, not lost.
"""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from core import content  # noqa: E402
from core.models import Article  # noqa: E402
from core.profile import get_profile  # noqa: E402

TOKEN = os.environ["ADMIN_TOKEN"]
GENERATION_SECONDS = 0.6
NEWS_ITEM = {"title": "Налоговая меняет правила УСН", "description": "x", "lead_magnet_preset": "audit"}


@pytest.fixture
def example_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", str(PROJECT_ROOT / "profiles" / "example-person.yaml"))
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / "dotenv")
    get_profile.cache_clear()
    yield get_profile()
    get_profile.cache_clear()


@pytest.fixture
def slow_generation(monkeypatch):
    calls = {}

    def blocking_articles(campaign, prof, news=None, *, regenerate=False):
        time.sleep(GENERATION_SECONDS)  # stands in for urllib LLM calls
        return [Article(id="s1", campaign_id=campaign.id, platform="vc", title="Заголовок",
                        lead="", content_markdown="тело", content_html="", tags="")]

    async def fake_lead_magnet(slug, prof, *, output_dir):
        calls["lead_magnet"] = (slug, output_dir)
        if calls.get("fail"):
            raise RuntimeError("lead magnet LLM failed")
        return {}

    monkeypatch.setattr(content, "articles", blocking_articles)
    monkeypatch.setattr(content, "lead_magnet", fake_lead_magnet)
    return calls


def _synthesize_with_ticker():
    """Runs the request and a 10 ms ticker side by side; returns (response json, ticks during the request)."""
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            ticks = 0
            request = asyncio.create_task(client.post(
                "/api/horizon/synthesize", json={"news_item": NEWS_ITEM}, headers={"X-Admin-Token": TOKEN}))
            while not request.done():
                await asyncio.sleep(0.01)
                ticks += 1
            body = await (await request).json()
            for _ in range(20):  # let the background lead magnet task finish
                await asyncio.sleep(0.01)
            return body, ticks
    return asyncio.run(main())


def test_loop_answers_during_synthesis(example_profile, slow_generation):
    body, ticks = _synthesize_with_ticker()
    assert body["success"] is True
    assert body["result"]["articles_count"] == 1 and body["result"]["lead_magnet_preset"] == "audit"
    # a blocked loop gives ~0 ticks during the 0.6 s generation; a free one ~60
    assert ticks >= 20
    slug, output_dir = slow_generation["lead_magnet"]
    assert slug == "audit" and output_dir == Path(body["result"]["exports_dir"])


def test_lead_magnet_failure_is_logged(example_profile, slow_generation, caplog):
    slow_generation["fail"] = True
    with caplog.at_level(logging.ERROR, logger="NewsRadar"):
        body, _ = _synthesize_with_ticker()
    assert body["success"] is True
    assert any(r.name == "NewsRadar" and r.exc_info and "lead magnet LLM failed" in str(r.exc_info[1])
               for r in caplog.records)


def test_preset_from_request_is_a_slug(example_profile, slow_generation, monkeypatch):
    from config.settings import EXPORTS_DIR
    monkeypatch.setattr(sys.modules[__name__], "NEWS_ITEM", dict(NEWS_ITEM, lead_magnet_preset="../../Evil Preset/x"))
    body, _ = _synthesize_with_ticker()
    slug, output_dir = slow_generation["lead_magnet"]
    assert slug == "______evil_preset_x" and body["result"]["lead_magnet_preset"] == slug
    assert output_dir.resolve().is_relative_to(EXPORTS_DIR.resolve())
