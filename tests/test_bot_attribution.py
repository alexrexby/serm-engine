"""Every event of an article visitor is logged under the article's /start slug, also across a bot restart."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_templates import _run, _start, _callback, _pdf, profile, alerts  # noqa: E402,F401
import core.opp_bot as opp_bot  # noqa: E402
from core.db import Database  # noqa: E402


def _events(user_id):
    with Database()._conn() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT event_type, slug FROM bot_events WHERE user_id = ? ORDER BY created_at, rowid", (user_id,))]


def test_article_visitor_events_keep_the_platform_slug(profile, alerts, tmp_path):
    uid = 7101 + (profile.kind == "company")
    slug = f"vc_{profile.lead_magnet().slug}"
    exports = _pdf(profile, tmp_path)
    _run(profile, [_start(uid, slug)], exports_dir=exports)
    _run(profile, [_callback(uid, "check_sub")], subscribed={uid}, exports_dir=exports)
    assert _events(uid) == [("start", slug), ("check_sub_attempt", slug), ("subscribed", slug), ("pdf_delivered", slug)]


def test_restart_between_gate_and_check_keeps_slug(profile, alerts, tmp_path):
    uid = 7201 + (profile.kind == "company")
    slug = f"dzen_{profile.lead_magnet().slug}"
    exports = _pdf(profile, tmp_path)
    _run(profile, [_start(uid, slug)], exports_dir=exports)
    opp_bot.user_pending_magnet.clear()  # bot restart
    calls = _run(profile, [_callback(uid, "check_sub")], subscribed={uid}, exports_dir=exports)
    assert any(n == "SendDocument" for n, _ in calls)
    assert _events(uid)[-2:] == [("subscribed", slug), ("pdf_delivered", slug)]


def test_restart_before_lead_click_keeps_slug(profile, alerts, tmp_path):
    uid = 7301 + (profile.kind == "company")
    slug = f"spark_{profile.lead_magnet().slug}"
    _run(profile, [_start(uid, slug)], subscribed={uid}, exports_dir=_pdf(profile, tmp_path))
    opp_bot.user_pending_magnet.clear()  # deploy/restart before the click
    tier = next(t for t in profile.offer_tiers if t.id != "diy")
    _run(profile, [_callback(uid, f"tier_{tier.id}")], subscribed={uid})
    assert _events(uid)[-1] == ("tier_click", slug)
    assert slug in alerts[-1]["error_message"]


def test_organic_restart_stays_organic_for_leads(profile, alerts):
    uid = 7401 + (profile.kind == "company")
    _run(profile, [_start(uid, "")], subscribed={uid})
    opp_bot.user_pending_magnet.clear()
    tier = next(t for t in profile.offer_tiers if t.id != "diy")
    _run(profile, [_callback(uid, f"tier_{tier.id}")], subscribed={uid})
    assert _events(uid)[-1] == ("tier_click", opp_bot.ORGANIC_SLUG)


def test_organic_restart_old_check_sub_button_gives_no_pdf(profile, alerts, tmp_path):
    uid = 7501 + (profile.kind == "company")
    exports = _pdf(profile, tmp_path)
    _run(profile, [_start(uid, "")], exports_dir=exports)
    opp_bot.user_pending_magnet.clear()
    calls = _run(profile, [_callback(uid, "check_sub")], subscribed={uid}, exports_dir=exports)
    assert "SendDocument" not in [n for n, _ in calls]
