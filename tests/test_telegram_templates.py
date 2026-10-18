"""
core/telegram_posts.py over templates/telegram/**: neutral posts render on the example profiles,
profile override templates (profiles/<name>/templates/telegram/) replace the built-in ones,
bot deep links come from the profile. The owner's profile is never required: its brand strings
(if a private profile is present) are only used as markers that must not leak into the neutral output.
"""
import sys
import shutil
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.profile import load_profile  # noqa: E402
from core.telegram_posts import TelegramPostsGenerator, load_posts  # noqa: E402
from core.models import Campaign  # noqa: E402

PROFILES = PROJECT_ROOT / "profiles"
TEMPLATES = PROJECT_ROOT / "templates" / "telegram"
EXAMPLES = ["example-person", "example-company"]


def _private_markers():
    markers = set()
    for path in PROFILES.glob("*.yaml"):
        if path.stem.startswith("example"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tg = data.get("telegram") or {}
        values = [data.get("name"), data.get("display_name"), data.get("campaign_id"),
                  tg.get("channel"), tg.get("contact"), tg.get("bot_username"), *(data.get("aliases") or [])]
        markers.update(v for v in values if v)
    return sorted(markers)


@pytest.mark.parametrize("name", EXAMPLES)
def test_neutral_pack_renders(tmp_path, name):
    profile = load_profile(PROFILES / f"{name}.yaml")
    path = TelegramPostsGenerator(tmp_path, profile=profile).generate_posts_pack()
    text = path.read_text(encoding="utf-8")

    assert path.name == "telegram_content_pack.md"
    assert text.startswith(f"# Пакет публикаций для Telegram-канала ({profile.display_name})\n")
    assert "{{" not in text and "{%" not in text
    posts = load_posts(profile)
    assert len(posts) == 5 and all(p["content"] for p in posts)
    assert text.count("```text\n") == len(posts)
    assert profile.telegram.contact in text
    for tier in profile.offer_tiers:
        assert tier.title in text


def test_campaign_brand_in_header(tmp_path):
    profile = load_profile(PROFILES / "example-company.yaml")
    campaign = Campaign(id="c1", brand_name="Другой Бренд", target_query="q", seed_topic="s")
    text = TelegramPostsGenerator(tmp_path, profile=profile).generate_posts_pack(campaign, []).read_text(encoding="utf-8")
    assert text.startswith("# Пакет публикаций для Telegram-канала (Другой Бренд)\n")


def test_voice_follows_kind():
    person = " ".join(p["content"] for p in load_posts(load_profile(PROFILES / "example-person.yaml")))
    company = " ".join(p["content"] for p in load_posts(load_profile(PROFILES / "example-company.yaml")))
    assert "Мои принципы" in person and "наша команда" not in person.lower()
    assert "Наши принципы" in company
    assert "меня зовут" not in company.lower() and "пишите мне" not in company.lower()


def test_deeplink_only_with_bot_username():
    profile = load_profile(PROFILES / "example-person.yaml")
    assert profile.telegram.bot_username is None
    assert "t.me/" not in " ".join(p["content"] for p in load_posts(profile))

    profile.telegram.bot_username = "demo_example_bot"
    magnet = profile.lead_magnet()
    assert f"(https://t.me/demo_example_bot?start={magnet.slug})" in load_posts(profile)[0]["content"]


def test_profile_override_templates(tmp_path):
    # profiles/<name>.yaml + profiles/<name>/templates/telegram/... with templates_dir left null
    shutil.copy(PROFILES / "example-person.yaml", tmp_path / "acme.yaml")
    override = tmp_path / "acme" / "templates" / "telegram"
    (override / "posts").mkdir(parents=True)
    (override / "posts.yaml.j2").write_text(
        "posts:\n  - id: only\n    title: \"Свой пост\"\n    template: telegram/posts/only.md.j2\n", encoding="utf-8")
    (override / "posts" / "only.md.j2").write_text("Привет от {{ profile.display_name }}\n", encoding="utf-8")
    profile = load_profile(tmp_path / "acme.yaml")
    assert profile.templates_dir is None

    assert load_posts(profile) == [{"id": "only", "title": "Свой пост", "content": "Привет от Иван Петров"}]
    text = TelegramPostsGenerator(tmp_path / "out", profile=profile).generate_posts_pack().read_text(encoding="utf-8")
    assert "## Свой пост\n\n```text\nПривет от Иван Петров\n```\n\n---\n" in text  # layout from built-in pack


def test_no_private_markers_in_neutral_output():
    markers = _private_markers()
    if not markers:
        pytest.skip("no private profile in profiles/ — nothing to compare against")
    sources = [p.read_text(encoding="utf-8") for p in TEMPLATES.rglob("*.j2")]
    for name in EXAMPLES:
        sources += [p["title"] + p["content"] for p in load_posts(load_profile(PROFILES / f"{name}.yaml"))]
    leaked = sorted({m for m in markers for text in sources if m in text})
    assert not leaked, f"private profile strings in neutral telegram templates/output: {leaked}"
