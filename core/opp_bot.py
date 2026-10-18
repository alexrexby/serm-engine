"""
OPP Telegram Bot (Organic Post-Processing & Dynamic Lead Magnet Delivery).
Runs on aiogram 3.x with strict HTML formatting, deep-linking router, and Conversion Analytics.

Deep link routes come from the profile (`lead_magnets`): /start <slug> -> the first lead magnet
(in profile order) with a `match` substring found in the slug; anything else -> default_lead_magnet,
logged under the raw slug. /start without a payload is an organic visit (no article link): the same
subscription gate, then the offer menu without a PDF (bot/organic_welcome.html.j2), logged as "organic".
Funnel buttons are tier_<id> callbacks of offer tiers.
Texts: bot/*.html.j2 (Telegram HTML, no autoescape — user input through |e), bot/buttons.yaml.j2,
bot/alerts.yaml.j2, bot/alert_*.txt.j2.
"""
import os
import re
import sys
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import yaml
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)
from aiogram.filters import CommandStart, CommandObject
from aiogram.enums import ChatMemberStatus
from jinja2 import TemplateNotFound

from core.db import Database
from core.lead_status import alert_keyboard, lead_router
from core.profile import ProfileError, get_profile, load_profile, resolve_profile_path
from core.templating import render

logger = logging.getLogger("OPPBot")


def _profile_defaults():
    # Read at import: run_opp_bot's defaults are the installation's channel/contact. Without a profile
    # (e.g. before `cli.py init`) the import must not fail; the bot itself then stops with ProfileError.
    # Loaded uncached: importing this module must not pin get_profile() before SERM_PROFILE is final.
    try:
        telegram = load_profile(resolve_profile_path()).telegram
    except ProfileError:
        return None, None
    return telegram.channel, telegram.contact


DEFAULT_CHANNEL, DEFAULT_CONTACT = _profile_defaults()

# Deep-link payload alphabet and length (Telegram); a hand-typed /start is cut to the same before the DB
_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
SLUG_MAX_LEN = 64
# bot_events.slug of /start without a payload (reader came to the bot not from an article link)
ORGANIC_SLUG = "organic"

# In-memory storage for pending lead magnet requests by user_id
user_pending_magnet: Dict[int, str] = {}


def _profile(profile: Optional[Any]) -> Any:
    return profile if profile is not None else get_profile()


def _tg_url(username: str) -> str:
    return f"https://t.me/{username.lstrip('@')}"


def _default_magnet(profile: Any):
    magnet = profile.lead_magnet() or (profile.lead_magnets[0] if profile.lead_magnets else None)
    if magnet is None:
        raise ProfileError("profile has no lead_magnets: the bot has nothing to deliver")
    return magnet


def start_slug(args: Optional[str]) -> str:
    """/start argument as stored in bot_events.slug: [A-Za-z0-9_-], at most 64 chars ("" when nothing is left)."""
    return _SLUG_UNSAFE_RE.sub("", args or "")[:SLUG_MAX_LEN]


def resolve_magnet_info(slug: Optional[str], exports_dir: Optional[Path], profile: Optional[Any] = None) -> Dict[str, Any]:
    profile = _profile(profile)
    slug_key = (slug or "").lower().strip()

    magnet = next((m for m in profile.lead_magnets
                   if any(p and p.lower() in slug_key for p in m.match)), None)
    if magnet is not None:
        resolved_slug = magnet.slug
    else:
        magnet = _default_magnet(profile)
        resolved_slug = slug_key or "general"

    target_pdf = None
    if exports_dir and exports_dir.exists():
        matches = list(exports_dir.glob(f"**/{magnet.file_prefix}"))
        if matches:
            target_pdf = matches[0]
        else:
            any_pdf = list(exports_dir.glob("**/lead_magnet_*.pdf"))
            if any_pdf:
                target_pdf = any_pdf[0]

    return {
        "slug": resolved_slug,
        "raw_slug": slug or "general",
        "title": magnet.title,
        "filename": magnet.download_filename,
        "desc": magnet.desc,
        "pdf_path": target_pdf
    }


def _buttons(profile: Any, channel_username: str, contact_username: Optional[str] = None) -> Dict[str, Any]:
    contact = contact_username or profile.telegram.contact
    data = yaml.safe_load(render(
        "bot/buttons.yaml.j2", profile, channel=channel_username, channel_url=_tg_url(channel_username),
        contact=contact, contact_url=_tg_url(contact))) or {}
    funnel: List[Dict[str, Any]] = []
    for entry in data.get("funnel") or []:
        tier = profile.offer_tier(entry["id"])
        funnel.append({
            "id": entry["id"],
            "button": entry.get("button") or (tier.cta or tier.title if tier else entry["id"]),
            "reply_button": entry.get("reply_button") or entry["id"],
            "reply_url": entry.get("reply_url") or (tier.url if tier and tier.url else _tg_url(contact)),
            "lead": entry.get("lead", True),
            # magnet: the button sends the lead magnet PDF in the bot (hidden right after a PDF was sent)
            "magnet": bool(entry.get("magnet", False)),
            # hidden: not in new menus, but buttons of menus already sent keep working
            "hidden": bool(entry.get("hidden", False)),
            # interest: what the owner's lead alert names (default: the offer tier title)
            "interest": entry.get("interest"),
            "tier": tier,
        })
    data["funnel"] = funnel
    return data


def lead_interests(profile: Any, channel_username: Optional[str] = None) -> Dict[str, str]:
    """leads.interest ('tier_<id>') -> what the lead alert names: the entry's interest, else the tier title."""
    buttons = _buttons(profile, channel_username or profile.telegram.channel)
    return {f"tier_{entry['id']}": entry["interest"] or (entry["tier"].title if entry["tier"] else entry["id"])
            for entry in buttons["funnel"]}


def get_subscription_keyboard(channel_username: str, profile: Optional[Any] = None,
                              organic: bool = False) -> InlineKeyboardMarkup:
    buttons = _buttons(_profile(profile), channel_username)
    # Organic gate delivers no material, so its button must not promise one
    check_sub = buttons.get("check_sub_organic", buttons["check_sub"]) if organic else buttons["check_sub"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=buttons["subscribe"], url=_tg_url(channel_username))],
        # The organic marker travels in callback_data: process memory is lost on a bot restart
        [InlineKeyboardButton(text=check_sub, callback_data="check_sub_organic" if organic else "check_sub")]
    ])


def get_funnel_keyboard(profile: Optional[Any] = None, channel_username: Optional[str] = None,
                        after_magnet: bool = False) -> InlineKeyboardMarkup:
    profile = _profile(profile)
    buttons = _buttons(profile, channel_username or profile.telegram.channel)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=entry["button"], callback_data=f"tier_{entry['id']}")]
        for entry in buttons["funnel"]
        if not entry["hidden"] and not (after_magnet and entry["magnet"])
    ])


def _alert(profile: Any, kind: str, message: str, reply_markup: Optional[InlineKeyboardMarkup] = None,
           **ctx: Any) -> None:
    """Fire-and-forget admin alert; texts from bot/alerts.yaml.j2. reply_markup is passed on only when set."""
    from core.alerts import send_telegram_alert
    meta = yaml.safe_load(render("bot/alerts.yaml.j2", profile, **ctx))[kind]
    asyncio.create_task(send_telegram_alert(
        title=meta["title"],
        error_message=message,
        context=meta.get("context"),
        level="milestone",
        **({"reply_markup": reply_markup} if reply_markup is not None else {})
    ))


async def is_user_subscribed(bot: Bot, channel_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        ]
    except Exception as e:
        logger.warning(f"Failed to check subscription for {user_id} in {channel_id}: {e}")
        return False


async def send_lead_magnet(bot: Bot, chat_id: int, magnet_info: Dict[str, Any], db: Database,
                           user_meta: Optional[Dict[str, Any]] = None, profile: Optional[Any] = None,
                           channel_username: Optional[str] = None, with_funnel: bool = True):
    profile = _profile(profile)
    welcome_text = render("bot/magnet_caption.html.j2", profile, magnet=magnet_info)

    pdf_path = magnet_info.get("pdf_path")
    if pdf_path and pdf_path.exists():
        document = FSInputFile(str(pdf_path), filename=magnet_info["filename"])
        await bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=welcome_text,
            parse_mode="HTML"
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            parse_mode="HTML"
        )

    # Log PDF delivery event
    if user_meta:
        db.log_bot_event(
            user_id=chat_id,
            username=user_meta.get("username"),
            first_name=user_meta.get("first_name"),
            event_type="pdf_delivered",
            # the visit source (the /start slug), as the start event: per-platform rows stay whole
            slug=magnet_info.get("raw_slug") or magnet_info.get("slug", "general"),
            details=magnet_info.get("filename", "")
        )

    if with_funnel:
        await _send_funnel(bot, chat_id, profile, channel_username, after_magnet=True)


async def _send_funnel(bot: Bot, chat_id: int, profile: Any, channel_username: Optional[str],
                       after_magnet: bool = False) -> None:
    # Offer tiers
    await bot.send_message(
        chat_id=chat_id,
        text=render("bot/funnel.html.j2", profile, after_magnet=after_magnet),
        reply_markup=get_funnel_keyboard(profile, channel_username, after_magnet=after_magnet),
        parse_mode="HTML"
    )


async def send_organic_menu(bot: Bot, chat_id: int, profile: Optional[Any] = None,
                            channel_username: Optional[str] = None, first_name: Optional[str] = None):
    """Subscribed organic visitor: a welcome without a lead magnet, then the offer tiers."""
    profile = _profile(profile)
    channel = channel_username or profile.telegram.channel
    await bot.send_message(
        chat_id=chat_id,
        text=render("bot/organic_welcome.html.j2", profile, subscribed=True,
                    channel=channel, first_name=first_name),
        parse_mode="HTML"
    )
    await _send_funnel(bot, chat_id, profile, channel_username)


def _tier_text(profile: Any, tier_id: str, **ctx: Any) -> str:
    """bot/tier_<id>.html.j2 if the profile/built-ins have it, else the generic bot/tier.html.j2."""
    name = f"bot/tier_{tier_id}.html.j2"
    try:
        return render(name, profile, tier_id=tier_id, **ctx)
    except TemplateNotFound as e:
        if e.name != name:
            raise
    return render("bot/tier.html.j2", profile, tier_id=tier_id, **ctx)


def _restored_slug(db: Database, user_id: int) -> str:
    """Lead magnet slug for a gate pressed after a restart: the latest /start; organic -> "" (no PDF, as
    without a restart), unknown -> the default magnet."""
    slug = db.last_start_slug(user_id)
    if slug == ORGANIC_SLUG:
        return ""
    return slug or "general"


def create_opp_dispatcher(channel_username: str, exports_dir: Optional[Path], contact_username: str,
                          profile: Optional[Any] = None) -> Dispatcher:
    profile = _profile(profile)
    # Import-time DEFAULT_* are None when the profile appeared after import: take them from the profile now.
    channel_username = channel_username or profile.telegram.channel
    contact_username = contact_username or profile.telegram.contact
    _default_magnet(profile)  # fail at startup, not on the first /start
    dp = Dispatcher()
    db = Database()
    buttons = _buttons(profile, channel_username, contact_username)
    funnel = {entry["id"]: entry for entry in buttons["funnel"]}
    # Owner's lead status buttons (core/lead_status.py); removing the feature = removing this line
    dp.include_router(lead_router(profile, db, lead_interests(profile, channel_username)))

    @dp.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject, bot: Bot):
        user = message.from_user
        user_id = user.id
        slug = start_slug(command.args)
        user_pending_magnet[user_id] = slug  # "" = organic visit

        # Log Start Event
        db.log_bot_event(
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
            event_type="start",
            slug=slug or ORGANIC_SLUG,
            details="User entered via /start"
        )

        try:
            summary = db.get_funnel_summary()
            starts = summary.get("unique_starts", 0)
            if starts in (50, 100):
                # Link only from config, never with the access token in it
                admin_url = os.getenv("PUBLIC_ADMIN_URL", "").strip()
                _alert(profile, "milestone",
                       render("bot/alert_milestone.txt.j2", profile, starts=starts, admin_url=admin_url),
                       starts=starts)
        except Exception:
            logger.exception("Bot milestone alert failed")

        subscribed = await is_user_subscribed(bot, channel_username, user_id)
        if not slug:
            db.log_bot_event(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                event_type="subscribed" if subscribed else "check_sub_attempt",
                slug=ORGANIC_SLUG,
                details="Already subscribed on start" if subscribed else "Unsubscribed, gate displayed"
            )
            if subscribed:
                await send_organic_menu(bot, message.chat.id, profile, channel_username, user.first_name)
            else:
                await message.answer(
                    text=render("bot/organic_welcome.html.j2", profile, subscribed=False,
                                channel=channel_username, first_name=user.first_name),
                    reply_markup=get_subscription_keyboard(channel_username, profile, organic=True),
                    parse_mode="HTML"
                )
            return

        magnet = resolve_magnet_info(slug, exports_dir, profile)
        user_meta = {"username": user.username, "first_name": user.first_name}

        if subscribed:
            db.log_bot_event(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                event_type="subscribed",
                slug=slug,
                details="Already subscribed on start"
            )
            await send_lead_magnet(bot, message.chat.id, magnet, db, user_meta, profile, channel_username)
        else:
            db.log_bot_event(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                event_type="check_sub_attempt",
                slug=slug,
                details="Unsubscribed, gate displayed"
            )

            not_sub_text = render("bot/need_sub.html.j2", profile, magnet=magnet,
                                  channel=channel_username, first_name=user.first_name)
            await message.answer(
                text=not_sub_text,
                reply_markup=get_subscription_keyboard(channel_username, profile),
                parse_mode="HTML"
            )

    @dp.callback_query(F.data.in_({"check_sub", "check_sub_organic"}))
    async def cb_check_sub(callback: CallbackQuery, bot: Bot):
        user = callback.from_user
        user_id = user.id
        subscribed = await is_user_subscribed(bot, channel_username, user_id)
        if callback.data == "check_sub_organic":
            slug = ""  # organic gate: never a lead magnet, even after a restart
        else:
            # No pending entry (the bot restarted after the gate): the latest /start from the DB, else default
            slug = user_pending_magnet[user_id] if user_id in user_pending_magnet else _restored_slug(db, user_id)
        magnet = resolve_magnet_info(slug, exports_dir, profile) if slug else None
        event_slug = magnet["raw_slug"] if magnet else ORGANIC_SLUG

        if subscribed:
            db.log_bot_event(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                event_type="subscribed",
                slug=event_slug,
                details="Subscribed verified via callback button"
            )

            await callback.answer(buttons["sub_confirmed"], show_alert=False)
            try:
                await callback.message.delete()
            except Exception:
                pass

            if magnet is None:
                await send_organic_menu(bot, callback.message.chat.id, profile, channel_username, user.first_name)
                return
            user_meta = {"username": user.username, "first_name": user.first_name}
            await send_lead_magnet(bot, callback.message.chat.id, magnet, db, user_meta, profile, channel_username)
        else:
            db.log_bot_event(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                event_type="check_sub_attempt",
                slug=event_slug,
                details="User clicked check_sub but not subscribed"
            )
            await callback.answer(buttons["not_subscribed"], show_alert=True)

    # Offer tiers (commercial conversion tracking): callback tier_<id> of the funnel keyboard
    @dp.callback_query(F.data.startswith("tier_"))
    async def cb_tier(callback: CallbackQuery, bot: Bot):
        tier_id = callback.data[len("tier_"):]
        entry = funnel.get(tier_id)
        if entry is None:
            await callback.answer()
            return
        user = callback.from_user
        if user.id in user_pending_magnet:
            slug = user_pending_magnet[user.id] or ORGANIC_SLUG
        else:  # the bot restarted since this user's /start
            slug = db.last_start_slug(user.id) or "general"
        if entry["magnet"] and not await is_user_subscribed(bot, channel_username, user.id):
            # An old menu after unsubscribing: the material stays behind the subscription
            await callback.answer(buttons["not_subscribed"], show_alert=True)
            return
        db.log_bot_event(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            # Analytics counts tier_click as a lead: a free button must not inflate it
            event_type="tier_click" if entry["lead"] else "free_click",
            slug=slug,
            details=f"tier_{tier_id}"
        )
        await callback.answer()

        if entry["magnet"]:
            # The default lead magnet (what the button names), logged under the visit's source slug;
            # user_pending_magnet stays untouched, no second menu
            magnet = dict(resolve_magnet_info("general", exports_dir, profile), slug=slug, raw_slug=slug)
            user_meta = {"username": user.username, "first_name": user.first_name}
            await send_lead_magnet(bot, callback.message.chat.id, magnet, db, user_meta, profile,
                                   channel_username, with_funnel=False)
            return

        tier = entry["tier"]
        if entry["lead"]:
            status_buttons = None
            try:
                lead_id = db.upsert_lead(user.id, user.username, user.first_name, slug, f"tier_{tier_id}")
                status_buttons = alert_keyboard(profile, db.get_lead(lead_id))
            except Exception:
                # Lead status is the owner's bookkeeping: its failure must not cost the alert
                logger.exception(f"Bot lead status failed (tier_{tier_id}, user {user.id})")
            try:
                _alert(profile, "lead", render(
                    "bot/alert_lead.txt.j2", profile, username=user.username, first_name=user.first_name,
                    user_id=user.id, interest=entry["interest"] or (tier.title if tier else tier_id), slug=slug),
                    reply_markup=status_buttons)
            except Exception:
                # The lead alert is the owner's money signal: never lose it silently
                logger.exception(f"Bot lead alert failed (tier_{tier_id}, user {user.id})")
        await callback.message.answer(
            _tier_text(profile, tier_id, tier=tier, channel=channel_username, channel_url=_tg_url(channel_username),
                       contact=contact_username, contact_url=_tg_url(contact_username)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=entry["reply_button"], url=entry["reply_url"])]
            ]),
            parse_mode="HTML"
        )

    return dp


async def poll_opp_bot(token: str, dp: Dispatcher, channel_username: str, exports_dir: Optional[Path] = None,
                       handle_signals: bool = False):
    """Polling over a dispatcher built by the caller (scheduler builds it before the task: a broken profile or
    template then fails at start, not inside a task reported as running).
    handle_signals=False by default: inside scheduler.py the daemon owns SIGTERM/SIGINT (aiogram would replace
    its handler on every bot restart and stop only polling); the standalone bot (run_opp_bot) lets aiogram handle them."""
    bot = Bot(token=token)
    logger.info(f"Starting OPP Bot for channel {channel_username} with dynamic lead magnet router: {exports_dir}")
    try:
        await dp.start_polling(bot, handle_signals=handle_signals)
    finally:
        await bot.session.close()


async def run_opp_bot(token: str, channel_username: str = DEFAULT_CHANNEL, exports_dir: Optional[Path] = None, contact_username: str = DEFAULT_CONTACT):
    dp = create_opp_dispatcher(channel_username, exports_dir, contact_username)
    await poll_opp_bot(token, dp, channel_username, exports_dir, handle_signals=True)


if __name__ == "__main__":
    from config.settings import EXPORTS_DIR, get_bot_token
    bot_token = get_bot_token()
    if not bot_token:
        print("OPP_BOT_TOKEN is not set.")
        sys.exit(1)

    asyncio.run(run_opp_bot(bot_token, DEFAULT_CHANNEL, EXPORTS_DIR, DEFAULT_CONTACT))
