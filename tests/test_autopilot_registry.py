"""
core/autopilot.py over publishers/registry: publishers built by the registry factory (the old local
get_publisher called EditorialPitcher(platform, cfg) — TypeError for tbank/biz360/rb), platforms
limited to registry.enabled(profile), only `ready` articles published, TG posts from the profile.
Uses only example profiles; real .env, data/ and the network are not touched.
"""
import sys
import asyncio
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.autopilot as autopilot  # noqa: E402
import core.profile as profile_module  # noqa: E402
from core.models import Article, PublishResult  # noqa: E402
from core.profile import load_profile  # noqa: E402

PROFILES = PROJECT_ROOT / "profiles"
ALL_KEYS = ["vc", "spark", "dzen", "executive", "tbank", "biz360", "rb"]


def _article(platform: str, status: str = "ready") -> Article:
    return Article(id=f"art-{platform}-{status}", campaign_id="camp1", platform=platform, title=f"T {platform}",
                   lead="", content_markdown="body", content_html="<p>body</p>", tags="", status=status)


class FakeDb:
    def __init__(self, articles):
        self.articles = articles
        self.updates = []

    def list_articles_by_campaign(self, campaign_id):
        return [a for a in self.articles if a.campaign_id == campaign_id]

    def update_article_status(self, article_id, status, **kwargs):
        self.updates.append((article_id, status, kwargs))


def _profile(platforms=None, name="example-person"):
    profile = load_profile(PROFILES / f"{name}.yaml")
    if platforms is not None:
        for key in ALL_KEYS:
            profile.platforms.pop(key, None)
        for key in platforms:
            profile.platforms[key] = profile_module.PlatformSettings(enabled=True)
    return profile


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from config.settings import Settings
    monkeypatch.setattr(Settings, "env_path", tmp_path / "absent.env")  # never the real .env
    for name in ("OPP_BOT_TOKEN", "SERM_TG_BOT_TOKEN", "OPP_CHANNEL", "AUTOPILOT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(autopilot, "STATE_FILE", tmp_path / "autopilot_state.json")

    async def no_sleep(*args, **kwargs):
        return None

    async def no_alert(**kwargs):
        alerts.append(kwargs)

    alerts = []
    monkeypatch.setattr(autopilot.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(autopilot, "send_telegram_alert", no_alert)
    monkeypatch.setattr(autopilot, "FastIndexer", lambda: type("I", (), {"ping_all": lambda *a: None})())
    return alerts


@pytest.fixture
def published(monkeypatch):
    calls = []

    def fake_publish(self, article):
        calls.append((type(self).__name__, self.platform_name, article.platform))
        return PublishResult(success=True, platform=article.platform, article_id=article.id, status="submitted")

    from publishers.editorial_pitcher import EditorialPitcher
    monkeypatch.setattr(EditorialPitcher, "publish", fake_publish)
    return calls


@pytest.mark.parametrize("platform", ["tbank", "biz360", "rb"])
def test_editorial_platform_publishes(monkeypatch, published, isolated, platform):
    # Regression: constructed through the profile resolver, as scheduler/admin do (AutopilotEngine(campaign_id))
    monkeypatch.setattr(profile_module, "get_profile", lambda spec=None: _profile(ALL_KEYS))
    engine = autopilot.AutopilotEngine("camp1")
    engine.db = FakeDb([_article(platform)])

    res = asyncio.run(engine.step_article_publication())

    assert "error" not in res, res
    assert published == [("EditorialPitcher", platform, platform)]
    assert engine.db.updates == [(f"art-{platform}-ready", "submitted", {"published_url": None})]
    assert isolated == []


def test_only_enabled_platforms(published):
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(["vc"]))
    engine.db = FakeDb([_article("rb"), _article("tbank")])

    assert asyncio.run(engine.step_article_publication()) is None
    assert published == []
    assert engine.get_status()["ready_count"] == 0


def test_draft_is_not_published(published):
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(ALL_KEYS))
    engine.db = FakeDb([_article("rb", status="draft"), _article("tbank", status="ready")])

    res = asyncio.run(engine.step_article_publication())

    assert res["platform"] == "tbank"
    assert [c[2] for c in published] == ["tbank"]
    assert all(update[0] != "art-rb-draft" for update in engine.db.updates)


def test_defaults_from_profile_and_env(monkeypatch):
    profile = _profile()
    engine = autopilot.AutopilotEngine(profile=profile)
    engine.db = FakeDb([])
    assert engine.campaign_id == profile.campaign_id
    status = engine.get_status()
    assert status["target_channel"] == profile.telegram.channel
    assert status["bot_configured"] is False and status["enabled"] is False

    monkeypatch.setenv("OPP_CHANNEL", "@env_channel")
    monkeypatch.setenv("AUTOPILOT_ENABLED", "true")
    assert engine.get_status()["target_channel"] == "@env_channel"
    assert engine.is_enabled() is True


def test_telegram_post_from_profile_templates(monkeypatch, tmp_path):
    sent = []

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, **kwargs):
            sent.append(kwargs)

        async def close(self):
            pass

    import aiogram
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setenv("OPP_BOT_TOKEN", "123456:" + "x" * 35)
    profile = _own_posts_profile(tmp_path, "example-company")
    engine = autopilot.AutopilotEngine(profile=profile)

    res = asyncio.run(engine.step_telegram_post())

    assert res["success"] is True
    assert sent[0]["chat_id"] == profile.telegram.channel
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>" in sent[0]["text"] and "**" not in sent[0]["text"]
    assert autopilot.AutopilotState.load()["next_tg_post_idx"] == 1


def _own_posts_profile(tmp_path, name):
    """Copy of an example profile that ships its own telegram templates (as a real install would)."""
    import shutil
    shutil.copy(PROFILES / f"{name}.yaml", tmp_path / f"{name}.yaml")
    shutil.copytree(PROJECT_ROOT / "templates" / "telegram", tmp_path / name / "templates" / "telegram")
    return load_profile(tmp_path / f"{name}.yaml")


def test_builtin_scaffold_is_never_posted_to_channel(monkeypatch):
    sent = []

    class FakeBot:
        def __init__(self, token):
            self.session = self

        async def send_message(self, **kwargs):
            sent.append(kwargs)

        async def close(self):
            pass

    import aiogram
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setenv("OPP_BOT_TOKEN", "123456:" + "x" * 35)
    engine = autopilot.AutopilotEngine(profile=_profile(name="example-company"))

    assert asyncio.run(engine.step_telegram_post()) is None
    assert sent == []


def test_broken_posts_template_alerts_once_and_marks_slot(monkeypatch, tmp_path, isolated):
    profile = _own_posts_profile(tmp_path, "example-company")
    (profile.templates_path / "telegram" / "posts.yaml.j2").write_text("posts: {{ no_such_var }}\n", encoding="utf-8")
    monkeypatch.setenv("OPP_BOT_TOKEN", "123456:" + "x" * 35)
    monkeypatch.setenv("AUTOPILOT_ENABLED", "true")
    monday_slot = autopilot.datetime(2026, 1, 5, 11, 5, tzinfo=autopilot.timezone.utc)  # a Monday
    monkeypatch.setattr(autopilot, "get_msk_now", lambda: monday_slot)
    ticks = []

    async def three_ticks(*args, **kwargs):
        ticks.append(1)
        if len(ticks) >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(autopilot.asyncio, "sleep", three_ticks)
    engine = autopilot.AutopilotEngine(profile=profile)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autopilot.periodic_autopilot_worker(engine, check_interval_seconds=1))

    assert len(isolated) == 1 and "posts.yaml.j2" in isolated[0]["context"]
    assert autopilot.AutopilotState.load()["last_tg_slot"] == "2026-01-05_mon_insight"
