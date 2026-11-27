"""Database indexes for every lookup the app makes, so finding a record stays fast (O(log n)) as data grows instead
of scanning whole collections (O(n)). Created at startup; creating an index that already exists is a no-op.

Unique indexes also stop duplicates (e.g. two accounts for one phone number when two sign-ins race). Before making
one unique, existing data is checked: if duplicates are already there, a normal index is created instead and the
duplicates are logged, so startup never fails and nothing is changed automatically.
"""
import logging

from pymongo import ASCENDING as A, DESCENDING as D

from core import db

logger = logging.getLogger(__name__)

# (collection, keys, options). Comment = the queries each index serves.
INDEXES = [
    # users: every signed-in request (current_user by id), sign-in (by phone), dashboard sign-ups, birthday job
    ("users", [("id", A)], {"unique": True}),
    ("users", [("phone", A)], {"unique": True}),
    ("users", [("created_at", A)], {}),
    ("users", [("dob_md", A)], {"sparse": True}),
    # orders: customer's order list, order pages, payment confirmation, admin tabs, reminders, dashboard, code stats
    ("orders", [("id", A)], {"unique": True}),
    ("orders", [("user_id", A), ("created_at", D)], {}),
    ("orders", [("razorpay_order_id", A)], {"sparse": True}),
    ("orders", [("status", A), ("created_at", D)], {}),
    ("orders", [("created_at", D)], {}),
    ("orders", [("day", A)], {"sparse": True}),
    ("orders", [("paid_at", A)], {"sparse": True}),
    ("orders", [("discount_label", A)], {"sparse": True}),
    ("orders", [("product_id", A), ("user_id", A)], {"sparse": True}),  # "did this customer receive it" (reviews)
    # designs: "my designs", design pages, admin moderation list
    ("designs", [("id", A)], {"unique": True}),
    ("designs", [("user_id", A), ("updatedAt", D)], {}),
    ("designs", [("updatedAt", D)], {}),
    # files: every image request (by path), account deletion (by owner)
    ("files", [("path", A)], {"unique": True}),
    ("files", [("owner_id", A)], {}),
    # shop
    ("catalog_products", [("id", A)], {"unique": True}),
    ("catalog_products", [("status", A), ("created_at", D), ("id", A)], {}),  # shop listing (newest first, paged)
    ("catalog_products", [("status", A), ("category_id", A), ("created_at", D)], {}),
    ("catalog_products", [("creator_id", A), ("created_at", D)], {}),
    ("categories", [("id", A)], {"unique": True}),
    ("categories", [("parent_id", A)], {}),
    ("creators", [("id", A)], {"unique": True}),
    ("creators", [("user_id", A)], {"unique": True}),
    ("creators", [("slug", A)], {"unique": True, "partialFilterExpression": {"slug": {"$type": "string"}}}),
    ("creators", [("status", A), ("created_at", D)], {}),
    ("earnings", [("creator_id", A), ("status", A), ("created_at", D)], {}),
    ("earnings", [("creator_id", A), ("created_at", D)], {}),
    ("earnings", [("order_id", A)], {}),
    ("earnings", [("id", A)], {}),
    ("discounts", [("code", A)], {"unique": True}),
    ("discounts", [("creator_id", A), ("created_at", D)], {"sparse": True}),
    ("discounts", [("user_id", A)], {"sparse": True}),
    ("discounts", [("created_at", D)], {}),
    ("shares", [("slug", A)], {"unique": True}),
    ("shares", [("user_id", A)], {}),
    ("shares", [("created_at", D)], {}),
    # AI
    ("ai_credits", [("user_id", A)], {"unique": True}),
    ("ai_keys", [("user_id", A)], {"unique": True}),
    ("ai_images", [("user_id", A)], {}),
    ("credit_orders", [("razorpay_order_id", A)], {"sparse": True}),
    ("credit_orders", [("user_id", A)], {}),
    ("ai_usage", [("key", A)], {}),
    ("text_checks", [("key", A)], {}),
    ("text_verdicts", [("key", A)], {}),
    ("ai_violations", [("key", A), ("created_at", D)], {}),
    ("ai_violations", [("created_at", D)], {}),
    # sign-in and email limits
    ("otp_sends", [("phone", A), ("at", D)], {}),
    ("otp_sends", [("ip", A), ("at", D)], {}),
    ("otp_fails", [("phone", A), ("at", D)], {}),
    ("email_codes", [("user_id", A), ("at", D)], {}),
    # tracking + push
    ("events", [("day", A), ("anon_id", A)], {"sparse": True}),
    ("events", [("user_id", A)], {"sparse": True}),
    ("push_tokens", [("promo", A)], {}),
    ("push_campaigns", [("at", D)], {}),
    ("fonts", [("id", A)], {}),
    ("fonts", [("family", A)], {}),
    ("settings", [("key", A)], {}),
]


async def _duplicates(coll: str, keys: list, opts: dict) -> list:
    """A few duplicated key values, if any (only documents the unique index would cover)."""
    match = opts.get("partialFilterExpression") or {k: {"$exists": True} for k, _ in keys}
    group = {"$group": {"_id": {k.replace(".", "_"): f"${k}" for k, _ in keys}, "n": {"$sum": 1}}}
    return await db[coll].aggregate([{"$match": match}, group, {"$match": {"n": {"$gt": 1}}}, {"$limit": 5}], allowDiskUse=True).to_list(5)


async def ensure_all() -> dict:
    made, downgraded = 0, []
    for coll, keys, opts in INDEXES:
        opts = dict(opts)
        try:
            if opts.get("unique"):
                dups = await _duplicates(coll, keys, opts)
                if dups:
                    opts.pop("unique")
                    downgraded.append(f"{coll}.{'+'.join(k for k, _ in keys)}")
                    logger.warning("index %s %s: duplicates exist, created as non-unique. Fix these records: %s", coll, keys, dups)
            await db[coll].create_index(keys, **opts)
            made += 1
        except Exception as e:  # one bad index must not stop the app from starting
            logger.warning("index %s %s failed: %s", coll, keys, e)
    return {"indexes": made, "not_unique_because_of_duplicates": downgraded}
