"""Regression (pre-release review B-1): the organic marker must survive a bot restart."""
import sys, asyncio, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bot_templates import _run, _start, _callback, _pdf, _logged_slugs, profile, alerts, RecordingSession, Bot  # noqa
import core.opp_bot as opp_bot
import core.templating as templating


def test_organic_gate_then_restart_then_check_sub(profile, alerts, monkeypatch, tmp_path):
    logged = _logged_slugs(monkeypatch)
    exports = _pdf(profile, tmp_path)
    calls = _run(profile, [_start(2001, "")], exports_dir=exports)  # gate shown
    gate = calls[-1][1]
    print("gate button:", [(b.text, b.callback_data) for row in gate.reply_markup.inline_keyboard for b in row])
    opp_bot.user_pending_magnet.clear()  # bot restart: in-memory dict is gone
    pressed = [b.callback_data for row in gate.reply_markup.inline_keyboard for b in row][-1]  # what the user taps
    calls = _run(profile, [_callback(2001, pressed)], subscribed={2001}, exports_dir=exports)
    names = [n for n, _ in calls]
    print("after restart:", names, logged)
    assert "SendDocument" not in names, "organic gate delivered PDF after restart"


def test_slug_start_unchanged(profile, alerts, monkeypatch, tmp_path):
    logged = _logged_slugs(monkeypatch)
    exports = _pdf(profile, tmp_path)
    m = profile.lead_magnet()
    calls = _run(profile, [_start(2101, m.slug)], subscribed={2101}, exports_dir=exports)
    assert calls[0][0] == "SendDocument"
    calls = _run(profile, [_start(2102, m.slug)], exports_dir=exports)
    calls = _run(profile, [_callback(2102, "check_sub")], subscribed={2102}, exports_dir=exports)
    assert calls and any(n == "SendDocument" for n, _ in calls)


def test_broken_organic_template(profile, alerts, monkeypatch, tmp_path):
    real = templating.render
    def render(name, *a, **kw):
        if name == "bot/organic_welcome.html.j2":
            raise templating.__dict__.get("TemplateError", RuntimeError)("broken")  if False else RuntimeError("broken organic_welcome")
        return real(name, *a, **kw)
    monkeypatch.setattr(opp_bot, "render", render)
    exports = _pdf(profile, tmp_path)
    err = None
    try:
        calls = _run(profile, [_start(2201, ""), _start(2202, profile.lead_magnet().slug)], subscribed={2202}, exports_dir=exports)
    except Exception as e:
        err = e
    print("feed_update raised:", repr(err), "alerts:", alerts)
