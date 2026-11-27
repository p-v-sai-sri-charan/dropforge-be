"""Account deletion (required by the App Store and Google Play): customers can delete their account from the app or
the website. Personal data, designs and uploads are erased; order and payment records are kept because tax and
consumer law require them (stated in the privacy policy). Deletion waits while an order is still on its way or a
creator is still owed money, so nothing in flight is lost."""
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import current_user, db, is_admin_phone
from files_routes import design_srcs, release_files

router = APIRouter(prefix="/api/account")

IN_FLIGHT = ["paid", "printed", "shipped"]


async def _blockers(user: dict) -> list:
    out = []
    if is_admin_phone(user.get("phone")):
        out.append("Admin accounts can't be deleted from the app. Remove the number from ADMIN_PHONES first.")
    n = await db.orders.count_documents({"user_id": user["id"], "status": {"$in": IN_FLIGHT}})
    if n:
        out.append(f"You have {n} order{'s' if n != 1 else ''} on the way. You can delete your account once {'they are' if n != 1 else 'it is'} delivered.")
    c = await db.creators.find_one({"user_id": user["id"]}, {"_id": 0, "id": 1})
    if c and await db.earnings.count_documents({"creator_id": c["id"], "status": "pending"}):
        out.append("You have creator earnings we haven't paid out yet. Contact support so we can settle them first.")
    return out


@router.get("/deletion")
async def deletion_check(user: dict = Depends(current_user)):
    """What deleting would do for this user, and anything that has to happen first."""
    credits = await db.ai_credits.find_one({"user_id": user["id"]}, {"_id": 0, "paid": 1}) or {}
    return {
        "blockers": await _blockers(user),
        "designs": await db.designs.count_documents({"user_id": user["id"]}),
        "orders": await db.orders.count_documents({"user_id": user["id"], "status": {"$ne": "created"}}),
        "ai_credits": int(credits.get("paid") or 0),
        "creator": bool(await db.creators.find_one({"user_id": user["id"]}, {"_id": 1})),
    }


class DeleteIn(BaseModel):
    confirm: Literal["DELETE"]


@router.post("/delete")
async def delete_account(body: DeleteIn, user: dict = Depends(current_user)):
    blockers = await _blockers(user)
    if blockers:
        raise HTTPException(409, blockers[0])
    uid = user["id"]

    # designs + the images only they used (anything an order still needs is kept for printing)
    srcs = []
    async for d in db.designs.find({"user_id": uid}, {"_id": 0}):
        srcs += design_srcs(d)
    await db.designs.delete_many({"user_id": uid})
    async for s in db.shares.find({"user_id": uid}, {"_id": 0}):
        srcs += [s.get("front_image"), s.get("back_image"), *(design_srcs(s["design"]) if s.get("design") else [])]
    await db.shares.delete_many({"user_id": uid})
    srcs += ["/api/files/" + f["path"] async for f in db.files.find({"owner_id": uid, "is_deleted": False}, {"path": 1})]

    # creator: products leave the shop, KYC details are erased (payout records stay with the earnings)
    c = await db.creators.find_one({"user_id": uid}, {"_id": 0, "id": 1})
    if c:
        await db.catalog_products.update_many({"creator_id": c["id"]}, {"$set": {"status": "hidden", "creator_deleted": True}})
        await db.creators.delete_one({"user_id": uid})

    for coll in (db.push_tokens, db.ai_keys, db.ai_credits, db.ai_images):
        await coll.delete_many({"user_id": uid})
    await db.events.update_many({"user_id": uid}, {"$set": {"user_id": None}})
    await db.orders.update_many({"user_id": uid}, {"$set": {"account_deleted": True}})  # kept for tax/consumer-law records
    await db.credit_orders.update_many({"user_id": uid}, {"$set": {"account_deleted": True}})
    await db.users.delete_one({"id": uid})
    # proof the request was honoured, without keeping the phone number itself
    await db.deleted_accounts.insert_one({
        "id": str(uuid.uuid4()),
        "phone_sha256": hashlib.sha256(user["phone"].encode()).hexdigest(),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    await release_files(srcs)  # after the owning documents are gone
    return {"ok": True}
