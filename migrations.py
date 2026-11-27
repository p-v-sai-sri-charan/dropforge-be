"""One-time data fixes that let the new indexes do their job. Safe to run on every start: each step only touches
records that still need it, so after the first run it finds nothing to do. Runs in the background at startup."""
import logging
from datetime import datetime, timezone

from core import db, ist_day

logger = logging.getLogger(__name__)
IST = "+05:30"
BATCH = 1000


async def _python_fill(coll: str, query: dict, compute) -> int:
    """Fallback when the database can't do it in one server-side update (older MongoDB, test mocks)."""
    n = 0
    while True:
        docs = await db[coll].find(query, {"_id": 1, "created_at": 1, "at": 1, "dob": 1}).limit(BATCH).to_list(BATCH)
        if not docs:
            return n
        for d in docs:
            await db[coll].update_one({"_id": d["_id"]}, {"$set": compute(d)})
        n += len(docs)


def _day_from(v) -> str:
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return ist_day(v)


async def _fill(coll: str, query: dict, pipeline: list, compute) -> int:
    try:
        r = await db[coll].update_many(query, pipeline)  # server-side, one pass
        return r.modified_count
    except Exception:
        return await _python_fill(coll, query, compute)


async def run() -> dict:
    out = {}
    try:
        # India-time day on old orders / sign-ups (from created_at) and visits (from at)
        from_iso = {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateFromString": {"dateString": "$created_at"}}, "timezone": IST}}
        for coll in ("orders", "users"):
            out[f"{coll}.day"] = await _fill(coll, {"day": {"$exists": False}, "created_at": {"$type": "string"}}, [{"$set": {"day": from_iso}}],
                                             lambda d: {"day": _day_from(d["created_at"])})
        out["events.day"] = await _fill("events", {"day": {"$exists": False}, "at": {"$type": "date"}},
                                        [{"$set": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$at", "timezone": IST}}}}],
                                        lambda d: {"day": _day_from(d["at"])})
        # birthday month-day ("06-05") from the saved date of birth ("YYYY-MM-DD")
        out["users.dob_md"] = await _fill("users", {"dob_md": {"$exists": False}, "dob": {"$type": "string", "$ne": ""}},
                                          [{"$set": {"dob_md": {"$substrCP": ["$dob", 5, 5]}}}], lambda d: {"dob_md": d["dob"][5:]})
        # push devices of people who turned promotions off (the broadcast reads this flag from the device record)
        if not await db.settings.find_one({"key": "migrated_push_promo"}):
            n, ids = 0, []
            async for u in db.users.find({"push_promotions": False}, {"id": 1}):
                ids.append(u["id"])
                if len(ids) >= BATCH:
                    n += (await db.push_tokens.update_many({"user_id": {"$in": ids}}, {"$set": {"promo": False}})).modified_count
                    ids = []
            if ids:
                n += (await db.push_tokens.update_many({"user_id": {"$in": ids}}, {"$set": {"promo": False}})).modified_count
            await db.settings.update_one({"key": "migrated_push_promo"}, {"$set": {"at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
            out["push_tokens.promo"] = n
    except Exception as e:  # never block the app; next start tries again
        logger.warning("data migration step failed: %s", e)
    changed = {k: v for k, v in out.items() if v}
    if changed:
        logger.info("data migrations: %s", changed)
    return out
