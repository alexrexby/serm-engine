"""
Lead magnets over example profiles: presets, keywords and the default file come from the profile and
templates/lead_magnet/**; web HTML is escaped; a topic from the admin panel or RSS cannot break the YAML data.
"""
import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.lead_magnet as lead_magnet  # noqa: E402
from core.profile import PROFILES_DIR, get_profile, load_profile  # noqa: E402
from test_no_personal import find_markers, private_markers  # noqa: E402

# Markers of the owner installation (outside git; empty in a public checkout) + owner-niche terms
PERSONAL = private_markers()
NICHE = ("54-ФЗ",)
HOSTILE_TOPIC = 'Возвраты "под ключ": <script>alert(1)</script> {{ 7*7 }} 🚀\n# - x'


@pytest.fixture(params=["example-person", "example-company"])
def profile(request):
    return load_profile(PROFILES_DIR / f"{request.param}.yaml")


@pytest.fixture
def pdf_log(monkeypatch):
    log = []

    class Page:
        async def set_content(self, html, wait_until=None):
            self.html = html

        async def pdf(self, path, **opts):
            log.append(self.html)
            Path(path).write_bytes(b"%PDF-test\n")

    class Browser:
        async def new_page(self):
            return Page()

        async def close(self):
            pass

    class Chromium:
        async def launch(self, **kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(lead_magnet, "async_playwright", lambda: Playwright())
    return log


def test_presets_follow_profile_lead_magnets(profile):
    presets, keywords = lead_magnet.load_presets(profile)
    assert list(presets) == [m.template.rsplit("/", 1)[-1] for m in profile.lead_magnets]
    magnet = profile.lead_magnet()
    preset = presets[lead_magnet.preset_key(magnet)]
    assert preset["title"] == magnet.title and preset["blocks"]


def test_markdown_and_html_use_profile_and_offer(profile, tmp_path):
    gen = lead_magnet.DynamicLeadMagnetGenerator(tmp_path, profile)
    preset = lead_magnet.load_presets(profile)[0]["checklist"]
    md = gen.render_markdown(preset, profile.display_name, profile.telegram.contact)
    html = gen.render_html(preset, profile.display_name, profile.telegram.contact)
    for text in (md, html):
        assert profile.display_name in text and profile.lead_magnet().title in text
        for tier in profile.offer_tiers:
            assert tier.title in text
        assert find_markers(text, PERSONAL) == []
        for term in NICHE:
            assert term not in text
    assert f"(https://t.me/{profile.telegram.contact[1:]})" in md


def test_generate_custom_topic_is_escaped_and_safe(profile, tmp_path, pdf_log):
    gen = lead_magnet.DynamicLeadMagnetGenerator(tmp_path, profile)
    res = asyncio.run(gen.generate_for_topic(HOSTILE_TOPIC))
    assert res["preset_key"] == "checklist" and HOSTILE_TOPIC.strip() in res["title"]
    html = pdf_log[0]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "{{ 7*7 }}" in res["markdown"].read_text(encoding="utf-8")  # topic is data, not a template
    default = profile.lead_magnet().file_prefix
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        {"lead_magnet_checklist.md", "lead_magnet_checklist.pdf", default, Path(default).with_suffix(".md").name})


def test_preset_topic_and_detection(profile, tmp_path, pdf_log):
    gen = lead_magnet.DynamicLeadMagnetGenerator(tmp_path, profile)
    assert gen.detect_preset_key("что угодно") == "checklist"
    res = asyncio.run(gen.generate_for_topic("checklist"))
    assert res["title"] == profile.lead_magnet().title
    assert profile.telegram.contact in res["markdown"].read_text(encoding="utf-8")


def test_module_presets_mapping_reads_installation_profile(profile, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", str(profile.source_path))
    get_profile.cache_clear()
    try:
        assert list(lead_magnet.LEAD_MAGNET_TOPIC_PRESETS) == ["checklist"]
        assert "checklist" in lead_magnet.LEAD_MAGNET_TOPIC_PRESETS
    finally:
        get_profile.cache_clear()
