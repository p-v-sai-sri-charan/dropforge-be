"""Background jobs: a gentle reminder for checkouts that were started but never paid, and birthday offers. Runs in every server worker;
each order is claimed atomically, so it's reminded once no matter how many workers there are."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from birthday import send_birthday_offers
from core import db
from email_routes import email_order
from push import notify_user

logger = logging.getLogger(__name__)

EVERY_SECONDS = 600
REMIND_AFTER = timedelta(hours=1)  # give people time to finish on their own first
GIVE_UP_AFTER = timedelta(hours=24)  # don't nag about old checkouts


async def remind_abandoned_checkouts(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    window = {"$lte": (now - REMIND_AFTER).isoformat(), "$gte": (now - GIVE_UP_AFTER).isoformat()}
    sent = 0
    while True:
        # claim one unpaid, un-reminded checkout at a time
        o = await db.orders.find_one_and_update(
            {"status": "created", "created_at": window, "reminded_at": {"$exists": False}, "account_deleted": {"$ne": True}},
            {"$set": {"reminded_at": now.isoformat()}},
        )
        if not o:
            return sent
        # paid with a later attempt (each payment attempt is its own order) -> nothing to remind about
        if await db.orders.find_one({"user_id": o["user_id"], "status": {"$ne": "created"}, "created_at": {"$gte": o["created_at"]}}, {"_id": 1}):
            continue
        await notify_user(o["user_id"], "Your design is waiting", f"Finish your order for {o.get('design_name', 'your design')} - it's still saved.", {"url": "/orders"})
        await email_order(o, "reminder")
        sent += 1


async def run_forever():
    await asyncio.sleep(30)  # let the server finish starting
    while True:
        try:
            n = await remind_abandoned_checkouts()
            if n:
                logger.info("sent %d checkout reminders", n)
        except Exception as e:  # never let the loop die
            logger.warning("checkout reminders failed: %s", e)
        try:
            n = await send_birthday_offers()
            if n:
                logger.info("sent %d birthday offers", n)
        except Exception as e:
            logger.warning("birthday offers failed: %s", e)
        await asyncio.sleep(EVERY_SECONDS)
