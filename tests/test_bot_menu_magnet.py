"""Funnel entry with `magnet: true`: the button sends the lead magnet PDF in the bot.

Attribution must survive the click (the article slug stays the user's source), the menu is not sent
twice, and right after a PDF the menu hides the magnet button.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_templates import _run, _start, _callback, _pdf, _logged_slugs, profile, alerts  # noqa: E402,F401
import core.opp_bot as opp_bot  # noqa: E402

_real_buttons = opp_bot._buttons


def _with_magnet_entry(monkeypatch, profile):
    """Marks the first funnel entry of the built-in buttons as the magnet button; returns its id."""
    marked = {}

    def buttons(*a, **kw):
        data = _real_buttons(*a, **kw)
        data["funnel"][0]["magnet"] = True
        data["funnel"][0]["lead"] = False
        marked["id"] = data["funnel"][0]["id"]
        return data

    monkeypatch.setattr(opp_bot, "_buttons", buttons)
    buttons(profile, profile.telegram.channel)  # fills marked["id"] before the first update
    return marked


def _funnel_ids(message):
    return [row[0].callback_data for row in message.reply_markup.inline_keyboard]


def test_organic_menu_magnet_button_sends_pdf_once(profile, alerts, monkeypatch, tmp_path):
    marked = _with_magnet_entry(monkeypatch, profile)
    logged = _logged_slugs(monkeypatch)
    exports = _pdf(profile, tmp_path)
    opp_bot.user_pending_magnet.clear()

    calls = _run(profile, [_start(5001, "")], subscribed={5001}, exports_dir=exports)
    funnel = calls[-1][1]
    assert f"tier_{marked['id']}" in _funnel_ids(funnel)  # organic menu offers the free material

    calls = _run(profile, [_callback(5001, f"tier_{marked['id']}")], subscribed={5001}, exports_dir=exports)
    names = [n for n, _ in calls]
    assert names == ["AnswerCallbackQuery", "SendDocument"], names  # PDF, and no second menu
    assert profile.lead_magnet().title in calls[1][1].caption
    assert not alerts  # not a lead
    assert ("free_click", opp_bot.ORGANIC_SLUG) in logged  # not a lead in analytics
    assert not [e for e in logged if e[0] == "tier_click"]
    assert ("pdf_delivered", opp_bot.ORGANIC_SLUG) in logged  # under the visit source, no phantom "general" row
    assert opp_bot.user_pending_magnet[5001] == ""  # still an organic visitor


def test_article_visitor_menu_hides_magnet_and_keeps_attribution(profile, alerts, monkeypatch, tmp_path):
    marked = _with_magnet_entry(monkeypatch, profile)
    logged = _logged_slugs(monkeypatch)
    exports = _pdf(profile, tmp_path)
    slug = f"vc_{profile.lead_magnet().slug}"

    calls = _run(profile, [_start(5101, slug)], subscribed={5101}, exports_dir=exports)
    assert [n for n, _ in calls] == ["SendDocument", "SendMessage"]
    ids = _funnel_ids(calls[1][1])
    assert f"tier_{marked['id']}" not in ids and ids  # PDF already sent: no "get the PDF" button

    lead_id = ids[0][len("tier_"):]
    calls = _run(profile, [_callback(5101, f"tier_{lead_id}")], subscribed={5101}, exports_dir=exports)
    assert ("tier_click", slug) in logged  # the article stays the source
    assert len(alerts) == 1 and slug in alerts[0]["error_message"]


def test_magnet_click_after_article_keeps_slug(profile, alerts, monkeypatch, tmp_path):
    marked = _with_magnet_entry(monkeypatch, profile)
    logged = _logged_slugs(monkeypatch)
    exports = _pdf(profile, tmp_path)
    slug = f"dzen_{profile.lead_magnet().slug}"
    _run(profile, [_start(5201, slug)], subscribed={5201}, exports_dir=exports)
    calls = _run(profile, [_callback(5201, f"tier_{marked['id']}")], subscribed={5201}, exports_dir=exports)
    assert [n for n, _ in calls] == ["AnswerCallbackQuery", "SendDocument"]
    assert opp_bot.user_pending_magnet[5201] == slug
    assert ("free_click", slug) in logged and ("pdf_delivered", slug) in logged


def test_builtin_menu_unchanged_without_magnet_entries(profile, alerts, tmp_path):
    # Profiles without `magnet:` in buttons keep the old menu after a PDF: every tier button stays
    calls = _run(profile, [_start(5301, profile.lead_magnet().slug)], subscribed={5301},
                 exports_dir=_pdf(profile, tmp_path))
    assert _funnel_ids(calls[1][1]) == [f"tier_{t.id}" for t in profile.offer_tiers if t.id != "diy"]


def test_magnet_button_requires_subscription(profile, alerts, monkeypatch, tmp_path):
    marked = _with_magnet_entry(monkeypatch, profile)
    logged = _logged_slugs(monkeypatch)
    calls = _run(profile, [_callback(5401, f"tier_{marked['id']}")], subscribed=(), exports_dir=_pdf(profile, tmp_path))
    assert [n for n, _ in calls] == ["AnswerCallbackQuery"] and calls[0][1].show_alert
    assert not logged
