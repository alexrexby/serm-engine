"""
Telegram Alerting & Critical Notification Service.
Sends high-priority alerts and milestone notifications directly to the administrator.
"""
import sys
import logging
import asyncio
from html import escape
from typing import Any, Optional, Set
from aiogram import Bot

from config import settings

logger = logging.getLogger("AlertsService")

DEFAULT_ADMIN_ID = None

# Fire-and-forget alert tasks: the loop keeps only a weak reference, an unreferenced task may be collected
_background: Set[asyncio.Task] = set()


# Read fresh on every alert (env, then .env): the admin panel may change values at runtime
def get_admin_id() -> Optional[int]:
    try:
        admin_id = settings.load_settings().admin_tg_id
    except Exception as e:
        logger.error(f"Alert admin id unavailable: {e}")
        return DEFAULT_ADMIN_ID
    return admin_id if admin_id is not None else DEFAULT_ADMIN_ID


def get_bot_token() -> Optional[str]:
    try:
        return settings.get_bot_token() or None
    except Exception as e:
        logger.error(f"Alert bot token unavailable: {e}")
        return None


async def send_telegram_alert(
    title: str,
    error_message: str,
    context: Optional[str] = None,
    level: str = "error",
    reply_markup: Optional[Any] = None
) -> bool:
    """
    Sends structured alert message to the administrator.
    Levels: 'error', 'warning', 'milestone', 'info'
    reply_markup: inline keyboard under the alert (lead status buttons); omitted from the request when None.
    """
    token = get_bot_token()
    admin_id = get_admin_id()
    if not token or not admin_id:
        logger.warning("Telegram alert skipped: token or admin_id missing.")
        return False

    icon = "🚨" if level == "error" else ("⚠️" if level == "warning" else ("🎉" if level == "milestone" else "ℹ️"))
    
    text = f"{icon} <b>Оповещение системы SERM</b>\n\n"
    text += f"<b>Событие:</b> {escape(title)}\n"
    if context:
        text += f"<b>Контекст:</b> <code>{escape(context)}</code>\n"
    text += f"<b>Детали:</b>\n<code>{escape(error_message[:3500])}</code>\n"

    try:
        async with Bot(token=token) as bot:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                **({"reply_markup": reply_markup} if reply_markup is not None else {})
            )
        logger.info(f"Telegram alert sent to admin {admin_id}: {title}")
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False


async def send_admin_message(text_html: str, reply_markup: Optional[Any] = None) -> bool:
    """A ready Telegram HTML message to the administrator (lead cards, reminders), without the alert frame.
    The caller escapes user input; same token and admin id as the alerts."""
    token = get_bot_token()
    admin_id = get_admin_id()
    if not token or not admin_id:
        logger.warning("Admin message skipped: token or admin_id missing.")
        return False
    try:
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=admin_id, text=text_html, parse_mode="HTML", reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.error(f"Failed to send admin message: {e}")
        return False


def _forget(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background Telegram alert failed", exc_info=task.exception())


def notify_admin_sync(title: str, error_message: str, context: Optional[str] = None, level: str = "error"):
    """Fire-and-forget sync wrapper for background alerting."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            task = asyncio.create_task(send_telegram_alert(title, error_message, context, level))
            _background.add(task)
            task.add_done_callback(_forget)
        else:
            loop.run_until_complete(send_telegram_alert(title, error_message, context, level))
    except Exception:
        asyncio.run(send_telegram_alert(title, error_message, context, level))
