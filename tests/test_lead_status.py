"""
Lead status: the owner marks in Telegram what happened after a lead click (contacted -> call -> result -> money),
the admin panel reads it. State is one row per person in `leads`; `bot_events` stays an untouched log.
Only ADMIN_TG_ID may change a status; without it the alert goes out exactly as before.
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.db import Database  # noqa: E402

# bot_events as the live installation has it before this feature (core/db.py at 812ba3b)
OLD_SCHEMA = """
CREATE TABLE campaigns (id TEXT PRIMARY KEY, brand_name TEXT NOT NULL, target_query TEXT NOT NULL,
    seed_topic TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE bot_events (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT, first_name TEXT,
    event_type TEXT NOT NULL, slug TEXT, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
"""
OLD_EVENTS = [
    ("e1", 11, "anna", "Анна", "start", "vc_audit", "User entered via /start", "2026-10-01 09:00:00"),
    ("e2", 11, "anna", "Анна", "tier_click", "vc_audit", "tier_turnkey", "2026-10-01 09:05:00"),
    ("e3", 11, "anna", "Анна", "tier_click", "dzen_audit", "tier_workshop", "2026-10-02 10:00:00"),
    ("e4", 12, None, "Борис", "tier_click", "", "tier_diagnostic", "2026-10-03 11:00:00"),
    ("e5", 13, "vera", "Вера", "free_click", "vc_audit", "tier_channel", "2026-10-03 12:00:00"),
    ("e6", 13, "vera", "Вера", "pdf_delivered", "vc_audit", "x.pdf", "2026-10-03 12:00:01"),
]


def _old_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany("INSERT INTO bot_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", OLD_EVENTS)
    conn.execute("INSERT INTO campaigns (id, brand_name, target_query, seed_topic) VALUES ('c1', 'b', 'q', 's')")
    conn.commit()
    conn.close()
    return path


def _rows(path: Path, sql: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migration_backfills_leads_from_live_db_without_touching_events(tmp_path):
    path = _old_db(tmp_path / "serm.db")
    events_before = _rows(path, "SELECT * FROM bot_events ORDER BY rowid")

    Database(path)

    assert _rows(path, "SELECT * FROM bot_events ORDER BY rowid") == events_before
    assert _rows(path, "SELECT id FROM campaigns") == [("c1",)]
    assert _rows(path, "PRAGMA user_version") == [(1,)]
    leads = _rows(path, "SELECT user_id, username, first_name, slug, interest, status, created_at, reminded_status "
                        "FROM leads ORDER BY user_id")
    # first lead click of each person; a free click is not a lead; old leads are not reminded about
    assert leads == [
        (11, "anna", "Анна", "vc_audit", "tier_turnkey", "new", "2026-10-01 09:05:00", "new"),
        (12, None, "Борис", "general", "tier_diagnostic", "new", "2026-10-03 11:00:00", "new"),
    ]
    # two independent counts agree (the deploy acceptance check)
    assert _rows(path, "SELECT COUNT(*) FROM leads") == _rows(
        path, "SELECT COUNT(DISTINCT user_id) FROM bot_events WHERE event_type = 'tier_click'")


def test_migration_runs_once(tmp_path):
    path = _old_db(tmp_path / "serm.db")
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM leads WHERE user_id = 12")  # a second backfill would bring it back
    conn.commit()
    conn.close()
    Database(path)
    assert _rows(path, "SELECT user_id FROM leads") == [(11,)]
    assert _rows(path, "PRAGMA user_version") == [(1,)]


def test_fresh_db_gets_empty_leads(tmp_path):
    path = tmp_path / "new.db"
    Database(path)
    assert _rows(path, "SELECT COUNT(*) FROM leads") == [(0,)]
    assert _rows(path, "PRAGMA user_version") == [(1,)]


# --- Bot: lead click, owner buttons, access ---

import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from aiogram.types import Chat, Message, Update, User  # noqa: E402

import core.alerts  # noqa: E402
import core.lead_status as lead_status  # noqa: E402
import core.opp_bot as opp_bot  # noqa: E402
from core.profile import PROFILES_DIR, load_profile  # noqa: E402
from test_bot_templates import DATE, _callback, _run, _start, installation, profile  # noqa: E402,F401

ADMIN = 4242


@pytest.fixture
def alerts(monkeypatch):
    """Alert recorder that also sees reply_markup (new kwarg)."""
    sent = []

    async def record(title, error_message, context=None, level="error", **kw):
        sent.append({"title": title, "error_message": error_message, "context": context, **kw})
        return False

    monkeypatch.setattr(core.alerts, "send_telegram_alert", record)
    return sent


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(core.alerts, "get_admin_id", lambda: ADMIN)
    lead_status.awaiting_amount.clear()
    yield ADMIN
    lead_status.awaiting_amount.clear()


def _uid(base, profile):
    return base + (profile.kind == "company")


def _lead_tier(profile):
    return next(t for t in profile.offer_tiers if t.id != "diy")


def _lead_of(user_id):
    with Database()._conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def _text(uid, text, upd=None):
    return Update(update_id=upd or uid + 7, message=Message(
        message_id=uid, date=DATE, chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Хозяин"), text=text))


def _buttons(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


def _answers(calls):
    return [m.text for n, m in calls if n == "AnswerCallbackQuery"]


def _click(profile, uid, slug):
    """A visitor from an article: /start <slug>, then the lead button."""
    _run(profile, [_start(uid, slug), _callback(uid, f"tier_{_lead_tier(profile).id}")])
    return _lead_of(uid)


def test_lead_click_with_admin_creates_lead_and_status_buttons(profile, alerts, admin):
    uid = _uid(9101, profile)
    lead = _click(profile, uid, "vc_t2")
    assert lead["slug"] == "vc_t2" and lead["status"] == "new" and lead["interest"] == f"tier_{_lead_tier(profile).id}"
    (alert,) = alerts
    assert _buttons(alert["reply_markup"]) == [f"lead:{lead['id']}:contacted", f"lead:{lead['id']}:no_contact"]


def test_lead_click_without_admin_alert_unchanged(profile, alerts, monkeypatch):
    monkeypatch.setattr(core.alerts, "get_admin_id", lambda: None)
    uid = _uid(9201, profile)
    lead = _click(profile, uid, "vc_t3")
    assert lead is not None and lead["slug"] == "vc_t3"
    (alert,) = alerts
    assert "reply_markup" not in alert  # the old call, byte for byte


def test_owner_walks_lead_to_sale_and_analytics_count_it(installation, alerts, admin):
    profile = installation
    uid = _uid(9301, profile)
    slug = f"t4_{profile.kind}"
    lead = _click(profile, uid, slug)
    before = next(r for r in Database().get_slug_breakdown() if r["slug"] == slug)
    lid = lead["id"]
    calls = _run(profile, [_callback(ADMIN, f"lead:{lid}:{a}") for a in
                           ("contacted", "call_set", "call_done", "sale:turnkey")])
    assert {k: v[0] for k, v in lead_status.awaiting_amount.items()} == {ADMIN: lid}
    edited = [m for n, m in calls if n == "EditMessageText"]
    assert len(edited) == 4 and _buttons(edited[-1].reply_markup) == [f"lead:{lid}:noamount"]
    _run(profile, [_text(ADMIN, "150 000")])

    lead = _lead_of(uid)
    assert (lead["status"], lead["product"], lead["amount"]) == ("sale", "turnkey", 150000)
    assert all(lead[c] for c in ("contacted_at", "call_set_at", "call_done_at", "closed_at"))
    assert lead_status.awaiting_amount == {}
    after = next(r for r in Database().get_slug_breakdown() if r["slug"] == slug)
    assert {k: after[k] for k in ("contacted", "calls_set", "calls_done", "sales", "revenue")} == {
        "contacted": 1, "calls_set": 1, "calls_done": 1, "sales": 1, "revenue": 150000}
    old_keys = set(before) - {"contacted", "calls_set", "calls_done", "sales", "revenue"}
    assert old_keys == {"slug", "topic_name", "starts", "subs", "cr_sub", "pdfs", "leads", "cr_lead", "efficiency"}
    assert {k: after[k] for k in old_keys} == {k: before[k] for k in old_keys}  # bot_events counts untouched
    summary = Database().get_funnel_summary()
    assert summary["unique_sales"] >= 1 and summary["revenue"] >= 150000


def test_non_admin_cannot_change_status_or_amount(profile, alerts, admin):
    uid = _uid(9401, profile)
    lid = _click(profile, uid, "vc_t5")["id"]
    # the lead himself (or anyone) forges the owner's callback
    calls = _run(profile, [_callback(uid, f"lead:{lid}:contacted")])
    assert _lead_of(uid)["status"] == "new"
    assert _answers(calls) == [None] and [n for n, _ in calls] == ["AnswerCallbackQuery"]

    _run(profile, [_callback(ADMIN, f"lead:{lid}:{a}") for a in ("contacted", "call_set", "call_done")])
    _run(profile, [_callback(ADMIN, f"lead:{lid}:sale:other")])
    assert {k: v[0] for k, v in lead_status.awaiting_amount.items()} == {ADMIN: lid}
    calls = _run(profile, [_text(uid, "5000"), _callback(uid, f"lead:{lid}:noamount")])
    assert _lead_of(uid)["amount"] is None
    assert {k: v[0] for k, v in lead_status.awaiting_amount.items()} == {ADMIN: lid}  # the owner is still asked
    assert "SendMessage" not in [n for n, _ in calls]


def test_owner_in_a_group_is_not_the_owner_chat(profile, alerts, admin):
    uid = _uid(9451, profile)
    lid = _click(profile, uid, "vc_t5g")["id"]
    upd = _callback(ADMIN, f"lead:{lid}:contacted")
    upd.callback_query.message.chat.__dict__["type"] = "group"
    _run(profile, [upd])
    assert _lead_of(uid)["status"] == "new"


def test_stale_button_does_not_double_a_stage(profile, alerts, admin):
    uid = _uid(9501, profile)
    lid = _click(profile, uid, "vc_t6")["id"]
    _run(profile, [_callback(ADMIN, f"lead:{lid}:contacted")])
    first = _lead_of(uid)
    calls = _run(profile, [_callback(ADMIN, f"lead:{lid}:no_contact"), _callback(ADMIN, f"lead:{lid}:contacted")])
    lead = _lead_of(uid)
    assert (lead["status"], lead["contacted_at"]) == ("contacted", first["contacted_at"])
    assert all(t and t.startswith("Уже:") for t in _answers(calls))
    # the card is redrawn with the buttons of the real status
    assert _buttons([m for n, m in calls if n == "EditMessageText"][-1].reply_markup) == [
        f"lead:{lid}:call_set", f"lead:{lid}:no_sale"]


def test_double_tap_race_applies_once(tmp_path):
    db = Database(tmp_path / "race.db")
    lid = db.upsert_lead(1, "u", "f", "s", "tier_x")
    assert db.set_lead_status(lid, "new", "contacted", "contacted_at") is True
    assert db.set_lead_status(lid, "new", "contacted", "contacted_at") is False
    assert db.upsert_lead(1, "u2", "f2", "other_slug", "tier_y") == lid
    assert db.get_lead(lid)["slug"] == "s"  # first attribution kept
    with pytest.raises(ValueError):
        db.set_lead_status(lid, "contacted", "x", "status; DROP TABLE leads")


def test_restart_keeps_article_slug_for_lead(profile, alerts, admin):
    uid = _uid(9601, profile)
    _run(profile, [_start(uid, "dzen_t7")])
    opp_bot.user_pending_magnet.clear()  # deploy between the visit and the click
    _run(profile, [_callback(uid, f"tier_{_lead_tier(profile).id}")])
    assert _lead_of(uid)["slug"] == "dzen_t7"
    with Database()._conn() as conn:
        assert conn.execute("SELECT slug FROM bot_events WHERE user_id = ? AND event_type = 'tier_click'",
                            (uid,)).fetchone()[0] == "dzen_t7"


def test_leads_command_only_for_owner(profile, alerts, admin):
    uid = _uid(9701, profile)
    lid = _click(profile, uid, "vc_leads")["id"]
    assert _run(profile, [_text(uid, "/leads")]) == []
    calls = _run(profile, [_text(ADMIN, "/leads")])
    cards = [m for n, m in calls if n == "SendMessage"]
    assert any(f"lead:{lid}:contacted" in _buttons(m.reply_markup) for m in cards)


def test_number_without_waiting_is_not_consumed(profile, alerts, admin):
    assert _run(profile, [_text(ADMIN, "150000")]) == []


# --- Reminders ---

def _remind(profile, db, now, monkeypatch):
    sent = []

    async def send(text, reply_markup=None):
        sent.append((text, reply_markup))
        return True

    monkeypatch.setattr(core.alerts, "send_admin_message", send)
    asyncio.run(lead_status.remind_due(profile, db, now))
    return sent


def _age(db, lid, when):
    with db._conn() as conn:
        conn.execute("UPDATE leads SET status_at = ? WHERE id = ?", (when.strftime("%Y-%m-%d %H:%M:%S"), lid))


def test_reminders_once_per_status_and_only_in_daytime(profile, admin, monkeypatch, tmp_path):
    db = Database(tmp_path / "remind.db")
    noon = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)  # 12:00 at UTC+3 (profile default)
    lid = db.upsert_lead(1, "u", "f", "vc", "tier_x")
    _age(db, lid, noon - timedelta(hours=23))
    assert _remind(profile, db, noon, monkeypatch) == []
    _age(db, lid, noon - timedelta(hours=25))
    late = noon + timedelta(hours=11)  # 23:00 local
    assert _remind(profile, db, late, monkeypatch) == []
    (text, markup), = _remind(profile, db, noon, monkeypatch)
    assert "Человек вышел на связь?" in text and _buttons(markup)[0] == f"lead:{lid}:contacted"
    assert _remind(profile, db, noon, monkeypatch) == []

    db.set_lead_status(lid, "new", "contacted", "contacted_at")
    _age(db, lid, noon - timedelta(hours=100))
    assert _remind(profile, db, noon, monkeypatch) == []  # contacted is not reminded about
    db.set_lead_status(lid, "contacted", "call_set", "call_set_at")
    _age(db, lid, noon - timedelta(hours=49))
    assert len(_remind(profile, db, noon, monkeypatch)) == 1
    assert _remind(profile, db, noon, monkeypatch) == []


def test_no_reminders_without_admin(profile, monkeypatch, tmp_path):
    monkeypatch.setattr(core.alerts, "get_admin_id", lambda: None)
    db = Database(tmp_path / "remind.db")
    lid = db.upsert_lead(1, "u", "f", "vc", "tier_x")
    _age(db, lid, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert _remind(profile, db, datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc), monkeypatch) == []


# --- Templates ---

def _profiles():
    own = [p for p in sorted(PROFILES_DIR.glob("*.yaml")) if not p.stem.startswith("example")]
    return [PROFILES_DIR / "example-person.yaml", PROFILES_DIR / "example-company.yaml", *own]


@pytest.mark.parametrize("path", _profiles(), ids=lambda p: p.stem)
def test_templates_render_short_callbacks_and_escape(path):
    profile = load_profile(path)
    texts = lead_status.load_texts(profile)
    assert texts["products"] and set(texts["reminders"]) == {"new", "call_set", "thinking"}
    for status in lead_status.TRANSITIONS:
        assert status in texts["labels"]
        for awaiting in (False, True):
            lead = {"id": 10 ** 9, "status": status, "amount": None}
            for data in _buttons(lead_status.keyboard(texts, lead, awaiting)):
                assert len(data.encode()) <= 64, data
    lead = {"id": 7, "user_id": 1, "username": None, "first_name": "<b>Иван & Co</b>", "slug": "<i>x</i>",
            "interest": "tier_x", "status": "sale", "created_at": "2026-10-18 09:00:00",
            "closed_at": "2026-10-18 10:00:00", "product": texts["products"][0]["id"], "amount": 150000}
    card = lead_status.card_text(profile, texts, lead, {"tier_x": "<u>Интерес</u>"})
    assert "&lt;b&gt;Иван &amp; Co&lt;/b&gt;" in card and "<b>Иван" not in card
    assert "&lt;i&gt;x&lt;/i&gt;" in card and "&lt;u&gt;Интерес&lt;/u&gt;" in card
    assert "150 000" in card and "18.10 12:00" in card  # local time (UTC+3)



@pytest.mark.parametrize("text, amount", [
    ("150000", 150000), ("150 000", 150000), ("150к", 150000), ("150 К", 150000), ("150 000 ₽", 150000),
    ("1,5 млн", 1500000), ("90 тыс.", 90000), ("45000 руб", 45000)])
def test_parse_amount_accepts_how_people_write(text, amount):
    assert lead_status.parse_amount(text) == amount


@pytest.mark.parametrize("text", ["сто тысяч", "0", "", "1" * 13, "150 000 долларов"])
def test_parse_amount_rejects(text):
    assert lead_status.parse_amount(text) is None


def _to_amount_question(profile, uid, base):
    lid = _click(profile, uid, f"vc_{base}")["id"]
    _run(profile, [_callback(ADMIN, f"lead:{lid}:{a}") for a in ("contacted", "call_set", "call_done", "sale:other")])
    return lid


def test_unparsed_amount_is_answered_and_still_asked(profile, alerts, admin):
    uid = _uid(9701, profile)
    lid = _to_amount_question(profile, uid, "t9")
    calls = _run(profile, [_text(ADMIN, "сто тысяч")])
    assert _lead_of(uid)["amount"] is None
    assert any(n == "SendMessage" and "Не понял" in m.text for n, m in calls)
    assert {k: v[0] for k, v in lead_status.awaiting_amount.items()} == {ADMIN: lid}
    _run(profile, [_text(ADMIN, "150к")])
    assert _lead_of(uid)["amount"] == 150000


def test_amount_question_expires(profile, alerts, admin, monkeypatch):
    uid = _uid(9801, profile)
    lid = _to_amount_question(profile, uid, "t10")
    lead_status.awaiting_amount[ADMIN] = (lid, lead_status.awaiting_amount[ADMIN][1] - lead_status.AMOUNT_TTL_S - 1)
    calls = _run(profile, [_text(ADMIN, "5000")])
    assert _lead_of(uid)["amount"] is None and lead_status.awaiting_amount == {}
    assert any(n == "SendMessage" and "истекло" in m.text for n, m in calls)


def test_no_contact_lead_can_be_closed_as_no_sale(profile, alerts, admin):
    uid = _uid(9901, profile)
    lid = _click(profile, uid, "vc_t11")["id"]
    _run(profile, [_callback(ADMIN, f"lead:{lid}:no_contact"), _callback(ADMIN, f"lead:{lid}:no_sale")])
    assert _lead_of(uid)["status"] == "no_sale"
