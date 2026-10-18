from core.alerts import send_telegram_alert
"""
Autopilot Engine for SERM Distribution & Telegram Channel Automation.
Operates on the server 24/7 inside serm-daemon:
1. Monitors scheduled distribution slots (VC, Dzen, Spark, Executive) according to MSK business calendar.
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
from core.indexer import FastIndexer
from core.telegram_posts import load_posts
from config.settings import DATA_DIR, EXPORTS_DIR, get_bot_token, load_settings
from publishers.registry import enabled, get_publisher

logger = logging.getLogger("AutopilotEngine")

STATE_FILE = DATA_DIR / "autopilot_state.json"
MSK_OFFSET = timedelta(hours=3)


def get_msk_now() -> datetime:
    return datetime.now(timezone.utc) + MSK_OFFSET


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


class AutopilotEngine:
    def __init__(self, campaign_id: Optional[str] = None, profile: Optional[Any] = None):
        if profile is None:
            from core.profile import get_profile
            profile = get_profile()
        self.profile = profile
        self.campaign_id = campaign_id or profile.campaign_id
        self.db = Database()

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
            "current_msk_time": msk_now.strftime("%Y-%m-%d %H:%M:%S MSK"),
            "ready_count": len(ready_articles),
            "published_count": len(published_articles),
            "last_article_run": state.get("last_article_run"),
            "last_tg_run": state.get("last_tg_run"),
            "next_tg_post_idx": state.get("next_tg_post_idx", 0),
            "bot_configured": bool(token),
            "target_channel": channel,
            "history": state.get("published_history", [])[-10:]
        }

    async def step_article_publication(self) -> Optional[Dict[str, Any]]:
        """Publishes the next available article in the campaign queue."""
        ready_articles = self.ready_articles()

        if not ready_articles:
            logger.info("Autopilot: No ready articles left in campaign queue.")
            return None

        # Prioritize UGC platforms with active sessions first (dzen, vc, spark, executive)
        priority = {"dzen": 1, "vc": 2, "spark": 3, "executive": 4, "rb": 5, "tbank": 6, "biz360": 7}
        ready_articles.sort(key=lambda a: priority.get(a.platform, 99))
        target_article = ready_articles[0]

        logger.info(f"Autopilot: Selecting next article for [{target_article.platform}]: {target_article.title[:50]}...")
        
        max_retries = 3
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                publisher = get_publisher(target_article.platform)
                res = await asyncio.to_thread(publisher.publish, target_article)

                if res.success:
                    self.db.update_article_status(
                        article_id=target_article.id,
                        status=res.status,
                        published_url=res.published_url
                    )
                    if res.published_url:
                        indexer = FastIndexer()
                        await asyncio.to_thread(indexer.ping_all, target_article.id, target_article.title, res.published_url)

                    # Record in state
                    state = AutopilotState.load()
                    msk_now = get_msk_now()
                    state["last_article_day"] = msk_now.strftime("%Y-%m-%d")
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
                    AutopilotState.save(state)
                    logger.info(f"Autopilot: Successfully processed [{target_article.platform}] -> {res.status}")
                    return history_item
                else:
                    last_error = res.error or "Неизвестная ошибка публикации"
                    logger.warning(f"Autopilot: Attempt {attempt}/{max_retries} failed [{target_article.platform}]: {res.error}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Autopilot: Attempt {attempt}/{max_retries} exception [{target_article.platform}]: {e}")

            if attempt < max_retries:
                backoff_sec = attempt * 10
                logger.info(f"Autopilot: Retrying article in {backoff_sec}s (attempt {attempt + 1}/{max_retries})...")
                await asyncio.sleep(backoff_sec)

        self.db.update_article_status(article_id=target_article.id, status="failed", error_message=str(last_error))
        logger.error(f"Autopilot: All {max_retries} attempts failed [{target_article.platform}]: {last_error}")
        await send_telegram_alert(
            title=f"Ошибка публикации статьи на {target_article.platform}",
            error_message=f"Система исчерпала все {max_retries} попытки публикации. Ошибка: {last_error}",
            context=f"Статья: {target_article.title}",
            level="error"
        )
        return {"error": str(last_error), "platform": target_article.platform}

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

        max_retries = 3
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                from aiogram import Bot
                bot = Bot(token=token)
                await bot.send_message(
                    chat_id=channel,
                    text=text_html,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
                await bot.session.close()

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

            except Exception as e:
                last_error = e
                logger.warning(f"Autopilot: Attempt {attempt}/{max_retries} failed to post to {channel}: {e}")
                if attempt < max_retries:
                    backoff_sec = attempt * 5
                    logger.info(f"Autopilot: Retrying TG post in {backoff_sec}s (attempt {attempt + 1}/{max_retries})...")
                    await asyncio.sleep(backoff_sec)

        logger.error(f"Autopilot: All {max_retries} attempts failed to post to {channel}: {last_error}")
        await send_telegram_alert(
            title="Ошибка отправки поста в Telegram-канал",
            error_message=f"Система исчерпала все {max_retries} попытки отправки. Ошибка: {last_error}",
            context=f"Канал: {channel}, Пост: {post_data.get('title', 'Без названия')}",
            level="error"
        )
        return {"error": str(last_error)}


async def periodic_autopilot_worker(engine: AutopilotEngine, check_interval_seconds: int = 120):
    """
    Background loop running in serm-daemon supervisor.
    Checks MSK business calendar every 2 minutes:
    - Articles on VC/Dzen: Tuesdays & Thursdays at 10:30 MSK
    - Telegram posts on the profile's channel, next post of telegram/posts.yaml.j2 in each slot:
        * Monday 11:00 MSK
        * Tuesday 13:00 MSK (after the morning article)
        * Wednesday 11:30 MSK
        * Thursday 13:00 MSK (after the morning article)
        * Friday 15:00 MSK
    """
    logger.info("Autopilot smart-calendar worker loop initialized.")
    while True:
        try:
            if engine.is_enabled():
                msk = get_msk_now()
                weekday = msk.weekday()  # 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Sat, 6=Sun
                hour = msk.hour
                minute = msk.minute
                today_str = msk.strftime("%Y-%m-%d")

                state = AutopilotState.load()

                # --- 1. Article Publication Slots (Tue 10:30 MSK, Thu 10:30 MSK) ---
                if weekday in (1, 3) and hour == 10 and minute >= 30:
                    if state.get("last_article_day") != today_str:
                        logger.info(f"Autopilot slot match: Article on {today_str} (weekday={weekday}, time={hour}:{minute:02d} MSK)")
                        await engine.step_article_publication()

                # --- 2. Telegram Channel Slots ---
                tg_slot_key = None
                # Monday 11:00 MSK
                if weekday == 0 and hour == 11 and minute >= 0:
                    tg_slot_key = f"{today_str}_mon_insight"
                # Tuesday 13:00 MSK (after Tuesday morning article)
                elif weekday == 1 and hour == 13 and minute >= 0:
                    tg_slot_key = f"{today_str}_tue_teaser"
                # Wednesday 11:30 MSK
                elif weekday == 2 and hour == 11 and minute >= 30:
                    tg_slot_key = f"{today_str}_wed_case"
                # Thursday 13:00 MSK (after Thursday morning article)
                elif weekday == 3 and hour == 13 and minute >= 0:
                    tg_slot_key = f"{today_str}_thu_teaser"
                # Friday 15:00 MSK
                elif weekday == 4 and hour == 15 and minute >= 0:
                    tg_slot_key = f"{today_str}_fri_vibecoding"

                if tg_slot_key and state.get("last_tg_slot") != tg_slot_key:
                    logger.info(f"Autopilot slot match: TG post for slot {tg_slot_key}")
                    res = await engine.step_telegram_post()
                    if res and (res.get("success") or res.get("skip_slot")):
                        state = AutopilotState.load()
                        state["last_tg_slot"] = tg_slot_key
                        AutopilotState.save(state)

        except Exception as e:
            logger.error(f"Error in autopilot calendar loop: {e}")
            await send_telegram_alert(
                title="Критический сбой в цикле автопилота",
                error_message=str(e),
                context="periodic_autopilot_worker",
                level="error"
            )

        await asyncio.sleep(check_interval_seconds)
