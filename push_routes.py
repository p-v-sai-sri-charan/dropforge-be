"""App push notifications: device registration, the customer's promotions opt-out, and admin promotional broadcasts."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core import check_pin, client_ip, current_user, db
from push import send_push

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

TOKEN_RE = r"^Expo(nent)?PushToken\[[A-Za-z0-9_\-]{10,200}\]$"


class RegisterIn(BaseModel):
    token: str = Field(pattern=TOKEN_RE)
    platform: Literal["ios", "android"]


@router.post("/push/register")
async def register(body: RegisterIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc).isoformat()
    # a device belongs to whoever signed in on it last
    await db.push_tokens.update_one(
        {"token": body.token},
        {"$set": {"user_id": user["id"], "platform": body.platform, "updated_at": now, "promo": user.get("push_promotions", True)},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return {"ok": True, "promotions": user.get("push_promotions", True)}


class UnregisterIn(BaseModel):
    token: str = Field(pattern=TOKEN_RE)


@router.post("/push/unregister")
async def unregister(body: UnregisterIn):
    """Called on sign-out. Knowing the token is enough: it only stops notifications to that one device."""
    await db.push_tokens.delete_one({"token": body.token})
    return {"ok": True}


class PrefsIn(BaseModel):
    promotions: bool


@router.get("/push/prefs")
async def get_prefs(user: dict = Depends(current_user)):
    return {"promotions": user.get("push_promotions", True)}


@router.put("/push/prefs")
async def set_prefs(body: PrefsIn, user: dict = Depends(current_user)):
    await db.users.update_one({"id": user["id"]}, {"$set": {"push_promotions": body.promotions}})
    await db.push_tokens.update_many({"user_id": user["id"]}, {"$set": {"promo": body.promotions}})  # read by broadcasts
    return {"promotions": body.promotions}


class BroadcastIn(BaseModel):
    title: str = Field(min_length=2, max_length=60)
    body: str = Field(min_length=2, max_length=180)
    url: Optional[str] = Field(default=None, max_length=200, pattern=r"^/([A-Za-z0-9_\-?=&.%][A-Za-z0-9/_\-?=&.%]*)?$")  # in-app path opened on tap (never "//host")
    dry_run: bool = True


PROMO = {"promo": {"$ne": False}}  # devices whose owner hasn't turned promotions off (indexed)
CHUNK = 1000


async def _send_broadcast(rec: dict, title: str, text: str, data: dict):
    """Runs after the request returns: streams devices in chunks (memory stays flat) and records progress."""
    sent = failed = 0
    try:
        chunk = []
        async for t in db.push_tokens.find(PROMO, {"token": 1}):
            chunk.append(t["token"])
            if len(chunk) >= CHUNK:
                r = await send_push(chunk, title, text, data)
                sent, failed, chunk = sent + r["sent"], failed + r["failed"], []
                await db.push_campaigns.update_one({"id": rec["id"]}, {"$set": {"sent": sent, "failed": failed}})
        if chunk:
            r = await send_push(chunk, title, text, data)
            sent, failed = sent + r["sent"], failed + r["failed"]
        status = "sent"
    except Exception as e:
        logger.warning("broadcast %s stopped: %s", rec["id"], e)
        status = "stopped"
    await db.push_campaigns.update_one({"id": rec["id"]}, {"$set": {"sent": sent, "failed": failed, "status": status, "finished_at": datetime.now(timezone.utc).isoformat()}})


@router.post("/admin/push/broadcast")
async def broadcast(body: BroadcastIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    """Promotion to every app user who hasn't turned promotions off. dry_run (default) only counts the devices."""
    check_pin(x_admin_pin, client_ip(request))
    devices = await db.push_tokens.count_documents(PROMO)
    if body.dry_run:
        return {"devices": devices}
    if not devices:
        raise HTTPException(400, "No app users can receive promotions yet")
    rec = {"id": str(uuid.uuid4()), "title": body.title.strip(), "body": body.body.strip(), "url": body.url, "devices": devices, "sent": 0, "failed": 0,
           "status": "sending", "at": datetime.now(timezone.utc).isoformat()}
    await db.push_campaigns.insert_one(dict(rec))
    # sending to many devices takes minutes: do it in the background so the request never times out
    asyncio.create_task(_send_broadcast(rec, rec["title"], rec["body"], {"url": body.url or "/", "kind": "promotion"}))
    return rec


@router.get("/admin/push/campaigns")
async def past_broadcasts(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    return await db.push_campaigns.find({}, {"_id": 0}).sort("at", -1).to_list(20)


async def ensure_indexes():
    await db.push_tokens.create_index("token", unique=True)
    await db.push_tokens.create_index("user_id")
