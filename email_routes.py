"""Verifying the email on a customer's profile (a 6-digit code), and order emails - which only go to verified
addresses, so a typo never sends someone's order details to a stranger."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import JWT_SECRET, current_user, db
from mailer import SITE, email_enabled, layout, send_email

router = APIRouter(prefix="/api/auth/email")

CODE_MINUTES = 15
SENDS_PER_HOUR = 5
MAX_TRIES = 5


def _hash(user_id: str, email: str, code: str) -> str:
    return hmac.new(JWT_SECRET.encode(), f"{user_id}|{email}|{code}".encode(), hashlib.sha256).hexdigest()


@router.post("/send-code")
async def send_code(user: dict = Depends(current_user)):
    email = user.get("email")
    if not email:
        raise HTTPException(400, "Add an email to your profile first")
    if user.get("email_verified"):
        return {"sent": False, "verified": True}
    if not email_enabled():
        raise HTTPException(503, "Email isn't set up yet")
    now = datetime.now(timezone.utc)
    if await db.email_codes.count_documents({"user_id": user["id"], "at": {"$gte": now - timedelta(hours=1)}}) >= SENDS_PER_HOUR:
        raise HTTPException(429, "Too many codes requested. Try again in an hour")
    code = f"{secrets.randbelow(1_000_000):06d}"
    await db.email_codes.insert_one({"user_id": user["id"], "email": email, "hash": _hash(user["id"], email, code), "tries": 0, "at": now})
    ok = await send_email(
        email,
        f"{code} is your DropForge verification code",
        layout("Verify your email", [f"Your code is {code}. It expires in {CODE_MINUTES} minutes.", "If you didn't ask for this, you can ignore this email."]),
        f"Your DropForge verification code is {code}. It expires in {CODE_MINUTES} minutes.",
    )
    if not ok:
        raise HTTPException(502, "Couldn't send the email. Check the address and try again")
    return {"sent": True}


class VerifyIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


@router.post("/verify")
async def verify(body: VerifyIn, user: dict = Depends(current_user)):
    email = user.get("email")
    since = datetime.now(timezone.utc) - timedelta(minutes=CODE_MINUTES)
    rec = await db.email_codes.find_one({"user_id": user["id"], "email": email, "at": {"$gte": since}}, sort=[("at", -1)])
    if not email or not rec:
        raise HTTPException(400, "That code has expired. Send a new one")
    if rec["tries"] >= MAX_TRIES:
        raise HTTPException(429, "Too many wrong codes. Send a new one")
    if not hmac.compare_digest(rec["hash"], _hash(user["id"], email, body.code)):
        await db.email_codes.update_one({"_id": rec["_id"]}, {"$inc": {"tries": 1}})
        raise HTTPException(400, "That code isn't right")
    await db.users.update_one({"id": user["id"], "email": email}, {"$set": {"email_verified": True, "email_verified_at": datetime.now(timezone.utc).isoformat()}})
    await db.email_codes.delete_many({"user_id": user["id"]})
    return {"verified": True}


async def ensure_indexes():
    await db.email_codes.create_index("at", expireAfterSeconds=24 * 3600)


# ---------------------------------------------------------------- order emails

SYMBOL = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€", "AED": "AED "}
SUBJECTS = {
    "paid": ("Order confirmed", "Thanks for your order! We'll print it and let you know when it ships."),
    "printed": ("Your order is printed", "Your order has been printed and is being packed."),
    "shipped": ("Your order has shipped", "Your order is on its way."),
    "delivered": ("Your order was delivered", "Your order has been delivered. We hope you love it!"),
    "cancelled": ("Your order was cancelled", "Your order was cancelled. If you paid, your refund is being processed."),
    "refunded": ("Your refund is on its way", "We've issued your refund. It usually reaches your account within 5-7 business days."),
    "reminder": ("Your design is waiting", "You started checking out but didn't finish. Your order is still saved."),
}


def money(amount: int, currency: str) -> str:
    return f"{SYMBOL.get(currency, currency + ' ')}{amount / 100:,.2f}"


async def email_order(order: dict, kind: str, extra: Optional[str] = None) -> bool:
    """Order update to the customer's verified email (no-op otherwise)."""
    if kind not in SUBJECTS or not email_enabled():
        return False
    u = await db.users.find_one({"id": order.get("user_id")}, {"_id": 0, "email": 1, "email_verified": 1, "name": 1})
    if not u or not u.get("email") or not u.get("email_verified"):
        return False
    title, lead = SUBJECTS[kind]
    ref = order["id"][:8].upper()
    rows = [("Order", f"#{ref}"), ("Item", order.get("design_name", "")), ("Size × qty", f"{order.get('size', '')} × {order.get('qty', 1)}"),
            ("Total", money(order.get("amount", 0), order.get("currency", "INR")))]
    paras = [f"Hi {u['name'].split()[0]}," if u.get("name") else "Hi,", lead] + ([extra] if extra else [])
    track = order.get("tracking_url")
    button = ("Track your parcel", track) if kind == "shipped" and track else (("Complete your order", f"{SITE}/orders") if kind == "reminder" else ("View your orders", f"{SITE}/orders"))
    text = "\n".join(paras + [f"{k}: {v}" for k, v in rows] + [f"{button[0]}: {button[1]}"])
    return await send_email(u["email"], f"{title} · #{ref}", layout(title, paras, button, rows), text)
