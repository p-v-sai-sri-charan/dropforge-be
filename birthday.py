"""Birthday offers: on a customer's birthday (from their profile date of birth, India time) they get a personal one-use
discount code valid for a week, by push and email. Each customer is claimed atomically once per year, so several server
workers never send it twice. Customers who turned promotions off get no push; the email goes only to verified addresses."""
import logging
import secrets
from datetime import date, datetime, timedelta, timezone

from core import db
from mailer import SITE, email_enabled, layout, send_email
from push import notify_user

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
SEND_FROM_HOUR = 9  # don't ping anyone at midnight
VALID_DAYS = 7
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _birthday_keys(today: date) -> list:
    """'-MM-DD' endings whose birthday is today (29 Feb birthdays are celebrated on 28 Feb in other years)."""
    keys = [today.strftime("-%m-%d")]
    leap = today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0)
    if today.month == 2 and today.day == 28 and not leap:
        keys.append("-02-29")
    return keys


async def send_birthday_offers(now: datetime | None = None) -> int:
    from store_routes import load_store

    local = (now or datetime.now(timezone.utc)).astimezone(IST)
    if local.hour < SEND_FROM_HOUR:
        return 0
    pct = (await load_store()).birthday_pct
    if pct <= 0:
        return 0
    year = local.year
    pattern = "(" + "|".join(k.replace("-", "\\-") + "$" for k in _birthday_keys(local.date())) + ")"
    sent = 0
    while True:
        u = await db.users.find_one_and_update(
            {"dob": {"$regex": pattern}, "birthday_offer_year": {"$ne": year}, "deleted": {"$ne": True}},
            {"$set": {"birthday_offer_year": year}},
        )
        if not u:
            return sent
        code = "BDAY" + "".join(secrets.choice(ALPHABET) for _ in range(6))
        expires = (local.date() + timedelta(days=VALID_DAYS)).isoformat()
        await db.discounts.insert_one({
            "code": code, "percent": pct, "expires_at": expires, "active": True, "uses": 0, "max_uses": 1,
            "user_id": u["id"], "once_per_user": True, "kind": "birthday", "created_at": local.astimezone(timezone.utc).isoformat(),
        })
        first = (u.get("name") or "").split(" ")[0]
        title = f"Happy birthday{', ' + first if first else ''}!"
        msg = f"Here's {pct:g}% off anything, just for you. Use code {code} at checkout by {expires}."
        if u.get("push_promotions", True):
            await notify_user(u["id"], title, msg, {"url": "/shop", "kind": "promotion"})
        if email_enabled() and u.get("email") and u.get("email_verified"):
            paras = [title, f"To celebrate, here's {pct:g}% off any order, custom designs included.", f"Your code: {code}", f"It works once and is valid until {expires}."]
            await send_email(u["email"], f"{pct:g}% off for your birthday", layout("Happy birthday", paras, ("Start shopping", f"{SITE}/shop")), "\n".join(paras + [f"{SITE}/shop"]))
        sent += 1
