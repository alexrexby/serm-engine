"""
Autopilot schedule from the profile (profile.schedule): the worker is driven minute by minute through a
frozen clock, so each test sees exactly which slots fire and when. No schedule section = the calendar that
was hardcoded in core/autopilot.py (articles Tue/Thu 10:30, TG Mon-Fri) with the same state keys.
Uses only example profiles; real .env, data/ and the network are not touched.
"""
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.autopilot as autopilot  # noqa: E402
from core.models import Article, PublishResult  # noqa: E402
from core.profile import ProfileError, load_profile  # noqa: E402

PROFILES = PROJECT_ROOT / "profiles"
MONDAY = datetime(2026, 10, 12)  # local wall-clock time of the schedule
WEEK_MINUTES = 7 * 24 * 60

# What the hardcoded calendar of e9e4bee fires over a week, recorded from that code (first matching minute)
CURRENT_WEEK = [
    ("Mon 11:00", "tg", "2026-10-12_mon_insight"),
    ("Tue 10:30", "article", "2026-10-13"),
    ("Tue 13:00", "tg", "2026-10-13_tue_teaser"),
    ("Wed 11:30", "tg", "2026-10-14_wed_case"),
    ("Thu 10:30", "article", "2026-10-15"),
    ("Thu 13:00", "tg", "2026-10-15_thu_teaser"),
    ("Fri 15:00", "tg", "2026-10-16_fri_vibecoding"),
]

WEEKDAY_SCHEDULE = {
    "articles": [{"days": ["mon", "tue", "wed", "thu", "fri"], "time": "10:30"}],
    "telegram": [
        {"day": "mon", "time": "11:00", "slot": "insight"},
        {"day": "tue", "time": "13:00", "slot": "teaser"},
        {"day": "wed", "time": "11:30", "slot": "case"},
        {"day": "thu", "time": "13:00", "slot": "teaser"},
        {"day": "fri", "time": "15:00", "slot": "vibecoding"},
    ],
}


class FakeDb:
    def __init__(self, articles):
        self.articles = articles

    def list_articles_by_campaign(self, campaign_id):
        return [a for a in self.articles if a.campaign_id == campaign_id]

    def update_article_status(self, article_id, status, **kwargs):
        pass  # the article stays `ready`: every due slot has something to publish


class StopWorker(Exception):
    """Raised by the fake sleep when the simulated period is over."""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from config.settings import Settings
    monkeypatch.setattr(Settings, "env_path", tmp_path / "absent.env")  # never the real .env
    for name in ("OPP_BOT_TOKEN", "SERM_TG_BOT_TOKEN", "OPP_CHANNEL", "AUTOPILOT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(autopilot, "STATE_FILE", tmp_path / "autopilot_state.json")

    alerts = []

    async def no_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(autopilot, "send_telegram_alert", no_alert)
    monkeypatch.setattr(autopilot, "FastIndexer", lambda: type("I", (), {"ping_all": lambda *a: None})())
    yield
    assert alerts == []


class Clock:
    """Frozen UTC clock for core.autopilot; the fake sleep advances it and ends the worker loop."""

    def __init__(self, monkeypatch):
        self.utc = None
        clock = self

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.utc if tz else clock.utc.replace(tzinfo=None)

        monkeypatch.setattr(autopilot, "datetime", FrozenDatetime)

    def run(self, engine, start_local, minutes, offset=3, interval=60):
        self.utc = (start_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
        end = self.utc + timedelta(minutes=minutes)

        async def tick(seconds):
            self.utc += timedelta(seconds=seconds)
            if self.utc >= end:
                raise StopWorker

        original = asyncio.sleep
        asyncio.sleep = tick
        try:
            with pytest.raises(StopWorker):
                asyncio.run(autopilot.periodic_autopilot_worker(engine, check_interval_seconds=interval))
        finally:
            asyncio.sleep = original


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def _profile(tmp_path, schedule=None):
    data = yaml.safe_load((PROFILES / "example-person.yaml").read_text(encoding="utf-8"))
    data.pop("schedule", None)
    if schedule is not None:
        data["schedule"] = schedule
    path = tmp_path / "brand.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_profile(path)


def _engine(monkeypatch, profile, fired, clock, offset=3, publish_minutes=0):
    """Real AutopilotEngine (real article-slot marking), fake publisher and TG send; records local fire times.
    publish_minutes: how long the publisher holds the worker (browser + retries); the article is recorded
    at the moment it was taken."""
    def local_label():
        return (clock.utc + timedelta(hours=offset)).strftime("%a %H:%M")

    class FakePublisher:
        def publish(self, article):
            fired.append((local_label(), "article"))
            clock.utc += timedelta(minutes=publish_minutes)
            return PublishResult(success=True, platform=article.platform, article_id=article.id, status="submitted")

    monkeypatch.setattr(autopilot, "get_publisher", lambda platform: FakePublisher())
    engine = autopilot.AutopilotEngine("camp1", profile=profile)
    engine.db = FakeDb([Article(id="a1", campaign_id="camp1", platform="vc", title="T", lead="",
                                content_markdown="b", content_html="<p>b</p>", tags="", status="ready")])
    engine.is_enabled = lambda: True

    async def fake_tg(specific_idx=None):
        fired.append((local_label(), "tg"))
        return {"success": True}

    engine.step_telegram_post = fake_tg
    return engine


def _with_keys(fired, saves):
    """Pairs every fire with the state key the slot was marked with (last_article_day / last_tg_slot)."""
    article_days = iter(s["last_article_day"] for s in saves if s["what"] == "last_article_day")
    tg_slots = iter(s["last_tg_slot"] for s in saves if s["what"] == "last_tg_slot")
    return [(when, kind, next(article_days if kind == "article" else tg_slots)) for when, kind in fired]


@pytest.fixture
def saves(monkeypatch):
    """Log of state marks as they are written: which slot key changed on each save."""
    log = []
    original = autopilot.AutopilotState.save
    seen = {}

    def spy(state):
        for key in ("last_article_day", "last_tg_slot"):
            if state.get(key) != seen.get(key):
                seen[key] = state.get(key)
                log.append({"what": key, key: state.get(key)})
        original(state)

    monkeypatch.setattr(autopilot.AutopilotState, "save", staticmethod(spy))
    return log


def test_no_schedule_section_fires_current_calendar(monkeypatch, tmp_path, clock, saves):
    fired = []
    engine = _engine(monkeypatch, _profile(tmp_path), fired, clock)

    clock.run(engine, MONDAY, WEEK_MINUTES)

    assert _with_keys(fired, saves) == CURRENT_WEEK


def test_weekday_schedule_articles_mon_to_fri(monkeypatch, tmp_path, clock, saves):
    fired = []
    engine = _engine(monkeypatch, _profile(tmp_path, WEEKDAY_SCHEDULE), fired, clock)

    clock.run(engine, MONDAY, WEEK_MINUTES)

    assert [f for f in _with_keys(fired, saves) if f[1] == "article"] == [
        (f"{day} 10:30", "article", f"2026-10-{12 + i}") for i, day in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"])
    ]
    assert [f for f in _with_keys(fired, saves) if f[1] == "tg"] == [f for f in CURRENT_WEEK if f[1] == "tg"]


def test_restart_inside_window_does_not_repeat(monkeypatch, tmp_path, clock, saves):
    fired = []
    profile = _profile(tmp_path, WEEKDAY_SCHEDULE)
    # Monday 10:30-10:59 is the article window and Monday 11:00-11:59 the TG window: the process
    # restarts in the middle of each, with the state file surviving (data/ is a volume)
    clock.run(_engine(monkeypatch, profile, fired, clock), MONDAY + timedelta(hours=10, minutes=30), 5)
    clock.run(_engine(monkeypatch, profile, fired, clock), MONDAY + timedelta(hours=10, minutes=40), 35)
    clock.run(_engine(monkeypatch, profile, fired, clock), MONDAY + timedelta(hours=11, minutes=20), 30)

    assert fired == [("Mon 10:30", "article"), ("Mon 11:00", "tg")]


def test_article_day_follows_schedule_offset(monkeypatch, tmp_path, clock, saves):
    # UTC+14: 10:30 local is still yesterday in MSK, the slot must be marked with the local date
    fired = []
    schedule = {"utc_offset_hours": 14, "articles": [{"days": ["mon"], "time": "10:30"}], "telegram": []}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock, offset=14)

    clock.run(engine, MONDAY + timedelta(hours=10), 120, offset=14)

    assert _with_keys(fired, saves) == [("Mon 10:30", "article", "2026-10-12")]


def test_status_and_summary(tmp_path):
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(tmp_path))
    engine.db = FakeDb([])
    assert engine.get_status()["schedule"] == (
        "Статьи: вт, чт 10:30; Telegram: пн 11:00, вт 13:00, ср 11:30, чт 13:00, пт 15:00 (UTC+3)")

    weekdays = _profile(tmp_path, WEEKDAY_SCHEDULE).schedule.summary()
    assert weekdays.startswith("Статьи: пн, вт, ср, чт, пт 10:30; ")


@pytest.mark.parametrize("schedule, message", [
    ({"articles": [{"days": ["tue"], "time": "10:30"}, {"days": ["tue"], "time": "16:00"}]}, "one article slot"),
    ({"articles": [{"days": ["tue", "tue"], "time": "10:30"}]}, "one article slot"),
    ({"articles": [{"days": ["вт"], "time": "10:30"}]}, "articles.0.days.0"),
    ({"articles": [{"days": ["tue"], "time": "25:00"}]}, "HH:MM"),
    ({"articles": [{"days": ["tue"], "time": 630}]}, 'quoted "HH:MM"'),  # YAML reads unquoted 10:30 as 630
    ({"articles": [{"days": [], "time": "10:30"}]}, "articles.0.days"),
    ({"telegram": [{"day": "mon", "time": "11:00", "slot": "a"}, {"day": "mon", "time": "11:30", "slot": "b"}]},
     "less than 60 min apart"),
    # windows past midnight overlap the next day's slots: one state key would alternate between them
    ({"telegram": [{"day": "sun", "time": "23:30", "slot": "a"}, {"day": "mon", "time": "00:10", "slot": "b"}]},
     "less than 60 min apart"),
    ({"articles": [{"days": ["mon"], "time": "23:30"}, {"days": ["tue"], "time": "00:10"}]},
     "less than 60 min apart"),
    ({"telegram": [{"day": "mon", "time": "11:00", "slot": "a"}, {"day": "mon", "time": "15:00", "slot": "a"}]},
     "duplicate"),
    ({"telegram": [{"day": "mon", "time": "11:00", "slot": "Bad Slot"}]}, "slot"),
    ({"utc_offset_hours": 15}, "utc_offset_hours"),
    ({"utc_offset_hours": True}, "utc_offset_hours"),  # YAML `true` must not become UTC+1
    ({"utc_offset_hours": "3"}, "utc_offset_hours"),
    ({"articles": [{"days": ["tue"], "time": "10:30"}], "weekly": True}, "weekly"),
])
def test_invalid_schedule_is_profile_error(tmp_path, schedule, message):
    with pytest.raises(ProfileError, match=message):
        _profile(tmp_path, schedule)


def test_unquoted_yaml_time_is_profile_error(tmp_path):
    text = (PROFILES / "example-person.yaml").read_text(encoding="utf-8")
    path = tmp_path / "brand.yaml"
    path.write_text(text + "\nschedule:\n  articles:\n    - {days: [tue], time: 10:30}\n", encoding="utf-8")
    with pytest.raises(ProfileError, match='quoted "HH:MM"'):
        load_profile(path)


@pytest.mark.parametrize("phase", range(0, 120, 10))
def test_slot_at_hh59_fires_with_prod_interval(monkeypatch, tmp_path, clock, saves, phase):
    # prod ticks every 120 s: a window that ends with its hour would give HH:59 one minute and lose half the phases
    fired = []
    schedule = {"articles": [], "telegram": [{"day": "mon", "time": "10:59", "slot": "late"}]}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock)

    clock.run(engine, MONDAY + timedelta(hours=10, minutes=50, seconds=phase), 30, interval=120)

    assert [(kind, key) for _, kind, key in _with_keys(fired, saves)] == [("tg", "2026-10-12_mon_late")]


def test_tg_slot_due_during_article_publication_is_not_lost(monkeypatch, tmp_path, clock, saves):
    # the worker is blind while the browser publishes: the TG slot that opened meanwhile goes right after it
    fired = []
    schedule = {"articles": [{"days": ["mon"], "time": "10:30"}],
                "telegram": [{"day": "mon", "time": "10:50", "slot": "x"}]}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock, publish_minutes=31)

    clock.run(engine, MONDAY + timedelta(hours=10, minutes=30), 120, interval=120)

    assert _with_keys(fired, saves) == [("Mon 10:30", "article", "2026-10-12"), ("Mon 11:01", "tg", "2026-10-12_mon_x")]


def test_article_published_past_midnight_marks_slot_day(monkeypatch, tmp_path, clock, saves):
    # 23:30 slot, publishing takes 40 min: the mark is the slot's date, so tomorrow's slot still fires
    fired = []
    schedule = {"articles": [{"days": ["mon", "tue", "wed"], "time": "23:30"}], "telegram": []}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock, publish_minutes=40)

    clock.run(engine, MONDAY + timedelta(hours=23), 3 * 24 * 60, interval=120)

    assert _with_keys(fired, saves) == [
        ("Mon 23:30", "article", "2026-10-12"),
        ("Tue 23:30", "article", "2026-10-13"),
        ("Wed 23:30", "article", "2026-10-14"),
    ]


def test_window_past_midnight_belongs_to_slot_day(monkeypatch, tmp_path, clock, saves):
    # Sunday 23:30 slot, the process comes up on Monday 00:10 inside its window: Sunday's key, Sunday's post
    fired = []
    schedule = {"articles": [], "telegram": [{"day": "sun", "time": "23:30", "slot": "night"}]}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock)

    clock.run(engine, MONDAY + timedelta(minutes=10), 60, interval=120)

    assert _with_keys(fired, saves) == [("Mon 00:10", "tg", "2026-10-11_sun_night")]


def test_status_shows_schedule_local_time(monkeypatch, tmp_path, clock):
    clock.utc = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)
    schedule = {"utc_offset_hours": 5, "articles": [{"days": ["mon"], "time": "10:30"}], "telegram": []}
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(tmp_path, schedule))
    engine.db = FakeDb([])

    status = engine.get_status()

    assert status["current_msk_time"] == "2026-10-12 10:00:00 MSK"
    assert status["local_time"] == "2026-10-12 12:00:00 UTC+5"


def test_tg_slot_due_before_long_publication_still_goes(monkeypatch, tmp_path, clock, saves):
    # article and TG both at 13:00, publishing outlasts the TG window: the post goes late, as before the schedule
    fired = []
    schedule = {"articles": [{"days": ["mon"], "time": "13:00"}],
                "telegram": [{"day": "mon", "time": "13:00", "slot": "x"}]}
    engine = _engine(monkeypatch, _profile(tmp_path, schedule), fired, clock, publish_minutes=70)

    clock.run(engine, MONDAY + timedelta(hours=13), 180, interval=120)

    assert _with_keys(fired, saves) == [("Mon 13:00", "article", "2026-10-12"), ("Mon 14:10", "tg", "2026-10-12_mon_x")]


class StatusDb(FakeDb):
    """Keeps article statuses as the real Database does."""

    def update_article_status(self, article_id, status, **kwargs):
        for article in self.articles:
            if article.id == article_id:
                article.status = status


@pytest.mark.parametrize("failure", ["result", "exception"])
def test_failing_publisher_one_attempt_per_slot(monkeypatch, tmp_path, clock, saves, failure):
    # Owner's rule: one attempt per article slot, the slot is done whatever the outcome; the article stays
    # ready for the next slot and goes to failed only after 3 failed slots in a row
    calls, alerts, statuses = [], [], []

    class FailingPublisher:
        def publish(self, article):
            calls.append((clock.utc + timedelta(hours=3)).strftime("%a %H:%M") + " " + article.id)
            if failure == "exception":
                raise RuntimeError("boom")
            return PublishResult(success=False, platform=article.platform, article_id=article.id, error="boom")

    async def record_alert(**kwargs):
        alerts.append(kwargs)
        statuses.append({a.id: a.status for a in engine.db.articles})

    schedule = {"articles": WEEKDAY_SCHEDULE["articles"], "telegram": []}
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(tmp_path, schedule))
    engine.db = StatusDb([Article(id=f"a{i}", campaign_id="camp1", platform="vc", title=f"T{i}", lead="",
                                  content_markdown="b", content_html="<p>b</p>", tags="", status="ready")
                          for i in (1, 2)])
    engine.is_enabled = lambda: True
    monkeypatch.setattr(autopilot, "get_publisher", lambda platform: FailingPublisher())
    monkeypatch.setattr(autopilot, "send_telegram_alert", record_alert)

    clock.run(engine, MONDAY, WEEK_MINUTES, interval=120)

    assert calls == ["Mon 10:30 a1", "Tue 10:30 a1", "Wed 10:30 a1", "Thu 10:30 a2", "Fri 10:30 a2"]
    assert [s["last_article_day"] for s in saves if s["what"] == "last_article_day"] == [
        f"2026-10-{12 + i}" for i in range(5)]
    assert len(alerts) == 5
    assert all("boom" in a["error_message"] for a in alerts)
    assert [a["error_message"].split("(")[1].split(")")[0] for a in alerts] == [
        "неудача 1/3", "неудача 2/3", "неудача 3/3", "неудача 1/3", "неудача 2/3"]
    assert statuses == [
        {"a1": "ready", "a2": "ready"}, {"a1": "ready", "a2": "ready"},
        {"a1": "failed", "a2": "ready"}, {"a1": "failed", "a2": "ready"}, {"a1": "failed", "a2": "ready"}]
    assert autopilot.AutopilotState.load()["article_failures"] == {"a2": 2}


def test_success_after_failed_slot_clears_the_count(monkeypatch, tmp_path, clock, saves):
    outcomes = iter([False, True])

    class FlakyPublisher:
        def publish(self, article):
            ok = next(outcomes)
            return PublishResult(success=ok, platform=article.platform, article_id=article.id,
                                 status="submitted" if ok else "failed", error=None if ok else "boom")

    alerts = []

    async def record_alert(**kwargs):
        alerts.append(kwargs)

    schedule = {"articles": [{"days": ["mon", "tue"], "time": "10:30"}], "telegram": []}
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(tmp_path, schedule))
    engine.db = StatusDb([Article(id="a1", campaign_id="camp1", platform="vc", title="T", lead="",
                                  content_markdown="b", content_html="<p>b</p>", tags="", status="ready")])
    engine.is_enabled = lambda: True
    monkeypatch.setattr(autopilot, "get_publisher", lambda platform: FlakyPublisher())
    monkeypatch.setattr(autopilot, "send_telegram_alert", record_alert)

    clock.run(engine, MONDAY, 2 * 24 * 60, interval=120)

    assert engine.db.articles[0].status == "submitted"
    assert len(alerts) == 1
    assert autopilot.AutopilotState.load()["article_failures"] == {}


def test_failed_tg_post_is_one_attempt_and_marks_slot(monkeypatch, tmp_path, clock, saves):
    # A failed send is not retried every tick: the slot is done, the post idx stays for the next slot
    fired = []
    engine = _engine(monkeypatch, _profile(tmp_path, {"articles": [], "telegram": WEEKDAY_SCHEDULE["telegram"]}),
                     fired, clock)

    async def failing_tg(specific_idx=None):
        fired.append(((clock.utc + timedelta(hours=3)).strftime("%a %H:%M"), "tg"))
        return {"error": "boom"}

    engine.step_telegram_post = failing_tg

    clock.run(engine, MONDAY, 2 * 24 * 60, interval=120)

    assert _with_keys(fired, saves) == [("Mon 11:00", "tg", "2026-10-12_mon_insight"),
                                        ("Tue 13:00", "tg", "2026-10-13_tue_teaser")]


def test_dead_platform_is_retried_once_a_round(monkeypatch, tmp_path, clock, saves):
    # vc's session is dead: the slot after a vc failure goes to spark, vc is retried the next slot
    calls = []

    class VcDown:
        def publish(self, article):
            calls.append((clock.utc + timedelta(hours=3)).strftime("%a") + " " + article.id)
            ok = article.platform != "vc"
            return PublishResult(success=ok, platform=article.platform, article_id=article.id,
                                 status="published" if ok else None, error=None if ok else "session expired")

    schedule = {"articles": WEEKDAY_SCHEDULE["articles"], "telegram": []}
    engine = autopilot.AutopilotEngine("camp1", profile=_profile(tmp_path, schedule))
    engine.db = StatusDb([Article(id=i, campaign_id="camp1", platform=p, title=i, lead="", content_markdown="b",
                                  content_html="<p>b</p>", tags="", status="ready")
                          for i, p in (("v1", "vc"), ("s1", "spark"), ("s2", "spark"))])
    engine.is_enabled = lambda: True
    monkeypatch.setattr(autopilot, "get_publisher", lambda platform: VcDown())
    alerts = []

    async def record_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(autopilot, "send_telegram_alert", record_alert)

    clock.run(engine, MONDAY, WEEK_MINUTES, interval=120)

    assert calls == ["Mon v1", "Tue s1", "Wed v1", "Thu s2", "Fri v1"]
    assert {a.id: a.status for a in engine.db.articles}["v1"] == "failed"
    assert len(alerts) == 3
