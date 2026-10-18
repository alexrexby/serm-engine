"""
OPP bot and bot analytics over example profiles (person / company): lead magnets, deep-link matching,
offer tiers and texts come from the profile and templates/bot/**, never from the code;
user input is HTML-escaped; the milestone alert carries no token; analytics survive any /start slug.
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetChatMember, GetMe  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, Message, Update, User,
)

import core.alerts  # noqa: E402
import core.opp_bot as opp_bot  # noqa: E402
from core.db import Database  # noqa: E402
from core.profile import PROFILES_DIR, get_profile, load_profile  # noqa: E402
from test_no_personal import find_markers, private_markers  # noqa: E402

DATE = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
BOT_USER = User(id=999, is_bot=True, first_name="Bot", username="example_bot")
# Markers of the owner installation (outside git; empty in a public checkout) + what no installation may leak
PERSONAL = private_markers()
LEAKS = ("token=",)


@pytest.fixture(params=["example-person", "example-company"])
def profile(request):
    return load_profile(PROFILES_DIR / f"{request.param}.yaml")


class RecordingSession(BaseSession):
    def __init__(self, subscribed):
        super().__init__()
        self.calls = []
        self.subscribed = subscribed

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="u")
            return ChatMemberMember(user=user) if method.user_id in self.subscribed else ChatMemberLeft(user=user)
        self.calls.append((type(method).__name__, method))
        if getattr(method, "__returning__", None) is Message:
            return Message(message_id=1, date=DATE, chat=Chat(id=getattr(method, "chat_id", 0), type="private"))
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        if False:
            yield b""


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def record(title, error_message, context=None, level="error"):
        sent.append({"title": title, "error_message": error_message, "context": context})
        return False

    monkeypatch.setattr(core.alerts, "send_telegram_alert", record)
    return sent


def _start(uid, slug, first_name="Иван", username="user1"):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=DATE, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name=first_name, username=username),
        text=f"/start {slug}".strip()))


def _callback(uid, data, username=None):
    return Update(update_id=uid + 1, callback_query=CallbackQuery(
        id=f"cb{uid}", from_user=User(id=uid, is_bot=False, first_name="Пётр", username=username),
        chat_instance="ci", data=data,
        message=Message(message_id=uid, date=DATE, chat=Chat(id=uid, type="private"), from_user=BOT_USER, text="menu")))


def _run(profile, updates, subscribed=(), exports_dir=None):
    session = RecordingSession(set(subscribed))
    bot = Bot(token="123456:test-fake-token-aaaaaaaaaaaaaaaaaaaaaaaaaa", session=session)
    dp = opp_bot.create_opp_dispatcher(profile.telegram.channel, exports_dir, profile.telegram.contact, profile)

    async def main():
        for u in updates:
            await dp.feed_update(bot, u)
        await asyncio.sleep(0)

    asyncio.run(main())
    return session.calls


def _texts(calls):
    out = []
    for _, m in calls:
        for attr in ("text", "caption"):
            if getattr(m, attr, None):
                out.append(getattr(m, attr))
        markup = getattr(m, "reply_markup", None)
        for row in (markup.inline_keyboard if markup else []):
            out.extend(b.text for b in row)
    return "\n".join(out)


def test_resolve_magnet_from_profile_match(profile):
    magnet = profile.lead_magnets[0]
    info = opp_bot.resolve_magnet_info(f"x_{magnet.match[-1]}_y", None, profile)
    assert (info["slug"], info["title"], info["filename"]) == (magnet.slug, magnet.title, magnet.download_filename)
    unknown = opp_bot.resolve_magnet_info("zzz", None, profile)
    assert unknown["slug"] == "zzz" and unknown["title"] == profile.lead_magnet().title
    assert opp_bot.resolve_magnet_info("", None, profile)["slug"] == "general"


def test_resolve_magnet_finds_default_pdf(profile, tmp_path):
    (tmp_path / "campaign_x").mkdir()
    pdf = tmp_path / "campaign_x" / profile.lead_magnet().file_prefix
    pdf.write_bytes(b"%PDF")
    assert opp_bot.resolve_magnet_info("zzz", tmp_path, profile)["pdf_path"] == pdf


def test_gate_for_unsubscribed_escapes_user_name(profile, alerts):
    calls = _run(profile, [_start(101, "audit", first_name="<b>Иван & Co</b>")])
    (name, msg), = calls
    assert name == "SendMessage"
    assert "&lt;b&gt;Иван &amp; Co&lt;/b&gt;" in msg.text and "<b>Иван" not in msg.text
    assert profile.lead_magnet("audit").title in msg.text
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    assert buttons[0].url == f"https://t.me/{profile.telegram.channel[1:]}"
    assert buttons[1].callback_data == "check_sub"


def test_subscribed_gets_magnet_and_profile_offer_tiers(profile, alerts):
    calls = _run(profile, [_start(201, "audit")], subscribed={201})
    caption, funnel = calls[0][1], calls[1][1]
    assert profile.lead_magnet("audit").title in caption.text
    tiers = [t for t in profile.offer_tiers if t.id != "diy"]
    keyboard = [row[0] for row in funnel.reply_markup.inline_keyboard]
    assert [b.callback_data for b in keyboard] == [f"tier_{t.id}" for t in tiers]
    assert [b.text for b in keyboard] == [t.cta or t.title for t in tiers]
    for tier in profile.offer_tiers:
        assert tier.title in funnel.text


def test_tier_click_alerts_and_links_from_profile(profile, alerts):
    for tier in [t for t in profile.offer_tiers if t.id != "diy"]:
        alerts.clear()
        calls = _run(profile, [_callback(301, f"tier_{tier.id}", username=None)])
        answer = calls[-1][1]
        assert tier.title in answer.text
        button = answer.reply_markup.inline_keyboard[0][0]
        assert button.url == (tier.url or f"https://t.me/{profile.telegram.contact[1:]}")
        assert len(alerts) == 1 and f"Интерес: {tier.title}" in alerts[0]["error_message"]
        assert "Пётр (ID: 301)" in alerts[0]["error_message"]


def test_unknown_tier_is_ignored(profile, alerts):
    calls = _run(profile, [_callback(401, "tier_nope")])
    assert [n for n, _ in calls] == ["AnswerCallbackQuery"] and not alerts


def test_no_owner_texts_and_voice(profile, alerts):
    calls = _run(profile, [_start(501, "audit"), _start(502, "zzz"), _callback(501, "check_sub")]
                 + [_callback(503, f"tier_{t.id}") for t in profile.offer_tiers], subscribed={502})
    text = _texts(calls) + "\n".join(a["error_message"] for a in alerts)
    assert find_markers(text, PERSONAL) == []
    for leak in LEAKS:
        assert leak not in text
    for phrase in (("меня зовут", "пишите мне") if profile.kind == "company" else ("наша команда",)):
        assert phrase not in text.lower()


def test_milestone_alert_has_no_token(profile, alerts, monkeypatch):
    monkeypatch.setattr(Database, "get_funnel_summary", lambda self: {"unique_starts": 50})
    monkeypatch.delenv("PUBLIC_ADMIN_URL", raising=False)
    _run(profile, [_start(601, "audit")])
    assert len(alerts) == 1 and "50" in alerts[0]["title"]
    assert "http" not in alerts[0]["error_message"] and "token" not in alerts[0]["error_message"]
    alerts.clear()
    monkeypatch.setenv("PUBLIC_ADMIN_URL", "https://admin.example.com")
    _run(profile, [_start(602, "audit")])
    assert alerts[0]["error_message"].endswith(": https://admin.example.com")


def test_failed_lead_and_milestone_alerts_are_logged(profile, alerts, monkeypatch, caplog):
    # The lead alert is the bot's money signal: a broken alert template must leave a trace in the log
    def broken(*args, **kwargs):
        raise KeyError("lead")

    monkeypatch.setattr(opp_bot, "_alert", broken)
    monkeypatch.setattr(Database, "get_funnel_summary", lambda self: {"unique_starts": 50})
    lead = next(t for t in profile.offer_tiers if t.id != "diy")
    with caplog.at_level("ERROR", logger="OPPBot"):
        calls = _run(profile, [_start(701, "audit"), _callback(702, f"tier_{lead.id}")])
    failed = [r for r in caplog.records if r.name == "OPPBot" and r.exc_info]
    assert {"milestone", "lead"} <= {word for r in failed for word in r.getMessage().split()}
    assert lead.title in calls[-1][1].text  # the user still gets the tier answer


def test_start_slug_is_bounded_before_db(profile, alerts, monkeypatch):
    # A deep link limits the payload, but /start can be typed by hand: bot_events.slug stays short and plain
    logged = []
    monkeypatch.setattr(Database, "log_bot_event", lambda self, **kw: logged.append(kw["slug"]))
    hostile = "audit<script>alert(1)</script>" + "Я" * 10 + "z" * 200
    _run(profile, [_start(801, hostile), _start(802, "fiscal_tax-2"), _start(803, "  ")])
    assert all(len(s) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", s) for s in logged), logged
    assert logged[0].startswith("auditscriptalert1script")
    assert "fiscal_tax-2" in logged and "general" in logged
    assert set(opp_bot.user_pending_magnet[uid] for uid in (801, 802, 803)) <= set(logged)


def test_dispatcher_refuses_profile_without_lead_magnets(profile):
    empty = profile.model_copy(update={"lead_magnets": [], "default_lead_magnet": None})
    with pytest.raises(Exception, match="lead_magnets"):
        opp_bot.create_opp_dispatcher("@example_channel", None, "@example_contact", empty)


@pytest.fixture
def installation(monkeypatch, profile):
    monkeypatch.setenv("SERM_PROFILE", str(profile.source_path))
    get_profile.cache_clear()
    yield profile
    get_profile.cache_clear()


def test_analytics_topic_names_and_hostile_slug(installation, tmp_path):
    db = Database(tmp_path / "t.db")
    hostile = 'x": {{ 7*7 }}\n# - y'
    for uid, slug in enumerate(["audit"] * 12 + [hostile] * 40 + ["general"]):
        db.log_bot_event(user_id=uid, username="u", first_name="f", event_type="start", slug=slug, details="")
    names = {row["slug"]: row["topic_name"] for row in db.get_slug_breakdown()}
    assert names["audit"] == installation.lead_magnet("audit").topic_name
    assert hostile in names[hostile] and "49" not in names[hostile]
    recs = db.get_content_strategy_recommendations()
    assert recs[0]["type"] == "sample_emerging" and "53" in recs[0]["title"]
    joined = " ".join(r["title"] + r["text"] for r in recs)
    assert "54-ФЗ" not in joined and "fiscal_tax" not in joined


def test_analytics_launch_recommendations_neutral(installation, tmp_path):
    recs = Database(tmp_path / "t.db").get_content_strategy_recommendations()
    assert [r["type"] for r in recs] == ["launch", "content_plan", "magnet"]
    assert f"?start={installation.lead_magnets[0].slug}" in recs[2]["text"]
