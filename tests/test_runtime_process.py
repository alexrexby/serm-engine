"""
Process runtime (scheduler.py, core/alerts.py, core/content.py): failures of the long-running daemon
are loud. A bot that cannot start or dies is reported as offline with an alert; a non-example profile
without its own templates stops the start; the admin token never reaches the access log; background
alerts keep a reference and their errors are logged.
Uses only example profiles and tmp dirs; real .env, data/ and the network are not touched.
"""
import sys
import shutil
import asyncio
import logging
from pathlib import Path

import pytest
from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scheduler  # noqa: E402
import core.alerts as alerts_module  # noqa: E402
import core.opp_bot as opp_bot  # noqa: E402
from core.profile import ProfileError, load_profile  # noqa: E402

PROFILES = PROJECT_ROOT / "profiles"
TOKEN = "123456:" + "x" * 35


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def record(title, error_message, context=None, level="error"):
        sent.append({"title": title, "error_message": error_message, "context": context})
        return False

    monkeypatch.setattr(alerts_module, "send_telegram_alert", record)
    return sent


class _Settings:
    def __init__(self, token=TOKEN):
        self.bot_token = type("S", (), {"get_secret_value": lambda self: token})()
        self.channel = "@example_channel"
        self.contact = "@example_contact"


class _FakeBot:
    def __init__(self, token):
        self.session = self

    async def get_me(self):
        return type("Me", (), {"username": "example_bot", "first_name": "Bot"})()

    async def close(self):
        pass


@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setattr(scheduler, "load_settings", lambda: _Settings())
    monkeypatch.setattr(scheduler, "Bot", _FakeBot)
    return scheduler.BotSupervisor("@example_channel", "@example_contact", "camp1")


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- RT-H1: the bot does not die silently --------------------------------------------------------

def test_dispatcher_build_failure_is_offline_and_alerted(monkeypatch, supervisor, alerts, caplog):
    def boom(*args, **kwargs):
        raise ProfileError("bot/buttons.yaml.j2: broken")

    # Both lookups: the dispatcher is built either in start() or inside the polling task
    monkeypatch.setattr(opp_bot, "create_opp_dispatcher", boom)
    monkeypatch.setattr(scheduler, "create_opp_dispatcher", boom, raising=False)

    async def main():
        await supervisor.start()
        await _settle()

    with caplog.at_level(logging.ERROR, logger="SERMScheduler"):
        asyncio.run(main())

    assert supervisor.is_running is False
    assert len(alerts) == 1 and "buttons.yaml.j2" in alerts[0]["error_message"]
    assert any(r.exc_info for r in caplog.records if r.name == "SERMScheduler")


def test_polling_crash_resets_running_and_alerts(monkeypatch, supervisor, alerts, caplog):
    monkeypatch.setattr(scheduler, "create_opp_dispatcher", lambda *a, **kw: object(), raising=False)

    async def crashing_bot(*args, **kwargs):
        raise RuntimeError("polling died")

    monkeypatch.setattr(scheduler, "run_opp_bot", crashing_bot, raising=False)
    monkeypatch.setattr(scheduler, "poll_opp_bot", crashing_bot, raising=False)

    async def main():
        await supervisor.start()
        assert supervisor.is_running is True
        await _settle()

    with caplog.at_level(logging.ERROR, logger="SERMScheduler"):
        asyncio.run(main())

    assert supervisor.is_running is False
    assert len(alerts) == 1 and "polling died" in alerts[0]["error_message"]
    assert any(r.exc_info for r in caplog.records if r.name == "SERMScheduler")


def test_stop_is_not_reported_as_crash(monkeypatch, supervisor, alerts):
    monkeypatch.setattr(scheduler, "create_opp_dispatcher", lambda *a, **kw: object(), raising=False)

    async def polling(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "poll_opp_bot", polling, raising=False)

    async def main():
        await supervisor.start()
        await _settle()
        await supervisor.stop()
        await _settle()

    asyncio.run(main())
    assert supervisor.is_running is False and alerts == []


def test_parallel_restarts_leave_one_poller(monkeypatch, supervisor, alerts):
    # Two quick saves in the admin panel: start() yields at getMe, the restarts must not interleave
    class SlowBot(_FakeBot):
        async def get_me(self):
            await asyncio.sleep(0.01)
            return await super().get_me()

    monkeypatch.setattr(scheduler, "Bot", SlowBot)
    monkeypatch.setattr(scheduler, "create_opp_dispatcher", lambda *a, **kw: object(), raising=False)
    live = set()

    async def polling(*args, **kwargs):
        me = object()
        live.add(me)
        try:
            await asyncio.Event().wait()
        finally:
            live.discard(me)

    monkeypatch.setattr(scheduler, "poll_opp_bot", polling, raising=False)

    async def main():
        await supervisor.start()
        await _settle()
        await asyncio.gather(supervisor.restart(), supervisor.restart())
        await _settle()
        after_restarts = len(live)
        await supervisor.stop()
        await _settle()
        return after_restarts

    assert asyncio.run(main()) == 1
    assert live == set() and supervisor.is_running is False and alerts == []


def test_sigterm_after_bot_restart_stops_daemon(monkeypatch, tmp_path, alerts, caplog):
    # The daemon owns SIGTERM: real aiogram polling (handle_signals) must not take it over on a bot restart
    import os
    import signal
    import socket
    from aiogram import Dispatcher
    from config.settings import Settings

    monkeypatch.setattr(Settings, "env_path", tmp_path / "absent.env")  # never the real .env
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("OPP_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_HOST", "127.0.0.1")
    monkeypatch.setenv("ADMIN_PORT", str(port))
    monkeypatch.setenv("SERM_PROFILE", "example-person")  # opp_bot resolves the profile itself
    monkeypatch.setattr(scheduler, "get_profile", lambda: load_profile(PROFILES / "example-person.yaml"))
    monkeypatch.setattr(scheduler, "Bot", _FakeBot)
    monkeypatch.setattr(scheduler, "AutopilotEngine", lambda campaign_id: None)

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "periodic_autopilot_worker", idle)
    monkeypatch.setattr(scheduler, "periodic_healthcheck", idle)
    polls = []

    async def polling(self, *args, **kwargs):  # replaces getUpdates: no network
        polls.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            polls.append("stopped")

    monkeypatch.setattr(Dispatcher, "_polling", polling)
    supervisors = []

    class Supervisor(scheduler.BotSupervisor):
        def __init__(self, *args):
            super().__init__(*args)
            supervisors.append(self)

    monkeypatch.setattr(scheduler, "BotSupervisor", Supervisor)

    async def wait_for(cond):
        for _ in range(200):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")

    async def main():
        daemon = asyncio.create_task(scheduler.main())
        await wait_for(lambda: polls.count("started") == 1)
        await supervisors[0].restart()
        await wait_for(lambda: polls.count("started") == 2)
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(daemon, timeout=5)

    with caplog.at_level(logging.INFO, logger="SERMScheduler"):
        asyncio.run(main())

    assert polls.count("stopped") == 2
    assert any("Daemon stopped" in r.getMessage() for r in caplog.records)


# --- RT-H2: a real installation without its own templates does not start -------------------------

class _Reached(Exception):
    """main() got past the profile checks to the bot start."""


@pytest.fixture
def reach(monkeypatch, tmp_path):
    from config.settings import Settings
    monkeypatch.setattr(Settings, "env_path", tmp_path / "absent.env")  # never the real .env

    def stop_here(*args, **kwargs):
        raise _Reached()

    monkeypatch.setattr(scheduler, "BotSupervisor", stop_here)
    monkeypatch.setattr(scheduler, "NO_TEMPLATES_ALERT_MARK", tmp_path / "alert_no_templates.mark")


def _own_profile(tmp_path, with_templates):
    shutil.copy(PROFILES / "example-company.yaml", tmp_path / "acme.yaml")
    if with_templates:
        (tmp_path / "acme" / "templates").mkdir(parents=True)
        (tmp_path / "acme" / "templates" / "own_marker.j2").write_text("{# own #}\n", encoding="utf-8")
    return load_profile(tmp_path / "acme.yaml")


def test_own_profile_without_templates_fails_start(monkeypatch, tmp_path, reach, alerts, caplog):
    profile = _own_profile(tmp_path, with_templates=False)
    assert not profile.is_example
    monkeypatch.setattr(scheduler, "get_profile", lambda: profile)

    with caplog.at_level(logging.CRITICAL, logger="SERMScheduler"):
        with pytest.raises(ProfileError, match="templates"):
            asyncio.run(scheduler.main())

    assert any(r.levelno == logging.CRITICAL and "templates" in r.getMessage() for r in caplog.records)
    assert len(alerts) == 1 and "acme" in alerts[0]["error_message"]


def test_no_templates_crash_loop_alerts_once_per_hour(monkeypatch, tmp_path, reach, alerts):
    # N4: restart: unless-stopped re-runs main(); the alert must not repeat on every restart
    import os
    import time
    profile = _own_profile(tmp_path, with_templates=False)
    monkeypatch.setattr(scheduler, "get_profile", lambda: profile)
    mark = scheduler.NO_TEMPLATES_ALERT_MARK

    for _ in range(3):
        with pytest.raises(ProfileError):
            asyncio.run(scheduler.main())
    assert len(alerts) == 1 and mark.exists()

    hour_ago = time.time() - scheduler.NO_TEMPLATES_ALERT_EVERY_S - 1
    os.utime(mark, (hour_ago, hour_ago))
    with pytest.raises(ProfileError):
        asyncio.run(scheduler.main())
    assert len(alerts) == 2

    # templates restored: the marker goes, the next breakage alerts at once
    profile = _own_profile(tmp_path, with_templates=True)
    with pytest.raises(_Reached):
        asyncio.run(scheduler.main())
    assert not mark.exists()


def test_own_profile_with_templates_and_example_profile_start(monkeypatch, tmp_path, reach, alerts, caplog):
    for profile in (_own_profile(tmp_path, with_templates=True), load_profile(PROFILES / "example-person.yaml")):
        monkeypatch.setattr(scheduler, "get_profile", lambda: profile)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="SERMScheduler"):
            with pytest.raises(_Reached):
                asyncio.run(scheduler.main())
        summary = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Profile:"))
        assert "templates=" in summary
    assert alerts == []


# --- SEC-medium: ?token= is not written to the access log -----------------------------------------

def test_access_log_has_no_query(caplog):
    import aiohttp

    async def ok(request):
        return web.Response(text="ok")

    async def main():
        app = web.Application()
        app.router.add_get("/admin", ok)
        runner = scheduler.admin_runner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/admin?token=SECRET-VALUE&x=1",
                                       headers={"Referer": f"http://127.0.0.1:{port}/admin?token=SECRET-VALUE"}) as r:
                    assert r.status == 200
        finally:
            await runner.cleanup()

    with caplog.at_level(logging.INFO, logger="aiohttp.access"):
        asyncio.run(main())

    access = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert access and "/admin" in access[0] and " 200 " in access[0]
    assert all("SECRET-VALUE" not in line and "token" not in line for line in access)


# --- CFG-low: background alert keeps a reference, its error is logged -----------------------------

def test_background_alert_error_is_logged(monkeypatch, caplog):
    async def failing(*args, **kwargs):
        raise RuntimeError("alert transport down")

    monkeypatch.setattr(alerts_module, "send_telegram_alert", failing)

    async def main():
        alerts_module.notify_admin_sync("title", "details")
        await _settle()

    with caplog.at_level(logging.ERROR, logger="AlertsService"):
        asyncio.run(main())

    assert any("alert transport down" in r.getMessage() or (r.exc_info and "alert transport down" in str(r.exc_info[1]))
               for r in caplog.records if r.name == "AlertsService")


# --- RT-L1: template lead magnet is generated over the passed profile -------------------------------

def test_template_lead_magnet_uses_passed_profile(monkeypatch, tmp_path):
    from core import content
    from config.settings import Settings
    monkeypatch.setattr(Settings, "env_path", tmp_path / "absent.env")  # never the real .env
    import core.lead_magnet as lead_magnet_module
    seen = {}

    class FakeGenerator:
        def __init__(self, output_dir, profile=None):
            seen["profile"] = profile

        async def generate_for_topic(self, slug, **kwargs):
            return {"slug": slug, **kwargs}

    monkeypatch.setattr(lead_magnet_module, "DynamicLeadMagnetGenerator", FakeGenerator)
    profile = load_profile(PROFILES / "example-company.yaml")
    asyncio.run(content.lead_magnet("audit", profile, output_dir=tmp_path, mode="template"))
    assert seen["profile"] is profile

    asyncio.run(content.lead_magnet("audit", profile.model_dump(mode="json"), output_dir=tmp_path, mode="template"))
    assert seen["profile"].display_name == profile.display_name
