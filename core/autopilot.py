from core.alerts import send_telegram_alert
"""
Autopilot Engine for SERM Distribution & Telegram Channel Automation.
Operates on the server 24/7 inside serm-daemon:
1. Monitors scheduled distribution slots (VC, Dzen, Spark, Executive) by the profile's calendar (profile.schedule).
2. Runs headless browser publishers and registers draft/live URLs.
3. Automatically triggers FastIndexer pinging (IndexNow, Google/Yandex).
4. Dispatches scheduled expert Telegram posts and announcement teasers to the profile's channel.
5. Persists execution state in data/autopilot_state.json.
Platforms come from publishers/registry (only those enabled in the profile); only `ready` articles
are published, `draft` ones wait for approval in the admin panel.
"""
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

from core.db import Database
from core.models import Article
from core.indexer import FastIndexer, indexable_url
from core.telegram_posts import load_posts
from config.settings import DATA_DIR, EXPORTS_DIR, get_bot_token, load_settings
from core.profile import DAYS, SLOT_WINDOW_MINUTES
from publishers.registry import PLATFORMS, enabled, get_publisher

logger = logging.getLogger("AutopilotEngine")

STATE_FILE = DATA_DIR / "autopilot_state.json"
MAX_FAILED_SLOTS = 3  # failed article slots in a row before the article goes to `failed`
MSK_OFFSET = timedelta(hours=3)


def get_msk_now() -> datetime:
    return datetime.now(timezone.utc) + MSK_OFFSET


def due_slots(schedule, now: datetime):
    """(article slot day "YYYY-MM-DD" or None, TG slot key or None) at local time `now`.
    A slot HH:MM is due for SLOT_WINDOW_MINUTES from HH:MM; a window past midnight belongs to the day it started
    (its date and weekday). The profile schema keeps the windows of each kind apart, so at most one of each matches."""
    def started(slot_time: str) -> Optional[datetime]:
        # the latest HH:MM at or before now — yesterday's while today's is still ahead
        hour, minute = (int(part) for part in slot_time.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        return start if now < start + timedelta(minutes=SLOT_WINDOW_MINUTES) else None

    article_day = next((start.strftime("%Y-%m-%d") for slot in schedule.articles
                        for start in [started(slot.time)] if start and DAYS[start.weekday()] in slot.days), None)
    tg_slot_key = next((f"{start.strftime('%Y-%m-%d')}_{slot.day}_{slot.slot}" for slot in schedule.telegram
                        for start in [started(slot.time)] if start and DAYS[start.weekday()] == slot.day), None)
    return article_day, tg_slot_key


class AutopilotState:
    @staticmethod
    def load() -> Dict[str, Any]:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Failed to read autopilot state: {e}")
        return {
            "enabled": False,
            "schedule_mode": "smart_calendar",
            "last_article_day": None,
            "last_tg_slot": None,
            "last_article_run": None,
            "last_tg_run": None,
            "next_tg_post_idx": 0,
            "published_history": []
        }

    @staticmethod
    def save(state: Dict[str, Any]):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# UGC platforms with active sessions first (dzen, vc, spark, executive)
PLATFORM_PRIORITY = {"dzen": 1, "vc": 2, "spark": 3, "executive": 4, "rb": 5, "tbank": 6, "biz360": 7}


def pick_next_article(ready: List[Article], history: List[Dict[str, Any]],
                      attempts: Optional[Dict[str, str]] = None) -> Article:
    """Next article: the platform tried longest ago (never = first, ties by priority) — a test period covers
    every platform and a platform with a dead session is retried once a round instead of every slot;
    inside a platform a title not yet published there goes first (campaign variants share a title).
    Editorial-pitch platforms only prepare a pitch file for the owner, nothing reaches readers: they go
    after every platform that publishes, as before the rotation."""
    last = {h.get("platform"): h.get("timestamp") or "" for h in history}
    last.update(attempts or {})
    published = {(h.get("platform"), h.get("title")) for h in history}

    def key(a: Article):
        spec = PLATFORMS.get(a.platform)
        return (bool(spec and spec.type == "editorial_pitch"), last.get(a.platform, ""), (a.platform, a.title) in published,
                PLATFORM_PRIORITY.get(a.platform, 99))

    return min(ready, key=key)  # min keeps queue order among equals


class AutopilotEngine:
    def __init__(self, campaign_id: Optional[str] = None, profile: Optional[Any] = None):
        if profile is None:
            from core.profile import get_profile
            profile = get_profile()
        self.profile = profile
        self.campaign_id = campaign_id or profile.campaign_id
        self.db = Database()

    def local_now(self) -> datetime:
        # Wall clock of the schedule: MSK shifted to UTC+utc_offset_hours (3 = MSK, the default)
        return get_msk_now() + timedelta(hours=self.profile.schedule.utc_offset_hours) - MSK_OFFSET

    def channel(self) -> str:
        # Read fresh: the admin panel rewrites OPP_CHANNEL at runtime; key absent -> profile channel
        channel = load_settings().channel
        return channel if channel is not None else self.profile.telegram.channel

    def ready_articles(self) -> List[Article]:
        platforms = set(enabled(self.profile))
        return [a for a in self.db.list_articles_by_campaign(self.campaign_id)
                if a.status == "ready" and a.platform in platforms]

    def is_enabled(self) -> bool:
        env_val = load_settings().autopilot_enabled.lower()
        if env_val in ("1", "true", "yes"):
            return True
        state = AutopilotState.load()
        return bool(state.get("enabled", False))

    def set_enabled(self, enabled: bool):
        state = AutopilotState.load()
        state["enabled"] = enabled
        AutopilotState.save(state)
        logger.info(f"Autopilot state changed: enabled={enabled}")

    def get_status(self) -> Dict[str, Any]:
        state = AutopilotState.load()
        ready_articles = self.ready_articles()
        published_articles = [a for a in self.db.list_articles_by_campaign(self.campaign_id) if a.status in ("published", "draft_saved")]

        token = get_bot_token()
        channel = self.channel()
        msk_now = get_msk_now()

        return {
            "enabled": self.is_enabled(),
            "campaign_id": self.campaign_id,
            "schedule_mode": state.get("schedule_mode", "smart_calendar"),
            "schedule": self.profile.schedule.summary(),
            "current_msk_time": msk_now.strftime("%Y-%m-%d %H:%M:%S MSK"),
            # wall clock the schedule slots are in (UTC+utc_offset_hours)
            "local_time": f"{self.local_now():%Y-%m-%d %H:%M:%S} UTC{self.profile.schedule.utc_offset_hours:+d}",
            "ready_count": len(ready_articles),
            "published_count": len(published_articles),
            "last_article_run": state.get("last_article_run"),
            "last_tg_run": state.get("last_tg_run"),
            "next_tg_post_idx": state.get("next_tg_post_idx", 0),
            "bot_configured": bool(token),
            "target_channel": channel,
            "history": state.get("published_history", [])[-10:]
        }

    async def step_article_publication(self, slot_day: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Publishes the next available article in the campaign queue.
        slot_day: date of the schedule slot (the worker passes it) — marked done even if publishing ends after midnight."""
        ready_articles = self.ready_articles()

        if not ready_articles:
            logger.info("Autopilot: No ready articles left in campaign queue.")
            return None

        state = AutopilotState.load()
        target_article = pick_next_article(ready_articles, state.get("published_history", []),
                                           state.get("platform_attempts", {}))

        logger.info(f"Autopilot: Selecting next article for [{target_article.platform}]: {target_article.title[:50]}...")

        # One attempt per slot (owner's rule): whatever the outcome the slot is done, a failed article stays
        # `ready` for the next slot and goes to `failed` after MAX_FAILED_SLOTS failed slots in a row
        try:
            publisher = get_publisher(target_article.platform)
            res = await asyncio.to_thread(publisher.publish, target_article)
            error = None if res.success else (res.error or "Неизвестная ошибка публикации")
        except Exception as e:
            res, error = None, str(e)

        state = AutopilotState.load()
        msk_now = get_msk_now()
        # rotation key: any outcome counts as this platform's turn
        state.setdefault("platform_attempts", {})[target_article.platform] = msk_now.isoformat()
        # the worker compares it with the slot's date; a manual run marks the schedule's local today
        state["last_article_day"] = slot_day or self.local_now().strftime("%Y-%m-%d")
        failures = state.get("article_failures", {})  # article_id -> failed slots in a row; absent = none yet

        if error is None:
            self.db.update_article_status(
                article_id=target_article.id,
                status=res.status,
                published_url=res.published_url
            )
            failures.pop(target_article.id, None)
            state["last_article_run"] = msk_now.isoformat()
            history_item = {
                "article_id": target_article.id,
                "platform": target_article.platform,
                "title": target_article.title,
                "url": res.published_url,
                "status": res.status,
                "timestamp": msk_now.isoformat()
            }
            state.setdefault("published_history", []).append(history_item)
            # saved before pinging: a failing indexer must not make the published article look unpublished
            AutopilotState.save(state)
            public_url = indexable_url(res)
            if public_url:
                indexer = FastIndexer()
                await asyncio.to_thread(indexer.ping_all, target_article.id, target_article.title, public_url)
            elif res.status != "published" and PLATFORMS[target_article.platform].type != "editorial_pitch":
                # a pitch package is the normal outcome of an editorial platform, a draft is a failed auto-publish
                # the publisher may be unsure (Dzen: status not confirmed in time) — never tell to publish blindly
                details = f"Публикация не подтверждена: {target_article.title} → {res.published_url or 'адрес не получен'}. Проверьте и при необходимости опубликуйте вручную"
                await send_telegram_alert(
                    title=f"Статья на {target_article.platform} не опубликована",
                    error_message=f"{details}\n{res.message}" if res.message else details,
                    context=f"Статус: {res.status}",
                    level="warning"
                )
            logger.info(f"Autopilot: Successfully processed [{target_article.platform}] -> {res.status}")
            return history_item

        failed = failures.pop(target_article.id, 0) + 1
        if failed >= MAX_FAILED_SLOTS:
            self.db.update_article_status(article_id=target_article.id, status="failed", error_message=error)
            outcome = "статья переведена в failed"
        else:
            failures[target_article.id] = failed
            outcome = "статья остаётся в очереди на следующий слот"
        state["article_failures"] = failures
        AutopilotState.save(state)
        logger.error(f"Autopilot: Publication failed [{target_article.platform}] ({failed}/{MAX_FAILED_SLOTS}): {error}")
        await send_telegram_alert(
            title=f"Ошибка публикации статьи на {target_article.platform}",
            error_message=f"Ошибка: {error} (неудача {failed}/{MAX_FAILED_SLOTS}) — {outcome}",
            context=f"Статья: {target_article.title}",
            level="error"
        )
        return {"error": error, "platform": target_article.platform, "failed_slots": failed}

    async def step_telegram_post(self, specific_idx: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Publishes the next planned expert Telegram post to target channel."""
        token = get_bot_token()
        channel = self.channel()

        if not token:
            logger.warning("Autopilot: Cannot post to Telegram - OPP_BOT_TOKEN not configured.")
            return None

        state = AutopilotState.load()
        idx = specific_idx if specific_idx is not None else state.get("next_tg_post_idx", 0)

        # Built-in telegram templates are a neutral scaffold, not publishable copy:
        # only a profile's own posts may go to a real channel.
        own_dir = getattr(self.profile, "templates_path", None)
        if not own_dir or not (own_dir / "telegram" / "posts.yaml.j2").exists():
            logger.warning(
                "Autopilot: profile has no own telegram/posts.yaml.j2 in its templates dir; "
                "refusing to post the built-in scaffold to the channel."
            )
            return None

        try:
            posts = load_posts(self.profile)
        except Exception as e:
            # A broken template does not heal within the slot: one alert, and the caller marks the slot
            logger.exception("Autopilot: cannot render the profile's telegram/posts.yaml.j2")
            await send_telegram_alert(
                title="Ошибка шаблона постов Telegram — слот пропущен",
                error_message=str(e),
                context="telegram/posts.yaml.j2",
                level="error"
            )
            return {"error": str(e), "skip_slot": True}
        if not posts:
            logger.warning("Autopilot: Profile has no Telegram posts (telegram/posts.yaml.j2).")
            return None
        if idx >= len(posts):
            logger.info("Autopilot: All Telegram template posts already delivered. Recycling queue.")
            idx = 0

        post_data = posts[idx]
        text_content = post_data["content"].strip()

        import re
        links = []
        def save_link(match):
            anchor = match.group(1)
            url = match.group(2)
            links.append((anchor, url))
            return f"__LINK_{len(links)-1}__"

        text_html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", save_link, text_content)
        text_html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text_html)
        for i, (anchor, url) in enumerate(links):
            text_html = text_html.replace(f"__LINK_{i}__", f'<a href="{url}">{anchor}</a>')

        # One attempt per slot: a failed post keeps its idx, the next slot sends it
        try:
            from aiogram import Bot
            bot = Bot(token=token)
            try:
                await bot.send_message(
                    chat_id=channel,
                    text=text_html,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
            finally:
                await bot.session.close()
        except Exception as e:
            logger.error(f"Autopilot: Failed to post to {channel}: {e}")
            await send_telegram_alert(
                title="Ошибка отправки поста в Telegram-канал",
                error_message=f"Ошибка: {e} — пост уйдёт в следующий слот",
                context=f"Канал: {channel}, Пост: {post_data.get('title', 'Без названия')}",
                level="error"
            )
            return {"error": str(e)}

        # Advance index
        msk_now = get_msk_now()
        state["last_tg_run"] = msk_now.isoformat()
        state["next_tg_post_idx"] = idx + 1
        state.setdefault("published_history", []).append({
            "type": "telegram_post",
            "post_id": post_data["id"],
            "title": post_data["title"],
            "channel": channel,
            "timestamp": msk_now.isoformat()
        })
        AutopilotState.save(state)
        logger.info(f"Autopilot: Successfully posted to Telegram channel {channel}: {post_data['title']}")
        return {"success": True, "post": post_data["title"]}


def _mark_slot(key: str, value: str) -> None:
    state = AutopilotState.load()
    state[key] = value
    AutopilotState.save(state)


async def periodic_autopilot_worker(engine: AutopilotEngine, check_interval_seconds: int = 120):
    """
    Background loop running in serm-daemon supervisor.
    Checks the profile's calendar (profile.schedule, local time) every 2 minutes. Without a schedule section:
    - Articles: Tuesdays & Thursdays at 10:30 MSK
    - Telegram posts on the profile's channel, next post of telegram/posts.yaml.j2 in each slot:
        * Monday 11:00 MSK
        * Tuesday 13:00 MSK (after the morning article)
        * Wednesday 11:30 MSK
        * Thursday 13:00 MSK (after the morning article)
        * Friday 15:00 MSK
    A done slot is marked in the state file (last_article_day / last_tg_slot), so a restart inside the window
    does not publish twice.
    """
    logger.info("Autopilot smart-calendar worker loop initialized.")
    while True:
        try:
            if engine.is_enabled():
                local = engine.local_now()
                article_day, tg_slot_key = due_slots(engine.profile.schedule, local)

                state = AutopilotState.load()

                # --- 1. Article Publication Slots ---
                if article_day:
                    if state.get("last_article_day") != article_day:
                        logger.info(f"Autopilot slot match: Article on {article_day} (time={local:%H:%M} UTC{engine.profile.schedule.utc_offset_hours:+d})")
                        try:
                            await engine.step_article_publication(slot_day=article_day)
                        except Exception:
                            _mark_slot("last_article_day", article_day)  # one attempt per slot, even a crashed one
                            raise
                        # publishing holds the loop for minutes (browser, retries): a TG slot due before it still goes
                        # (late), otherwise the one whose window opened meanwhile
                        if not tg_slot_key or state.get("last_tg_slot") == tg_slot_key:
                            _, tg_slot_key = due_slots(engine.profile.schedule, engine.local_now())

                # --- 2. Telegram Channel Slots ---
                if tg_slot_key and state.get("last_tg_slot") != tg_slot_key:
                    logger.info(f"Autopilot slot match: TG post for slot {tg_slot_key}")
                    try:
                        res = await engine.step_telegram_post()
                    except Exception:
                        _mark_slot("last_tg_slot", tg_slot_key)
                        raise
                    # sent, skipped or failed (one attempt per slot); None = nothing to send yet (no token/posts)
                    if res is not None:
                        _mark_slot("last_tg_slot", tg_slot_key)

        except Exception as e:
            logger.error(f"Error in autopilot calendar loop: {e}")
            await send_telegram_alert(
                title="Критический сбой в цикле автопилота",
                error_message=str(e),
                context="periodic_autopilot_worker",
                level="error"
            )

        await asyncio.sleep(check_interval_seconds)
