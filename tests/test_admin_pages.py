"""
Admin panel on a neutral (example) profile: pages are rendered from templates/admin/* with
brand values from the profile, no personal markers, AGPL source link in the footer;
drafts are approved via POST /api/articles/approve and are never published before that.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Article  # noqa: E402
from core.profile import get_profile  # noqa: E402
from test_no_personal import find_markers, private_markers  # noqa: E402

TOKEN = os.environ["ADMIN_TOKEN"]
# Markers of the owner installation (private profile + .local/markers.txt, outside git; empty in a public checkout)
MARKERS = private_markers()


@pytest.fixture
def example_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", str(PROJECT_ROOT / "profiles" / "example-person.yaml"))
    for key in ("SERM_ACTIVE_CAMPAIGN", "OPP_CHANNEL", "OPP_CONTACT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / "dotenv")
    get_profile.cache_clear()
    yield get_profile()
    get_profile.cache_clear()


def _run(coro_fn):
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            return await coro_fn(client)
    return asyncio.run(main())


def test_neutral_pages_render_profile_values(example_profile):
    async def fetch(client):
        login = await (await client.get("/")).text()
        page = await (await client.get(f"/admin?token={TOKEN}")).text()
        return login, page
    login, page = _run(fetch)

    assert f"<title>Вход в панель управления | {example_profile.display_name}</title>" in login
    assert example_profile.display_name in page
    assert f'id="channelStatus">{example_profile.telegram.channel}<' in page
    assert f'const CAMPAIGN_ID = "{example_profile.campaign_id}";' in page
    for lm in example_profile.lead_magnets:
        assert f'<option value="{lm.slug}">' in page
    assert "AGPL-3.0-or-later" in page and f'<a href="{admin.SOURCE_CODE_URL}"' in page
    assert find_markers(login, MARKERS) == []
    assert find_markers(page, MARKERS) == []


def _add(db, article_id, status, platform="vc"):
    db.add_article(Article(id=article_id, campaign_id="approve01", platform=platform, title="T", lead="L",
                           content_markdown="body", content_html="<p>body</p>", tags="", status=status))


def test_approve_draft_to_ready(example_profile):
    db = Database()
    _add(db, "draft-1", "draft")
    _add(db, "pub-1", "published", platform="spark")

    async def flow(client):
        h = {"X-Admin-Token": TOKEN}
        ok = await client.post("/api/articles/approve", json={"article_id": "draft-1"}, headers=h)
        again = await client.post("/api/articles/approve", json={"article_id": "draft-1"}, headers=h)
        published = await client.post("/api/articles/approve", json={"article_id": "pub-1"}, headers=h)
        missing = await client.post("/api/articles/approve", json={"article_id": "nope"}, headers=h)
        anon = await client.post("/api/articles/approve", json={"article_id": "draft-1"})
        return ok.status, again.status, published.status, missing.status, anon.status

    assert _run(flow) == (200, 409, 409, 404, 401)
    assert db.get_article("draft-1").status == "ready"
    assert db.get_article("pub-1").status == "published"


def test_draft_is_not_published(example_profile):
    db = Database()
    _add(db, "draft-2", "draft", platform="dzen")

    async def flow(client):
        h = {"X-Admin-Token": TOKEN}
        single = await client.post("/api/publish/article", json={"article_id": "draft-2"}, headers=h)
        bulk = await client.post("/api/publish/campaign", json={"campaign_id": "approve01"}, headers=h)
        return single.status, await bulk.json()

    single_status, bulk = _run(flow)
    assert single_status == 409
    assert all(r["platform"] != "dzen" for r in bulk["results"])
    assert db.get_article("draft-2").status == "draft"


@pytest.mark.parametrize("status, url, pinged", [
    ("draft_saved", "https://vc.ru/drafts", False),
    ("published", "https://vc.ru/u/1-x/123-t", True),  # positive control: the observation sees a ping
])
def test_only_published_article_is_indexed(example_profile, monkeypatch, status, url, pinged):
    from core.models import PublishResult
    db = Database()
    campaign = f"index-{status}"
    # separate articles: after the single publish its article is no longer publishable from the admin
    for article_id, campaign_id in ((f"idx-{status}", f"single-{status}"), (f"idx-bulk-{status}", campaign)):
        db.add_article(Article(id=article_id, campaign_id=campaign_id, platform="vc", title="T", lead="L",
                               content_markdown="body", content_html="<p>body</p>", tags="", status="ready"))

    class FakePublisher:
        def publish(self, article):
            return PublishResult(success=True, platform="vc", article_id=article.id, published_url=url, status=status)

    pings = []
    monkeypatch.setattr(admin, "get_publisher", lambda platform: FakePublisher())
    monkeypatch.setattr(admin, "FastIndexer", lambda: type("I", (), {"ping_all": lambda self, *a: pings.append(a)})())

    async def flow(client):
        h = {"X-Admin-Token": TOKEN}
        single = await client.post("/api/publish/article", json={"article_id": f"idx-{status}"}, headers=h)
        await asyncio.sleep(0.2)  # the single-article ping runs as a background task
        bulk = await client.post("/api/publish/campaign", json={"campaign_id": campaign}, headers=h)
        return single.status, bulk.status

    assert _run(flow) == (200, 200)
    assert pings == ([(f"idx-{status}", "T", url), (f"idx-bulk-{status}", "T", url)] if pinged else [])


@pytest.mark.parametrize("status, phrase", [
    ("published", "уже опубликована"),
    ("draft_saved", "допубликуйте её в кабинете площадки"),
])
def test_article_already_on_platform_is_not_republished(example_profile, monkeypatch, status, phrase):
    # Republishing creates a second copy on the platform (Dzen: a public duplicate)
    db = Database()
    campaign = f"again-{status}"
    db.add_article(Article(id=f"again-{status}", campaign_id=campaign, platform="vc", title="T", lead="L",
                           content_markdown="body", content_html="<p>body</p>", tags="", status=status,
                           published_url="https://vc.ru/x"))
    calls = []
    monkeypatch.setattr(admin, "get_publisher", lambda platform: calls.append(platform))

    async def flow(client):
        h = {"X-Admin-Token": TOKEN}
        single = await client.post("/api/publish/article", json={"article_id": f"again-{status}"}, headers=h)
        bulk = await client.post("/api/publish/campaign", json={"campaign_id": campaign}, headers=h)
        return single.status, await single.json(), await bulk.json()

    single_status, single, bulk = _run(flow)
    assert single_status == 409 and phrase in single["error"] and "https://vc.ru/x" in single["error"]
    assert [(r["platform"], r["success"]) for r in bulk["results"]] == [("vc", False)]
    assert phrase in bulk["results"][0]["error"]
    assert calls == []
    assert db.get_article(f"again-{status}").status == status


@pytest.mark.parametrize("status", ["submitted", "exported"])
def test_editorial_pitch_can_be_prepared_again(status):
    # a pitch is a local package, nothing is created on the platform: no duplicate to guard against
    article = Article(id="pitch-1", campaign_id="c", platform="tbank", title="T", lead="L", content_markdown="b",
                      content_html="", tags="", status=status)
    assert admin.republish_error(article) is None
    assert admin.republish_error(Article(**{**article.__dict__, "platform": "vc"})) is not None
