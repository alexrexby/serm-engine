"""
Server Autopilot & Daemon Supervisor for SERM Distribution, OPP Telegram Bot & Admin Panel.
Runs 24/7 on the server, ensuring:
1. Web Admin Panel is accessible for managing secrets (bot token, channel, keys).
2. OPP Telegram Bot is active, serving lead magnets, and hot-reloadable on secrets change.
3. Smart business calendar autopilot handles external article & Telegram publishing.
4. Scheduled campaign tasks and indexing checks run reliably.
5. Graceful shutdown and process monitoring.
"""
import sys
import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiogram import Bot
from core.db import Database
from core.opp_bot import create_opp_dispatcher, poll_opp_bot, DEFAULT_CHANNEL, DEFAULT_CONTACT
from core.admin import create_admin_app
from core.autopilot import AutopilotEngine, periodic_autopilot_worker
from core.lead_status import periodic_lead_reminders
from config.settings import DATA_DIR, EXPORTS_DIR, load_settings
from core.profile import ProfileError, get_profile, missing_own_templates, profile_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SERMScheduler")

# "No profile templates" makes the container crash-loop (restart: unless-stopped): one alert per hour, not per restart
NO_TEMPLATES_ALERT_MARK = DATA_DIR / "alert_no_templates.mark"
NO_TEMPLATES_ALERT_EVERY_S = 3600


class AdminAccessLogger(AbstractAccessLogger):
    """Access log without query string and Referer: the admin panel accepts ?token=, and the log is docker logs."""

    def log(self, request, response, time):
        self.logger.info(f'{request.remote} "{request.method} {request.path}" {response.status} '
                         f'{response.body_length} {time:.3f}s')


def admin_runner(app: web.Application) -> web.AppRunner:
    return web.AppRunner(app, access_log_class=AdminAccessLogger)


def _alert_due(mark: Path, every_s: int) -> bool:
    """True at most once per every_s across process restarts (mtime of a marker file in data/)."""
    try:
        if time.time() - mark.stat().st_mtime < every_s:
            return False
    except FileNotFoundError:
        pass
    try:
        mark.touch()
    except OSError as e:  # an unwritable data/ must not silence the alert
        logger.error(f"Cannot write alert marker {mark}: {e}")
    return True


async def _alert(title: str, error: BaseException, context: str):
    try:
        from core.alerts import send_telegram_alert
        await send_telegram_alert(title=title, error_message=str(error), context=context, level="error")
    except Exception as e:  # the alert must not mask the original failure
        logger.error(f"Cannot alert admin: {e}")


class BotSupervisor:
    """Manages Telegram bot lifecycle and supports hot-reloading without container restarts."""

    def __init__(self, channel: str, contact: str, campaign_id: str):
        self.channel = channel
        self.contact = contact
        self.campaign_id = campaign_id
        self.task: Optional[asyncio.Task] = None
        self.is_running = False
        self.bot_username: Optional[str] = None
        # Serializes start/stop/restart: two quick saves in the admin panel must not leave two pollers
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            await self._start()

    async def stop(self):
        async with self._lock:
            await self._stop()

    async def restart(self):
        async with self._lock:
            logger.info("Restarting OPP Bot with new configuration...")
            await self._stop()
            await self._start()

    async def _start(self):
        # Fresh read on every (re)start: the admin panel rewrites secrets at runtime
        settings = load_settings()
        token = settings.bot_token.get_secret_value()
        # None = key absent; an empty value in .env stays "" as before
        if settings.channel is not None:
            self.channel = settings.channel
        if settings.contact is not None:
            self.contact = settings.contact

        if not token:
            logger.warning("⚠️ OPP_BOT_TOKEN not provided. Bot runner is disabled.")
            self.is_running = False
            self.bot_username = None
            return

        pdf_path = EXPORTS_DIR / f"campaign_{self.campaign_id}" / "lead_magnet_15_business_holes.pdf"
        if not pdf_path.exists():
            for p in EXPORTS_DIR.glob("**/lead_magnet_*.pdf"):
                pdf_path = p
                break

        # Check getMe to obtain bot username
        try:
            bot = Bot(token=token)
            me = await bot.get_me()
            self.bot_username = me.username
            await bot.session.close()
            logger.info(f"Verified Telegram bot: @{self.bot_username} ({me.first_name})")
        except Exception as e:
            logger.error(f"Failed to verify Telegram bot with token: {e}")

        # Built here, not inside the task: a broken profile/template must not leave a dead task reported as online
        try:
            dp = create_opp_dispatcher(self.channel, EXPORTS_DIR, self.contact)
        except Exception as e:
            logger.exception("OPP Bot dispatcher cannot be built; bot is NOT running")
            self.is_running = False
            await _alert("Telegram-бот не запущен: ошибка сборки диспетчера", e, "BotSupervisor.start")
            return

        logger.info(f"Starting OPP Telegram Bot task (channel: {self.channel})...")
        self.is_running = True
        self.task = asyncio.create_task(poll_opp_bot(token, dp, self.channel, EXPORTS_DIR))
        self.task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task):
        if task is not self.task or task.cancelled():
            return  # stop()/restart() cancelled it on purpose
        self.is_running = False
        error = task.exception()
        if error is None:
            logger.error("OPP Telegram Bot polling ended unexpectedly; bot is NOT running")
            return
        logger.error("OPP Telegram Bot crashed; bot is NOT running", exc_info=error)
        from core.alerts import notify_admin_sync
        notify_admin_sync("Telegram-бот упал", str(error), context="BotSupervisor task", level="error")

    async def _stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.is_running = False
        logger.info("OPP Telegram Bot stopped.")


async def periodic_healthcheck(interval_seconds: int = 1800):
    """Periodic healthcheck logging and DB integrity verification."""
    db = Database()
    while True:
        try:
            campaigns = db.list_campaigns()
            logger.info(f"Healthcheck OK: Database reachable, {len(campaigns)} active campaigns registered.")
        except Exception as e:
            logger.error(f"Healthcheck Error: Database error: {e}")
            try:
                from core.alerts import send_telegram_alert
                await send_telegram_alert(
                    title="Сбой проверки состояния базы данных SERM",
                    error_message=str(e),
                    context="periodic_healthcheck",
                    level="warning"
                )
            except Exception:
                pass
        await asyncio.sleep(interval_seconds)


async def main():
    # Fail loudly without a profile: the bot must not speak as a fictional example brand
    try:
        profile = get_profile()
    except ProfileError as e:
        logger.critical(f"Profile: {e}")
        raise
    logger.info(f"Profile: {profile_summary(profile)}")
    # A real installation without its own templates would silently speak with the neutral scaffold
    problem = missing_own_templates(profile)
    if problem:
        logger.critical(f"Profile: {problem}")
        if _alert_due(NO_TEMPLATES_ALERT_MARK, NO_TEMPLATES_ALERT_EVERY_S):
            await _alert("Сервер SERM не запущен: нет шаблонов профиля", ProfileError(problem), "scheduler.main")
        raise ProfileError(problem)
    NO_TEMPLATES_ALERT_MARK.unlink(missing_ok=True)  # fixed: the next breakage alerts at once

    settings = load_settings()
    for warning in settings.warnings():
        if warning not in settings.problems():  # problems are already logged by load_settings
            logger.warning(f"Config: {warning}")
    # Defaults apply only when the key is absent (empty value in .env stays "", as before)
    channel = settings.channel if settings.channel is not None else DEFAULT_CHANNEL
    contact = settings.contact if settings.contact is not None else DEFAULT_CONTACT
    campaign_id = settings.active_campaign if settings.active_campaign is not None else profile.campaign_id
    admin_port = settings.admin_port
    admin_host = settings.admin_host

    logger.info("=" * 60)
    logger.info("🚀 STARTING SERM SERVER DAEMON & ADMIN SUPERVISOR")
    logger.info(f"   Target Channel:   {channel}")
    logger.info(f"   Active Campaign:  {campaign_id}")
    logger.info(f"   Admin Web Port:   {admin_port}")
    logger.info("=" * 60)

    # 1. Start Bot Supervisor
    supervisor = BotSupervisor(channel, contact, campaign_id)
    await supervisor.start()

    # 2. Start Admin Web Panel
    admin_app = create_admin_app(supervisor)
    runner = admin_runner(admin_app)
    await runner.setup()
    site = web.TCPSite(runner, admin_host, admin_port)
    await site.start()
    logger.info(f"⚡ Admin Panel live and listening on http://{admin_host}:{admin_port}")

    # 3. Background healthchecks
    healthcheck_task = asyncio.create_task(periodic_healthcheck(interval_seconds=1800))
    # Lead status reminders to the owner (the standalone run_opp_bot sends none, on purpose)
    lead_reminder_task = asyncio.create_task(periodic_lead_reminders(interval_s=1800))

    # 4. Background Autopilot Worker (smart business calendar)
    autopilot_engine = AutopilotEngine(campaign_id)
    autopilot_task = asyncio.create_task(periodic_autopilot_worker(autopilot_engine, check_interval_seconds=120))

    # Graceful stop handling
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Termination signal received. Shutting down gracefully...")
        stop_event.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _handle_signal)
        except NotImplementedError:
            pass

    await stop_event.wait()
    healthcheck_task.cancel()
    lead_reminder_task.cancel()
    autopilot_task.cancel()
    await supervisor.stop()
    await runner.cleanup()
    logger.info("SERM Server Daemon stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Daemon exited.")
