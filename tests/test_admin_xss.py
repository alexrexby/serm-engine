"""
Admin page in a real browser: data from the API (Telegram name and /start argument, RSS items,
LLM article titles and HTML, lead magnet names) is shown as text, never parsed as markup;
links from that data open only http(s). Checked on the neutral page and on the installation's
own profile page (profiles/<name>/templates/admin, skipped on a public checkout).

Grepping the JSON proves nothing here: the sink is innerHTML in the browser, so the test renders
the page in headless Chromium and counts executed handlers.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

async_playwright = pytest.importorskip("playwright.async_api").async_playwright

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from config.settings import EXPORTS_DIR  # noqa: E402
from core import radar  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Article  # noqa: E402
from core.profile import PROFILES_DIR, get_profile  # noqa: E402

TOKEN = os.environ["ADMIN_TOKEN"]
OWN_PROFILE = next((p for p in sorted(PROFILES_DIR.glob("*.yaml")) if not p.stem.startswith("example")), None)


def payload(tag: str) -> str:
    """Markup that runs `window.__xss.push(tag)` if it is ever parsed as HTML."""
    return f'<img src=x onerror="window.__xss.push(\'{tag}\')">'


JS_LINK = "javascript:window.__xss.push('href')"
ATTR_BREAK = '" onmouseover="window.__xss.push(\'attr\')" x="'


def _seed(profile) -> None:
    db = Database()
    db.log_bot_event(user_id=1, username=None, first_name=payload("first_name"), event_type="start",
                     slug=payload("slug"), details=payload("details"))
    db.log_bot_event(user_id=2, username=None, first_name="x", event_type=payload("event_type"))

    campaign = profile.campaign_id
    db.add_article(Article(id="xss-pub", campaign_id=campaign, platform="vc", title=payload("title"),
                           lead=payload("lead"), content_markdown="md", tags="", status="published",
                           published_url=JS_LINK,
                           content_html=f"<p class=\"lead\">Абзац</p>{payload('content_html')}"
                                        f"<script>window.__xss.push('script')</script>"
                                        f'<a href="{JS_LINK}">ссылка</a>'))
    db.add_article(Article(id="xss-fail", campaign_id=campaign, platform="spark", title="t", lead="",
                           content_markdown="md", content_html="", tags="", status="failed",
                           error_message=ATTR_BREAK))
    db.add_article(Article(id="xss-draft');window.__xss.push('id", campaign_id=campaign, platform="dzen",
                           title="t", lead="", content_markdown="md", content_html="", tags="", status="draft"))

    radar.CACHE_FILE.write_text(json.dumps([{
        "applied_title": payload("applied_title"), "title": payload("rss_title"), "link": JS_LINK,
        "source_name": payload("source"), "business_hole": payload("hole"),
        "vibecode_solution": payload("solution"), "financial_impact": payload("impact"),
        "cursor_prompt": payload("prompt"), "business_score": 9,
    }]), encoding="utf-8")

    camp_dir = EXPORTS_DIR / f"campaign_{campaign}"
    camp_dir.mkdir(parents=True, exist_ok=True)
    (camp_dir / f"lead_magnet_{payload('lm')}.pdf").write_bytes(b"%PDF")


@pytest.fixture(params=["example", "own"])
def profile(request, tmp_path, monkeypatch):
    if request.param == "own":
        if OWN_PROFILE is None:
            pytest.skip("no own profile (public checkout)")
        path = OWN_PROFILE
    else:
        path = PROFILES_DIR / "example-person.yaml"
    monkeypatch.setenv("SERM_PROFILE", str(path))
    monkeypatch.delenv("SERM_ACTIVE_CAMPAIGN", raising=False)
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / "dotenv")
    get_profile.cache_clear()
    prof = get_profile()
    _seed(prof)
    yield prof
    radar.CACHE_FILE.unlink(missing_ok=True)
    get_profile.cache_clear()


RENDER_ALL = """async () => {
  await loadArticles();
  await loadHorizonNews();
  await loadLeadMagnets();
  await loadAnalytics();
  await openArticleModal('xss-pub');
  await new Promise(r => setTimeout(r, 400));  // let broken images fire onerror
  const text = document.body.textContent;  // hidden tabs included
  const jsLinks = [...document.querySelectorAll('a[href]')].filter(a => /^\\s*javascript:/i.test(a.getAttribute('href'))).length;
  const injected = document.querySelectorAll('img[src="x"], [onerror], [onmouseover]').length
    + [...document.scripts].filter(s => s.textContent.includes("__xss.push('script')")).length;
  const previewParagraphs = document.querySelectorAll('#modalHtmlPreviewBox p.lead').length;
  document.querySelectorAll('[onmouseover]').forEach(el => el.dispatchEvent(new MouseEvent('mouseover')));
  // inline handlers with an id from the API: a quote in the id must not break out of the JS string
  document.querySelectorAll('button[id^="btnApprove_"]').forEach(b => b.click());
  await new Promise(r => setTimeout(r, 200));
  return {xss: window.__xss, text, jsLinks, injected, previewParagraphs};
}"""


def _render(profile):
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            async with async_playwright() as pw:
                try:
                    browser = await pw.chromium.launch()
                except Exception as e:  # no browser binaries installed
                    pytest.skip(f"chromium unavailable: {e}")
                page = await browser.new_page()
                await page.add_init_script("window.__xss = [];")
                await page.goto(str(client.make_url(f"/admin?token={TOKEN}")))
                result = await page.evaluate(RENDER_ALL)
                await browser.close()
                return result
    return asyncio.run(main())


def test_api_data_is_rendered_as_text(profile):
    result = _render(profile)

    assert result["xss"] == []
    assert result["jsLinks"] == 0
    assert result["injected"] == 0
    # positive control: every sink really received the payload and shows it as text
    for tag in ("first_name", "slug", "details", "event_type", "title", "applied_title",
                "rss_title", "source", "hole", "solution", "impact", "prompt", "lm"):
        assert payload(tag) in result["text"], tag
    # safe markup of the article preview is kept, with its classes
    assert result["previewParagraphs"] >= 1
