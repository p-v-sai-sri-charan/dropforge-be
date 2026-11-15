"""AI PNG images (OpenAI gpt-image) for signed-in users: monthly free allowance + purchasable credits (Razorpay).
Approved creators can instead bring their own OpenAI key (stored encrypted, billed to them, no credits used)."""
import base64
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
import razorpay
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ai_guard import guard
from core import JWT_SECRET, client_ip, current_user, db
from files_routes import store_file

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "medium")
FREE_PER_MONTH = int(os.environ.get("AI_FREE_IMAGES_PER_MONTH", "3"))
COST = 1  # credits per image

# Credit packs (prices in minor units: paise / cents / pence / fils). Set above the provider cost so each image covers
# the model, moderation and payment fees. Edit here to change pricing.
PACKS = {
    "p20": {"credits": 20, "prices": {"INR": 39900, "USD": 499, "GBP": 399, "EUR": 449, "AED": 1900}},
    "p50": {"credits": 50, "prices": {"INR": 89900, "USD": 1099, "GBP": 899, "EUR": 999, "AED": 3900}},
    "p120": {"credits": 120, "prices": {"INR": 199900, "USD": 2399, "GBP": 1899, "EUR": 2199, "AED": 8900}},
}
Currency = Literal["INR", "USD", "GBP", "EUR", "AED"]


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _fernet() -> Fernet:
    secret = os.environ.get("KEY_ENCRYPTION_SECRET") or JWT_SECRET
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"ai-keys:{secret}".encode()).digest()))


async def _is_creator(user: dict) -> bool:
    return bool(await db.creators.find_one({"user_id": user["id"], "status": "approved"}, {"_id": 1}))


async def _own_key(user: dict) -> Optional[str]:
    rec = await db.ai_keys.find_one({"user_id": user["id"]}, {"_id": 0})
    if not rec:
        return None
    try:
        return _fernet().decrypt(rec["enc"].encode()).decode()
    except InvalidToken:
        return None


async def _wallet(user: dict) -> dict:
    """The user's credit wallet, with the free allowance reset on a new month."""
    m = _month()
    await db.ai_credits.update_one({"user_id": user["id"]}, {"$setOnInsert": {"paid": 0, "free_used": 0, "month": m}}, upsert=True)
    await db.ai_credits.update_one({"user_id": user["id"], "month": {"$ne": m}}, {"$set": {"month": m, "free_used": 0}})
    return await db.ai_credits.find_one({"user_id": user["id"]}, {"_id": 0})


async def _spend(user: dict) -> str:
    await _wallet(user)
    if (await db.ai_credits.update_one({"user_id": user["id"], "free_used": {"$lt": FREE_PER_MONTH}}, {"$inc": {"free_used": 1}})).modified_count:
        return "free"
    if (await db.ai_credits.update_one({"user_id": user["id"], "paid": {"$gte": COST}}, {"$inc": {"paid": -COST}})).modified_count:
        return "paid"
    raise HTTPException(402, "You're out of AI image credits. Buy a pack to keep generating.")


async def _refund(user: dict, source: str):
    await db.ai_credits.update_one({"user_id": user["id"]}, {"$inc": {"free_used": -1} if source == "free" else {"paid": COST}})


async def _status(user: dict) -> dict:
    w = await _wallet(user)
    creator = await _is_creator(user)
    rec = await db.ai_keys.find_one({"user_id": user["id"]}, {"_id": 0, "last4": 1})
    return {
        "free_left": max(0, FREE_PER_MONTH - w["free_used"]),
        "free_per_month": FREE_PER_MONTH,
        "paid": w["paid"],
        "credits": max(0, FREE_PER_MONTH - w["free_used"]) + w["paid"],
        "cost": COST,
        "is_creator": creator,
        "own_key": rec["last4"] if rec else None,
        "packs": [{"id": k, "credits": v["credits"], "prices": v["prices"]} for k, v in PACKS.items()],
    }


@router.get("/ai/credits")
async def credits(user: dict = Depends(current_user)):
    return await _status(user)


class ImageIn(BaseModel):
    prompt: str = Field(min_length=2, max_length=500)


PROMPT_WRAP = (
    "A print-ready graphic for apparel on a fully transparent background. Centered, bold and clean artwork with crisp edges. "
    "Do not draw a shirt, mockup, model, border, frame or background. Subject: {p}"
)


async def _openai_png(prompt: str, key: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=150) as http:
            r = await http.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": IMAGE_MODEL, "prompt": PROMPT_WRAP.format(p=prompt), "size": "1024x1024", "quality": IMAGE_QUALITY,
                      "background": "transparent", "output_format": "png", "n": 1},
            )
    except httpx.HTTPError as e:
        logger.warning("openai image request failed: %s", e)
        raise HTTPException(502, "The image service didn't respond. Please try again.")
    if r.status_code == 401:
        raise HTTPException(400, "The OpenAI key was rejected. Check the key in your AI settings.")
    if r.status_code == 400:
        msg = ((r.json().get("error") or {}).get("message") or "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if "safety" in msg.lower() or "moderation" in msg.lower():
            raise HTTPException(400, "That prompt was blocked by the image provider's safety system. Try a different idea.")
        logger.warning("openai image 400: %s", msg)
        raise HTTPException(502, "The image service couldn't make that image. Please try again.")
    if r.status_code == 429:
        raise HTTPException(429, "The image service is busy or out of quota. Please try again shortly.")
    if r.status_code >= 400:
        logger.warning("openai image %s: %s", r.status_code, r.text[:300])
        raise HTTPException(502, "The image service is unavailable. Please try again.")
    try:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    except Exception:
        raise HTTPException(502, "The image service returned no image. Please try again.")


@router.post("/ai/generate-image")
async def generate_image(body: ImageIn, request: Request, user: dict = Depends(current_user)):
    from server import ask_claude  # lazy: server imports this module

    own = await _own_key(user) if await _is_creator(user) else None
    platform = os.environ.get("OPENAI_API_KEY", "")
    if not own and not platform:
        raise HTTPException(503, "AI images aren't set up yet")
    await guard(body.prompt, "image", user, client_ip(request), ask_claude)
    source = "own_key"
    if not own:
        source = await _spend(user)
    try:
        png = await _openai_png(body.prompt, own or platform)
        saved = await store_file(png, "image/png", user["id"], False, png_only=True)
    except Exception:
        if source != "own_key":
            await _refund(user, source)  # never charge for a failed image
        raise
    await db.ai_images.insert_one({"id": str(uuid.uuid4()), "user_id": user["id"], "prompt": body.prompt[:500], "source": source,
                                   "path": saved["path"], "created_at": datetime.now(timezone.utc).isoformat()})
    return {"url": saved["url"], "source": source, **(await _status(user))}


# ---- creators: bring your own OpenAI key ----
class KeyIn(BaseModel):
    api_key: str = Field(min_length=20, max_length=300)


@router.put("/ai/openai-key")
async def save_key(body: KeyIn, user: dict = Depends(current_user)):
    if not await _is_creator(user):
        raise HTTPException(403, "Only approved creators can use their own API key")
    key = body.api_key.strip()
    if not key.startswith("sk-"):
        raise HTTPException(400, "That doesn't look like an OpenAI API key (it should start with sk-)")
    async with httpx.AsyncClient(timeout=20) as http:  # verify the key works before saving
        try:
            r = await http.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise HTTPException(502, "Couldn't reach OpenAI to check the key. Try again.")
    if r.status_code == 401:
        raise HTTPException(400, "OpenAI rejected that key")
    await db.ai_keys.update_one({"user_id": user["id"]}, {"$set": {"enc": _fernet().encrypt(key.encode()).decode(), "last4": key[-4:],
                                                                    "updated_at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
    return await _status(user)


@router.delete("/ai/openai-key")
async def delete_key(user: dict = Depends(current_user)):
    await db.ai_keys.delete_one({"user_id": user["id"]})
    return await _status(user)


# ---- buy credits ----
class BuyIn(BaseModel):
    pack: Literal["p20", "p50", "p120"]
    currency: Currency


@router.post("/ai/credits/checkout")
async def buy_checkout(body: BuyIn, user: dict = Depends(current_user)):
    if not (os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET")):
        raise HTTPException(503, "Card payments aren't set up yet")
    pack = PACKS[body.pack]
    amount = pack["prices"][body.currency]
    oid = str(uuid.uuid4())
    rzp = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))
    try:
        r = await run_in_threadpool(rzp.order.create, {"amount": amount, "currency": body.currency, "receipt": oid[:40], "notes": {"credits": pack["credits"]}})
    except Exception as e:
        logger.warning("razorpay credit order failed: %s", e)
        raise HTTPException(502, "Couldn't start payment, try again")
    await db.credit_orders.insert_one({"id": oid, "user_id": user["id"], "pack": body.pack, "credits": pack["credits"], "amount": amount,
                                       "currency": body.currency, "status": "created", "razorpay_order_id": r["id"],
                                       "created_at": datetime.now(timezone.utc).isoformat()})
    return {"checkout": {"key_id": os.environ["RAZORPAY_KEY_ID"], "order_id": r["id"], "amount": amount, "currency": body.currency, "name": "DropForge",
                         "description": f"{pack['credits']} AI image credits", "prefill": {"contact": user["phone"]}}}


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/ai/credits/verify")
async def buy_verify(body: VerifyIn, user: dict = Depends(current_user)):
    saved = await db.credit_orders.find_one({"razorpay_order_id": body.razorpay_order_id, "user_id": user["id"]})
    if not saved:
        raise HTTPException(404, "Order not found")
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    expected = hmac.new(secret.encode(), f"{saved['razorpay_order_id']}|{body.razorpay_payment_id}".encode(), hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected, body.razorpay_signature):
        raise HTTPException(400, "Payment could not be verified")
    done = await db.credit_orders.update_one({"_id": saved["_id"], "status": "created"},
                                             {"$set": {"status": "paid", "payment_id": body.razorpay_payment_id, "paid_at": datetime.now(timezone.utc).isoformat()}})
    if done.modified_count:  # credit only on the first verification
        await _wallet(user)
        await db.ai_credits.update_one({"user_id": user["id"]}, {"$inc": {"paid": saved["credits"]}})
    return await _status(user)
