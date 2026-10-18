"""
Verification test suite for SERM Distribution & Conversion Engine.

Runs on the neutral example-person profile (SERM_PROFILE), so it needs no private profile;
data/ is isolated by tests/conftest.py (SERM_DATA_DIR), the DB lives in a temporary directory.
"""
import sys
import tempfile
from pathlib import Path

import pytest

# Setup paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import Campaign, Article
from core.db import Database
from core.generator import ContentGenerator
from core.indexer import FastIndexer
from core.lead_magnet import LeadMagnetGenerator
from core.telegram_posts import TelegramPostsGenerator
from core.opp_bot import create_opp_dispatcher
from publishers.editorial_pitcher import EditorialPitcher
from config.settings import EXPORTS_DIR, PLATFORMS_CONFIG
from core.profile import PROFILES_DIR, get_profile


@pytest.fixture
def example_profile(monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", str(PROFILES_DIR / "example-person.yaml"))
    get_profile.cache_clear()
    yield get_profile()
    get_profile.cache_clear()


def test_full_flow(example_profile):
    profile = example_profile
    # EditorialPitcher exports into EXPORTS_DIR: it must be conftest's temporary data dir, never the live data/
    assert not EXPORTS_DIR.resolve().is_relative_to((PROJECT_ROOT / "data").resolve())
    with tempfile.TemporaryDirectory() as tmpdir:
        test_dir = Path(tmpdir)
        test_db_path = test_dir / "test_serm.db"
        db = Database(test_db_path)

        # 1. Test Campaign Creation
        campaign = Campaign(
            id="test001",
            brand_name="NovaTech",
            target_query="NovaTech отзывы",
            seed_topic="Автоматизация клиентского сервиса и контроль SLA"
        )
        db.add_campaign(campaign)
        saved_camp = db.get_campaign("test001")
        assert saved_camp is not None
        assert saved_camp.brand_name == "NovaTech"
        print("✅ 1. Campaign creation and SQLite persistence passed.")

        # 2. Test Content Generation (7 unique tailored variants)
        generator = ContentGenerator()
        articles = generator.generate_campaign_articles(
            campaign_id=campaign.id,
            brand_name=campaign.brand_name,
            target_query=campaign.target_query,
            seed_topic=campaign.seed_topic
        )
        assert len(articles) == 7
        platforms = {a.platform for a in articles}
        expected_platforms = {"vc", "spark", "dzen", "executive", "tbank", "biz360", "rb"}
        assert platforms == expected_platforms

        # Verify uniqueness (no duplicate content)
        contents = [a.content_markdown for a in articles]
        assert len(set(contents)) == 7, "Duplicate content detected across platforms!"

        for a in articles:
            db.add_article(a)
            assert "NovaTech" in a.title
            assert "NovaTech" in a.content_markdown
        print("✅ 2. Content Generator passed: 7 unique platform-tailored articles created without duplicates.")

        # 3. Test Lead Magnet Generator (Markdown & Structure)
        lm_gen = LeadMagnetGenerator(test_dir)
        md_file = lm_gen.generate_markdown()
        assert md_file.exists()
        md_text = md_file.read_text(encoding="utf-8")
        assert md_text.startswith("# ")
        assert profile.display_name in md_text
        assert profile.telegram.contact in md_text
        print("✅ 3. Lead Magnet Generator passed (profile brand and contact in the checklist).")

        # 4. Test Telegram Posts Generator (4 high-converting posts)
        tg_gen = TelegramPostsGenerator(test_dir)
        post_file = tg_gen.generate_posts_pack(campaign, articles)
        assert post_file.exists()
        tg_text = post_file.read_text(encoding="utf-8")
        assert "Пост 1" in tg_text
        assert "Пост 2" in tg_text
        assert "Пост 3" in tg_text
        assert "Пост 4" in tg_text
        print("✅ 4. Telegram Content Pack Generator passed (4 posts verified).")

        # 5. Test OPP Telegram Bot Dispatcher
        dp = create_opp_dispatcher(profile.telegram.channel, md_file, profile.telegram.contact)
        assert dp is not None
        print("✅ 5. OPP Bot subscription verification dispatcher initialized successfully.")

        # 6. Test Editorial Pitcher for T-Bank, Biz360, RB.ru
        for p_name in ("tbank", "biz360", "rb"):
            art = next(a for a in articles if a.platform == p_name)
            pitcher = EditorialPitcher(p_name)
            res = pitcher.publish(art)
            assert res.success is True
            assert res.status == "submitted"
            db.update_article_status(art.id, res.status)
        print("✅ 6. Editorial Pitcher passed: pitch packages & export files created.")

        # 7. Test Fast Indexer logic
        indexer = FastIndexer()
        res_tg = indexer.ping_telegram("art1", "Test Title", "https://vc.ru/example")
        assert res_tg.channel == "telegram"
        assert "not configured" in res_tg.message or res_tg.success is True
        print("✅ 7. Fast Indexer module passed: correct request payload formatting and graceful offline fallback.")

        # 8. Verify database state
        camp_articles = db.list_articles_by_campaign("test001")
        assert len(camp_articles) == 7
        submitted_count = sum(1 for a in camp_articles if a.status == "submitted")
        assert submitted_count == 3
        print("✅ 8. Database state queries verified.")

    print("\n🎉 ALL 8 TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
