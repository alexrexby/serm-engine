"""
core/generator.py in template mode over example profiles: 7 platforms rendered from the neutral
built-in templates, the right voice (person -> «я», company -> «мы»), no foreign brand in the output,
and a profile's own templates override the built-in ones.
The installation profile's verbatim output is checked byte-for-byte by tests/test_golden.py.

Personal markers are never written here as literals (a public test must not publish them): they are
collected from the non-example profiles/*.yaml and .local/markers.txt, when those exist.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.generator import ContentGenerator  # noqa: E402
from core.profile import PROFILES_DIR, load_profile  # noqa: E402
from publishers.registry import PLATFORMS  # noqa: E402

EXAMPLES = ("example-person", "example-company")
TEMPLATE_DIRS = [PROJECT_ROOT / "templates" / "articles", PROJECT_ROOT / "templates" / "partials"]
HANDLE_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_]{4,32})")
TME_RE = re.compile(r"t\.me/([A-Za-z0-9_]+)")


def _private_markers() -> list:
    markers = set()
    for path in PROFILES_DIR.glob("*.yaml"):
        if path.stem.startswith("example"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tg = data.get("telegram") or {}
        values = [data.get("name"), data.get("display_name"), data.get("campaign_id"),
                  (data.get("deploy") or {}).get("server_url"), *(data.get("aliases") or []),
                  tg.get("channel"), tg.get("contact"), tg.get("bot_username")]
        markers.update(str(v).lstrip("@") for v in values if v)
    local = PROJECT_ROOT / ".local" / "markers.txt"
    if local.is_file():
        markers.update(line.strip() for line in local.read_text(encoding="utf-8").splitlines() if line.strip())
    return sorted(m for m in markers if len(m) >= 4)


def _generate(profile):
    gen = ContentGenerator(profile=profile)
    return gen.generate_campaign_articles(profile.campaign_id, profile.display_name,
                                          profile.seo.target_query, profile.seo.seed_topic)


def _text(articles) -> str:
    return "\n".join(f"{a.title}\n{a.lead}\n{a.tags}\n{a.content_markdown}\n{a.content_html}" for a in articles)


@pytest.fixture(params=EXAMPLES)
def example(request):
    return load_profile(PROFILES_DIR / f"{request.param}.yaml")


def test_all_registry_platforms_rendered(example):
    articles = _generate(example)
    assert [a.platform for a in articles] and set(a.platform for a in articles) == set(PLATFORMS)
    for a in articles:
        assert a.title and a.lead and a.tags and a.status == "ready"
        assert example.display_name in a.title
        assert a.content_markdown.startswith(f"# {a.title}\n")
        assert "## 1. " in a.content_markdown and "<h2>1. " in a.content_html
        assert example.telegram.contact in a.content_markdown and example.telegram.contact in a.content_html
        for tier in example.offer_tiers:
            assert tier.title in a.content_markdown and tier.title in a.content_html


def test_only_own_handles_and_links(example):
    """Every @handle and t.me link in the output belongs to the example profile itself."""
    own = {example.telegram.channel.lstrip("@"), example.telegram.contact.lstrip("@")}
    text = _text(_generate(example))
    found = set(HANDLE_RE.findall(text)) | set(TME_RE.findall(text))
    assert found, "positive control: the offer block must link the profile contact"
    assert found <= own, f"foreign handles in output: {sorted(found - own)}"


def test_no_private_markers_in_example_output(example):
    markers = _private_markers()
    if not markers:
        pytest.skip("no private profile / .local/markers.txt on this machine")
    text = _text(_generate(example)).lower()
    leaked = [m for m in markers if m.lower() in text]
    assert not leaked, f"personal markers in {example.slug} output: {leaked}"


def test_no_private_markers_in_neutral_templates():
    markers = _private_markers()
    if not markers:
        pytest.skip("no private profile / .local/markers.txt on this machine")
    files = [p for d in TEMPLATE_DIRS for p in d.rglob("*.j2")]
    assert files, f"no templates in {TEMPLATE_DIRS}"
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        leaked = [m for m in markers if m.lower() in text]
        assert not leaked, f"personal markers in {path.relative_to(PROJECT_ROOT)}: {leaked}"


@pytest.mark.parametrize("name, present, absent", [
    ("example-person", ["напишите мне", "Мои принципы", "Моё рабочее правило"],
     ["напишите нам", "Наши принципы", "Наше рабочее правило", "наша команда"]),
    ("example-company", ["напишите нам", "Наши принципы", "Наше рабочее правило"],
     ["напишите мне", "Мои принципы", "Моё рабочее правило", "меня зовут"]),
])
def test_voice_follows_profile_kind(name, present, absent):
    text = _text(_generate(load_profile(PROFILES_DIR / f"{name}.yaml")))
    for phrase in present:
        assert phrase in text, f"{name}: expected «{phrase}»"
    for phrase in absent:
        assert phrase.lower() not in text.lower(), f"{name}: unexpected «{phrase}»"


def test_explicit_pronoun_overrides_kind():
    profile = load_profile(PROFILES_DIR / "example-company.yaml")
    profile = profile.model_copy(update={"voice": profile.voice.model_copy(update={"pronoun": "я"})})
    text = _text(_generate(profile))
    assert "напишите мне" in text and "напишите нам" not in text


def test_offer_links_lead_magnet_through_bot_deeplink():
    profile = load_profile(PROFILES_DIR / "example-person.yaml")
    assert profile.telegram.bot_username is None
    assert "?start=" not in _text(_generate(profile))
    with_bot = profile.model_copy(update={"telegram": profile.telegram.model_copy(update={"bot_username": "example_bot"})})
    articles = _generate(with_bot)
    # one deep link per platform, <platform>_<lead magnet slug>: the bot sees which article sent the reader
    slugs = set()
    for a in articles:
        found = set(re.findall(r"t\.me/example_bot\?start=([A-Za-z0-9_-]+)", f"{a.content_markdown}\n{a.content_html}"))
        assert found == {f"{a.platform}_{profile.default_lead_magnet}"}, (a.platform, found)
        slugs |= found
    assert len(slugs) == len(articles) == len(PLATFORMS)


def test_profile_templates_override_builtin(tmp_path):
    override = tmp_path / "tpl"
    (override / "articles").mkdir(parents=True)
    (override / "articles" / "intro.md.j2").write_text("Своё вступление {{ brand }}\n", encoding="utf-8")
    profile = load_profile(PROFILES_DIR / "example-person.yaml")
    profile = profile.model_copy(update={"templates_dir": override})
    articles = _generate(profile)
    assert {a.lead for a in articles} == {f"Своё вступление {profile.display_name}"}
    # the rest still comes from the built-in templates
    assert all(a.content_markdown.count(profile.telegram.contact) for a in articles)
