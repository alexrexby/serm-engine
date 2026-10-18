"""
Lead status: after a lead click the bot asks the owner what happened next — contacted, call set, call result,
sale and its amount — with buttons under the lead card in the owner's chat. One row per person in `leads`
(core/db.py); bot_events is not touched. The admin panel only reads it.

Only ADMIN_TG_ID (read fresh on every update) may change a status: callback_data can be forged by a modified
client. Without ADMIN_TG_ID the feature is off (nobody to ask); lead rows are still written by the bot.
Texts: bot/lead_status.yaml.j2 (labels, buttons, products, reminder thresholds), bot/lead_card.html.j2.
Removing the feature = removing `dp.include_router(lead_router(...))` in core/opp_bot.py and the reminder task
in scheduler.py.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import yaml
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core import alerts
from core.db import Database
from core.templating import render

logger = logging.getLogger("LeadStatus")

TEXTS = "bot/lead_status.yaml.j2"
CARD = "bot/lead_card.html.j2"
CALLBACK_PREFIX = "lead:"

# Allowed actions from each status (sale = one button per product); sale/no_sale are terminal
TRANSITIONS: Dict[str, tuple] = {
    "new": ("contacted", "no_contact"),
    "no_contact": ("contacted", "no_sale"),
    "contacted": ("call_set", "no_sale"),
    "call_set": ("call_done", "no_show"),
    "no_show": ("call_set", "no_sale"),
    "call_done": ("sale", "no_sale", "thinking"),
    "thinking": ("sale", "no_sale"),
    "sale": (),
    "no_sale": (),
}
# action -> (new status, stage column stamped on first reach)
ACTIONS: Dict[str, tuple] = {
    "contacted": ("contacted", "contacted_at"),
    "no_contact": ("no_contact", None),
    "call_set": ("call_set", "call_set_at"),
    "no_show": ("no_show", None),
    "call_done": ("call_done", "call_done_at"),
    "thinking": ("thinking", None),
    "sale": ("sale", "closed_at"),
    "no_sale": ("no_sale", "closed_at"),
}
_PRODUCT_ID_RE = re.compile(r"[a-z0-9_]{1,16}")
# "150000", "150 000", "150к", "1,5 млн", "150 000 ₽": up to 12 digits after scaling
_AMOUNT_RE = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(к|k|тыс\.?|млн\.?)?\s*(₽|руб\.?|р\.?|rub)?\s*$", re.I)
_AMOUNT_SCALE = {"к": 1_000, "k": 1_000, "тыс": 1_000, "млн": 1_000_000}
# The owner has this long to type the amount after the question; a number in the chat later is not a sum
AMOUNT_TTL_S = 30 * 60
# Reminders only in the owner's daytime, local time of profile.schedule
LEADS_SHOWN = 10  # /leads: newest open leads as cards, the rest as a count
REMIND_FROM_HOUR = 10
REMIND_TO_HOUR = 20

# Owner waiting to type a sale amount: {admin id: (lead id, monotonic time asked)}. Lost on restart — the card
# keeps a "sum" button.
awaiting_amount: Dict[int, tuple] = {}


def parse_amount(text: str) -> Optional[int]:
    match = _AMOUNT_RE.match(text or "")
    if not match:
        return None
    number = float(re.sub(r"\s", "", match.group(1)).replace(",", "."))
    scale = _AMOUNT_SCALE.get((match.group(2) or "").lower().rstrip("."), 1)
    amount = int(round(number * scale))
    return amount if 0 < amount < 10 ** 12 else None


def load_texts(profile: Any) -> Dict[str, Any]:
    data = yaml.safe_load(render(TEXTS, profile)) or {}
    products: List[Dict[str, str]] = []
    for entry in data.get("products") or []:
        product_id = str(entry.get("id", ""))
        if not _PRODUCT_ID_RE.fullmatch(product_id) or any(p["id"] == product_id for p in products):
            logger.warning(f"Lead status: product id {product_id!r} skipped (need unique [a-z0-9_], up to 16 chars)")
            continue
        tier = profile.offer_tier(product_id)
        products.append({"id": product_id, "label": entry.get("label") or (tier.title if tier else product_id)})
    data["products"] = products
    # Only statuses that still expect an action can be reminded about
    data["reminders"] = {status: int(hours) for status, hours in (data.get("reminders") or {}).items()
                         if TRANSITIONS.get(status)}
    return data


def is_admin(user: Any, chat: Any) -> bool:
    admin_id = alerts.get_admin_id()
    return (admin_id is not None and user is not None and user.id == admin_id
            and chat is not None and chat.type == "private")


def _local(value: Optional[str], offset_hours: int) -> Optional[str]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return (moment + timedelta(hours=offset_hours)).strftime("%d.%m %H:%M")


def card_text(profile: Any, texts: Dict[str, Any], lead: Dict[str, Any], interests: Dict[str, str],
              question: Optional[str] = None) -> str:
    offset = profile.schedule.utc_offset_hours
    stages = {"created": "created_at", "contacted": "contacted_at", "call_set": "call_set_at",
              "call_done": "call_done_at", "closed": "closed_at"}
    product = next((p["label"] for p in texts["products"] if p["id"] == lead.get("product")), lead.get("product") or "")
    status = lead["status"]
    return render(
        CARD, profile, lead=lead,
        interest=interests.get(lead.get("interest") or "", lead.get("interest") or ""),
        status=texts["labels"].get(status, status),
        at={key: _local(lead.get(column), offset) for key, column in stages.items()},
        product=product, currency=texts.get("currency", ""),
        question=(texts["questions"].get(status) or "") if question is None else question)


def keyboard(texts: Dict[str, Any], lead: Dict[str, Any], awaiting: bool = False) -> Optional[InlineKeyboardMarkup]:
    prefix = f"{CALLBACK_PREFIX}{lead['id']}:"
    actions = texts["actions"]
    rows = []
    for action in TRANSITIONS.get(lead["status"], ()):
        if action == "sale":
            rows.extend([InlineKeyboardButton(text=f"{actions['sale']} {p['label']}",
                                              callback_data=f"{prefix}sale:{p['id']}")]
                        for p in texts["products"])
        else:
            rows.append([InlineKeyboardButton(text=actions[action], callback_data=prefix + action)])
    if lead["status"] == "sale" and lead.get("amount") is None:
        action = "noamount" if awaiting else "amount"
        rows.append([InlineKeyboardButton(text=actions[action], callback_data=prefix + action)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def alert_keyboard(profile: Any, lead: Dict[str, Any]) -> Optional[InlineKeyboardMarkup]:
    """Status buttons under the lead alert; None without ADMIN_TG_ID (the alert then goes out exactly as before)."""
    if alerts.get_admin_id() is None:
        return None
    return keyboard(load_texts(profile), lead)


def lead_router(profile: Any, db: Database, interests: Optional[Dict[str, str]] = None) -> Router:
    router = Router(name="lead_status")
    texts = load_texts(profile)
    interests = interests or {}
    products = {p["id"] for p in texts["products"]}

    async def show(callback: CallbackQuery, lead: Dict[str, Any], question: Optional[str] = None,
                   awaiting: bool = False) -> None:
        try:
            await callback.message.edit_text(card_text(profile, texts, lead, interests, question),
                                             parse_mode="HTML", reply_markup=keyboard(texts, lead, awaiting))
        except Exception as e:  # same text ("not modified") or a message too old to edit
            logger.warning(f"Lead card {lead['id']} not updated: {e}")

    @router.callback_query(F.data.startswith(CALLBACK_PREFIX))
    async def cb_lead(callback: CallbackQuery):
        if not is_admin(callback.from_user, callback.message.chat if callback.message else None):
            await callback.answer()
            return
        admin_id = callback.from_user.id
        parts = callback.data.split(":")
        lead = db.get_lead(int(parts[1])) if len(parts) in (3, 4) and parts[1].isdigit() else None
        if lead is None:
            await callback.answer()
            return
        action = parts[2]
        stale = texts["stale"].replace("{status}", texts["labels"].get(lead["status"], lead["status"]))

        if action in ("amount", "noamount") and len(parts) == 3:
            if lead["status"] != "sale" or lead["amount"] is not None:
                await callback.answer(stale)
                await show(callback, lead)
                return
            if action == "amount":
                awaiting_amount[admin_id] = (lead["id"], time.monotonic())
                await callback.answer()
                await show(callback, lead, texts["amount_question"], awaiting=True)
            else:
                if (awaiting_amount.get(admin_id) or (None,))[0] == lead["id"]:
                    awaiting_amount.pop(admin_id)
                await callback.answer()
                await show(callback, lead)
            return

        product = None
        if action == "sale":
            product = parts[3] if len(parts) == 4 else None
            if product not in products:
                await callback.answer()
                return
        elif len(parts) != 3 or action not in ACTIONS:
            await callback.answer()
            return
        if action not in TRANSITIONS.get(lead["status"], ()):
            # A button of an older card: the stage is not doubled, the card shows the real state
            await callback.answer(stale)
            await show(callback, lead)
            return

        new_status, column = ACTIONS[action]
        if not db.set_lead_status(lead["id"], lead["status"], new_status, column, product):
            lead = db.get_lead(lead["id"])  # pressed twice: the other tap won
            await callback.answer(texts["stale"].replace("{status}", texts["labels"].get(lead["status"], "")))
            await show(callback, lead)
            return
        lead = db.get_lead(lead["id"])
        await callback.answer(texts["labels"].get(new_status, new_status))
        if new_status == "sale":
            awaiting_amount[admin_id] = (lead["id"], time.monotonic())
            await show(callback, lead, texts["amount_question"], awaiting=True)
        else:
            await show(callback, lead)

    # Only while the owner is asked for an amount (commands pass by): other users' messages never match
    @router.message(F.text, lambda m: m.from_user is not None and m.from_user.id in awaiting_amount
                    and not m.text.startswith("/"))
    async def msg_amount(message: Message):
        if not is_admin(message.from_user, message.chat):
            return
        lead_id, asked = awaiting_amount[message.from_user.id]
        if time.monotonic() - asked > AMOUNT_TTL_S:
            awaiting_amount.pop(message.from_user.id)
            await message.answer(texts.get("amount_expired", "Время ввода суммы истекло — нажмите «Указать сумму» на карточке."))
            return
        amount = parse_amount(message.text)
        if amount is None:  # keep waiting: the owner retypes
            await message.answer(texts.get("amount_bad", "Не понял сумму. Напишите числом, например 150000 или 150к."))
            return
        awaiting_amount.pop(message.from_user.id)
        if not db.set_lead_amount(lead_id, amount):
            await message.answer(texts.get("amount_failed", "Сумму записать не удалось: у этого лида она уже есть или он не в «Продаже»."))
            return
        lead = db.get_lead(lead_id)
        await message.answer(texts["amount_saved"] + "\n\n" + card_text(profile, texts, lead, interests),
                             parse_mode="HTML", reply_markup=keyboard(texts, lead))

    @router.message(Command("leads"))
    async def cmd_leads(message: Message):
        if not is_admin(message.from_user, message.chat):
            return  # silence: the command is not advertised and does nothing for others
        leads = db.list_leads(limit=10_000, open_only=True)
        if not leads:
            await message.answer(texts["no_open"], parse_mode="HTML")
            return
        for lead in leads[:LEADS_SHOWN]:
            await message.answer(card_text(profile, texts, lead, interests), parse_mode="HTML",
                                 reply_markup=keyboard(texts, lead))
        if len(leads) > LEADS_SHOWN:
            more = texts.get("more_open", "Ещё открытых: {n} — список в админке, вкладка «Аналитика».")
            await message.answer(more.replace("{n}", str(len(leads) - LEADS_SHOWN)))

    return router


async def remind_due(profile: Any, db: Database, now: Optional[datetime] = None,
                     interests: Optional[Dict[str, str]] = None) -> int:
    """One reminder card per lead and status past its threshold; only 10:00–20:00 local time of the profile.
    Returns how many were sent; an unsent one is retried on the next tick."""
    if alerts.get_admin_id() is None:
        return 0
    now = now or datetime.now(timezone.utc)
    local_hour = (now.astimezone(timezone.utc) + timedelta(hours=profile.schedule.utc_offset_hours)).hour
    if not REMIND_FROM_HOUR <= local_hour < REMIND_TO_HOUR:
        return 0
    texts = load_texts(profile)
    sent = 0
    for lead in db.due_lead_reminders(texts["reminders"], now):
        if await alerts.send_admin_message(card_text(profile, texts, lead, interests or {}), keyboard(texts, lead)):
            db.mark_reminded(lead["id"], lead["status"])
            sent += 1
    return sent


async def periodic_lead_reminders(interval_s: int = 1800):
    """Daemon loop (scheduler.py): reminders about leads waiting for the owner's answer."""
    from core.opp_bot import lead_interests
    from core.profile import get_profile
    db = Database()
    while True:
        try:
            profile = get_profile()
            sent = await remind_due(profile, db, interests=lead_interests(profile))
            if sent:
                logger.info(f"Lead reminders sent: {sent}")
        except Exception:
            logger.exception("Lead reminders failed")
        await asyncio.sleep(interval_s)
