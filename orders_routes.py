"""Orders + Razorpay Standard Checkout (server-side pricing, signature verification)."""
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import razorpay
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config_store import LABELS, SIZES, load_config
from core import check_pin, client_ip, current_user, db

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def payments_enabled() -> bool:
    return bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"))


class Shipping(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=20)
    line1: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    postal: str = Field(min_length=3, max_length=12)
    country: str = Field(min_length=2, max_length=56)


class OrderIn(BaseModel):
    design_id: Optional[str] = None
    product_id: Optional[str] = None
    share_slug: Optional[str] = None
    size: str
    qty: int = Field(ge=1, le=20)
    currency: Literal["INR", "USD", "GBP", "EUR", "AED"]
    discount_code: Optional[str] = Field(default=None, max_length=20)
    shipping: Optional[Shipping] = None


PAID_STATES = ["paid", "printed", "shipped", "delivered"]
STATUSES = ["created", "paid", "printed", "shipped", "delivered", "cancelled"]


async def resolve_item(user: dict, body: OrderIn) -> dict:
    """Server-side source of truth for what is bought and its unit price (minor units)."""
    cfg = await load_config()
    if body.product_id:
        p = await db.catalog_products.find_one({"id": body.product_id, "status": "active"}, {"_id": 0})
        if not p:
            raise HTTPException(404, "Product not found")
        price = p["prices"].get(body.currency)
        if not price:
            raise HTTPException(400, f"This product isn't sold in {body.currency}")
        left = p["stock"].get(body.size)
        if left is None:
            raise HTTPException(400, "Pick a valid size")
        if left < body.qty:
            raise HTTPException(400, f"Only {left} left in size {body.size}" if left else f"Size {body.size} is sold out")
        return {"kind": "catalog", "product_id": p["id"], "creator_id": p.get("creator_id"), "name": p["name"], "product": "catalog",
                "color": None, "photo": (p["photos"] or [None])[0], "unit": int(round(price * 100)), "snapshot": p}
    if body.share_slug:
        sh = await db.shares.find_one({"slug": body.share_slug}, {"_id": 0})
        if not sh:
            raise HTTPException(404, "Shared design not found")
        design = sh["design"]
    else:
        design = await db.designs.find_one({"id": body.design_id, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
        if not design:
            raise HTTPException(404, "Save the design to your account first")
    product = design.get("product", "tee")
    if body.size not in SIZES[product]:
        raise HTTPException(400, "Pick a valid size")
    unit = cfg.products[product].prices[body.currency]
    return {"kind": "share" if body.share_slug else "custom", "design_id": design["id"], "name": design["name"], "product": product,
            "color": design["color"], "photo": design.get("thumb"), "unit": int(round(unit * 100)), "snapshot": design}


async def quote_for(user: dict, body: OrderIn) -> dict:
    from store_routes import find_discount, load_store

    item = await resolve_item(user, body)
    subtotal = item["unit"] * body.qty
    pct, label, code_error = 0.0, None, None
    store = await load_store()
    if store.first_order_pct > 0 and not await db.orders.find_one({"user_id": user["id"], "status": {"$in": PAID_STATES}}, {"_id": 1}):
        pct, label = store.first_order_pct, "FIRST ORDER"
    if body.discount_code and body.discount_code.strip():
        try:
            d = await find_discount(body.discount_code)
            if d["percent"] > pct:
                pct, label = d["percent"], d["code"]
        except HTTPException as e:
            code_error = e.detail
    discount = int(round(subtotal * pct / 100))
    total = max(100, subtotal - discount)
    return {"item": item, "subtotal": subtotal, "discount": subtotal - total if discount else 0, "discount_pct": pct,
            "discount_label": label, "total": total, "currency": body.currency, "code_error": code_error}


@router.post("/orders/quote")
async def quote(body: OrderIn, user: dict = Depends(current_user)):
    q = await quote_for(user, body)
    q.pop("item")
    return q


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


def _public(o: dict) -> dict:
    return {k: v for k, v in o.items() if k not in ("_id", "signature", "design_snapshot")}


@router.get("/features")
async def features():
    from auth_routes import sms_enabled

    return {"payments": payments_enabled(), "sms": sms_enabled()}


@router.post("/orders")
async def create_order(body: OrderIn, user: dict = Depends(current_user)):
    if not body.shipping:
        raise HTTPException(400, "Add a shipping address")
    if not payments_enabled():
        raise HTTPException(503, "Card payments aren't set up yet")
    q = await quote_for(user, body)
    if q["code_error"]:
        raise HTTPException(400, q["code_error"])
    item = q["item"]
    amount = q["total"]
    oid = str(uuid.uuid4())
    rzp = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))
    try:
        r = await run_in_threadpool(
            rzp.order.create, {"amount": amount, "currency": body.currency, "receipt": oid[:40], "notes": {"item": item["name"][:60]}}
        )
    except Exception as e:
        logger.warning("razorpay order failed: %s", e)
        msg = "Payment provider rejected this currency" if body.currency != "INR" else "Couldn't start payment, try again"
        raise HTTPException(502, msg)
    order = {
        "id": oid,
        "user_id": user["id"],
        "phone": user["phone"],
        "kind": item["kind"],
        "design_id": item.get("design_id"),
        "product_id": item.get("product_id"),
        "creator_id": item.get("creator_id"),
        "share_slug": body.share_slug,
        "design_name": item["name"],
        "product": item["product"],
        "color": item["color"],
        "photo": item["photo"],
        "size": body.size,
        "qty": body.qty,
        "currency": body.currency,
        "unit_amount": item["unit"],
        "subtotal": q["subtotal"],
        "discount": q["discount"],
        "discount_label": q["discount_label"],
        "amount": amount,
        "shipping": body.shipping.model_dump(),
        "status": "created",
        "history": [{"status": "created", "at": datetime.now(timezone.utc).isoformat()}],
        "razorpay_order_id": r["id"],
        "design_snapshot": item["snapshot"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.orders.insert_one(dict(order))
    return {
        "order": _public(order),
        "checkout": {
            "key_id": os.environ["RAZORPAY_KEY_ID"],
            "order_id": r["id"],
            "amount": amount,
            "currency": body.currency,
            "name": "DropForge",
            "description": f"{LABELS.get(item['product'], item['product'].upper())} - {item['name']} ({body.size} x{body.qty})",
            "prefill": {"contact": user["phone"], "name": body.shipping.name},
        },
    }


@router.post("/orders/verify")
async def verify_order(body: VerifyIn, user: dict = Depends(current_user)):
    saved = await db.orders.find_one({"razorpay_order_id": body.razorpay_order_id, "user_id": user["id"]})
    if not saved:
        raise HTTPException(404, "Order not found")
    secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    expected = hmac.new(secret.encode(), f"{saved['razorpay_order_id']}|{body.razorpay_payment_id}".encode(), hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected, body.razorpay_signature):
        raise HTTPException(400, "Payment could not be verified")
    now = datetime.now(timezone.utc).isoformat()
    r = await db.orders.update_one(
        {"_id": saved["_id"], "status": "created"},
        {
            "$set": {"status": "paid", "payment_id": body.razorpay_payment_id, "signature": body.razorpay_signature, "paid_at": now},
            "$push": {"history": {"status": "paid", "at": now}},
        },
    )
    if r.modified_count:  # first verification only: stock, discount usage, creator commission
        if saved.get("product_id"):
            ok = await db.catalog_products.update_one(
                {"id": saved["product_id"], f"stock.{saved['size']}": {"$gte": saved["qty"]}}, {"$inc": {f"stock.{saved['size']}": -saved["qty"]}}
            )
            if not ok.modified_count:
                await db.orders.update_one({"_id": saved["_id"]}, {"$set": {"stock_issue": True}})
        if saved.get("discount_label") and saved["discount_label"] != "FIRST ORDER":
            await db.discounts.update_one({"code": saved["discount_label"]}, {"$inc": {"uses": 1}})
        if saved.get("creator_id"):
            from store_routes import load_store

            pct = (await load_store()).commission_pct
            await db.earnings.insert_one({
                "id": str(uuid.uuid4()), "creator_id": saved["creator_id"], "order_id": saved["id"], "product_name": saved["design_name"],
                "currency": saved["currency"], "pct": pct, "amount": int(round(saved["amount"] * pct / 100)), "status": "pending", "created_at": now,
            })
    doc = await db.orders.find_one({"_id": saved["_id"]})
    return _public(doc)


class StatusIn(BaseModel):
    status: Literal["paid", "printed", "shipped", "delivered", "cancelled"]


def send_sms(to: str, text: str) -> bool:
    sid, tok, frm = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    if not (sid and tok and frm):
        return False
    try:
        from twilio.rest import Client

        Client(sid, tok).messages.create(to=to, from_=frm, body=text)
        return True
    except Exception as e:
        logger.warning("sms failed: %s", e)
        return False


@router.patch("/admin/orders/{oid}/status")
async def set_status(oid: str, body: StatusIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    o = await db.orders.find_one({"id": oid})
    if not o:
        raise HTTPException(404, "Order not found")
    if o["status"] == "created":
        raise HTTPException(400, "This order hasn't been paid yet")
    flow = ["paid", "printed", "shipped", "delivered"]
    if o["status"] in ("cancelled", "delivered"):
        raise HTTPException(400, f"This order is already {o['status']}")
    if body.status != "cancelled" and flow.index(body.status) <= flow.index(o["status"]):
        raise HTTPException(400, "Orders can only move forward: paid → printed → shipped → delivered")
    if body.status == "cancelled":
        if o.get("product_id") and not o.get("stock_issue"):
            await db.catalog_products.update_one({"id": o["product_id"]}, {"$inc": {f"stock.{o['size']}": o["qty"]}})
        await db.earnings.update_many({"order_id": oid, "status": "pending"}, {"$set": {"status": "void"}})
    now = datetime.now(timezone.utc).isoformat()
    await db.orders.update_one({"id": oid}, {"$set": {"status": body.status}, "$push": {"history": {"status": body.status, "at": now}}})
    phone = o.get("phone") or (await db.users.find_one({"id": o["user_id"]}, {"phone": 1}) or {}).get("phone")
    sms = bool(phone) and await run_in_threadpool(
        send_sms, phone, f"DropForge: your order #{oid[:8].upper()} ({o['design_name']}) is now {body.status.upper()}."
    )
    doc = await db.orders.find_one({"id": oid})
    return {**_public(doc), "sms_sent": sms}


@router.get("/orders")
async def my_orders(user: dict = Depends(current_user)):
    docs = await db.orders.find({"user_id": user["id"]}).sort("created_at", -1).to_list(200)
    return [_public(d) for d in docs]


@router.get("/admin/orders")
async def admin_orders(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    docs = await db.orders.find({}).sort("created_at", -1).to_list(200)
    return [_public(d) for d in docs]
