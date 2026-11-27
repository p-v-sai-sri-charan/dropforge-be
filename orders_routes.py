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

from config_store import LABELS, load_config
from core import check_pin, client_ip, current_user, db
from email_routes import email_order
from push import notify_user
from tracking_routes import Attribution

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
    attribution: Optional[Attribution] = None  # campaign / link the customer came from (promotion tracking)


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
    if body.size not in cfg.products[product].sizes:
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
            d = await find_discount(body.discount_code, user, item)
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


def cancel_policy(o: dict) -> str:
    """Who may cancel what: custom designs and creator products never (customers contact support); only products the admin
    listed in the shop, and only before the order is processed (printed / shipped)."""
    if o["status"] in ("cancelled", "delivered"):
        return "closed"
    if o.get("kind") in ("custom", "share"):
        return "custom"
    if o.get("creator_id"):
        return "creator"
    return "allowed" if o["status"] in ("created", "paid") else "processed"


def _public(o: dict) -> dict:
    out = {k: v for k, v in o.items() if k not in ("_id", "signature", "design_snapshot")}
    pol = cancel_policy(o)
    out["cancel_policy"] = pol
    out["can_cancel"] = pol == "allowed" and o["status"] == "paid"
    return out


@router.get("/features")
async def features():
    from auth_routes import sms_enabled

    from mailer import email_enabled

    return {"payments": payments_enabled(), "sms": sms_enabled(), "email": email_enabled(), "support": os.environ.get("SUPPORT_CONTACT", "")}


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
    areas = None
    garment = item["product"] if item["product"] != "catalog" else (item["snapshot"].get("design") or {}).get("product")
    if garment:
        pc = (await load_config()).products[garment]
        areas = {"front": pc.front.model_dump(), "back": pc.back.model_dump()}  # print areas as they were when ordered
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
        "print_areas": areas,
        "attribution": (body.attribution.clean() if body.attribution else None) or user.get("attribution"),
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
        await notify_user(saved["user_id"], "Order confirmed", f"We got your order for {saved['design_name']}. We'll let you know when it ships.", {"url": "/orders"})
        await email_order({**saved, "status": "paid"}, "paid")
    doc = await db.orders.find_one({"_id": saved["_id"]})
    return _public(doc)


class TrackingIn(BaseModel):
    courier: Optional[str] = Field(default=None, max_length=40)
    tracking_number: Optional[str] = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    tracking_url: Optional[str] = Field(default=None, max_length=300, pattern=r"^https://[^\s<>\"']+$")


class StatusIn(TrackingIn):
    status: Literal["paid", "printed", "shipped", "delivered", "cancelled"]


# Courier tracking pages that can be built from just the AWB / tracking number. Anything else: give the link.
TRACK_URLS = {"shiprocket": "https://shiprocket.co/tracking/{n}"}


def tracking_fields(t: TrackingIn) -> dict:
    out = {}
    if t.courier:
        out["courier"] = t.courier.strip()
    if t.tracking_number:
        out["tracking_number"] = t.tracking_number.strip()
    url = t.tracking_url or (TRACK_URLS[t.courier.strip().lower()].format(n=t.tracking_number) if t.courier and t.tracking_number and t.courier.strip().lower() in TRACK_URLS else None)
    if url:
        out["tracking_url"] = url
    return out


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
    patch = {"status": body.status, **({"refund_due": True} if body.status == "cancelled" else {}), **(tracking_fields(body) if body.status == "shipped" else {})}
    await db.orders.update_one({"id": oid}, {"$set": patch, "$push": {"history": {"status": body.status, "at": now}}})
    phone = o.get("phone") or (await db.users.find_one({"id": o["user_id"]}, {"phone": 1}) or {}).get("phone")
    sms = bool(phone) and await run_in_threadpool(
        send_sms, phone, f"DropForge: your order #{oid[:8].upper()} ({o['design_name']}) is now {body.status.upper()}."
    )
    pushed = await notify_user(o["user_id"], PUSH_TITLES[body.status], f"Order #{oid[:8].upper()} - {o['design_name']}", {"url": "/orders"})
    doc = await db.orders.find_one({"id": oid})
    emailed = await email_order(doc, body.status)
    return {**_public(doc), "sms_sent": sms, "push_sent": pushed, "email_sent": emailed}


PUSH_TITLES = {
    "paid": "Order confirmed",
    "printed": "Your order is printed",
    "shipped": "Your order has shipped",
    "delivered": "Your order was delivered",
    "cancelled": "Your order was cancelled",
}

@router.patch("/admin/orders/{oid}/tracking")
async def set_tracking(oid: str, body: TrackingIn, request: Request, x_admin_pin: Optional[str] = Header(None)):
    """Add or correct the courier / tracking number on a shipped order (the customer sees a "Track parcel" link)."""
    check_pin(x_admin_pin, client_ip(request))
    fields = tracking_fields(body)
    if not fields:
        raise HTTPException(400, "Add a tracking number or link")
    r = await db.orders.update_one({"id": oid, "status": {"$in": ["shipped", "delivered"]}}, {"$set": fields})
    if not r.matched_count:
        raise HTTPException(400, "Only shipped orders have tracking")
    return _public(await db.orders.find_one({"id": oid}))


SUPPORT_HINT = "Custom design orders can't be cancelled. If you need help, contact support."
CANCEL_MESSAGES = {
    "custom": SUPPORT_HINT,
    "creator": "Orders for a creator's design can't be cancelled. If you need help, contact support.",
    "processed": "This order is already being processed and can't be cancelled. If you need help, contact support.",
    "closed": "This order is already closed.",
}


@router.post("/orders/{oid}/cancel")
async def cancel_my_order(oid: str, user: dict = Depends(current_user)):
    o = await db.orders.find_one({"id": oid, "user_id": user["id"]})
    if not o:
        raise HTTPException(404, "Order not found")
    pol = cancel_policy(o)
    if pol != "allowed":
        raise HTTPException(400, CANCEL_MESSAGES[pol])
    if o["status"] != "paid":
        raise HTTPException(400, "This order hasn't been paid, so there is nothing to cancel")
    now = datetime.now(timezone.utc).isoformat()
    r = await db.orders.update_one(
        {"_id": o["_id"], "status": "paid"},  # only while still unprocessed
        {"$set": {"status": "cancelled", "refund_due": True, "cancelled_by": "customer"}, "$push": {"history": {"status": "cancelled", "at": now}}},
    )
    if not r.modified_count:
        raise HTTPException(400, CANCEL_MESSAGES["processed"])
    if o.get("product_id") and not o.get("stock_issue"):
        await db.catalog_products.update_one({"id": o["product_id"]}, {"$inc": {f"stock.{o['size']}": o["qty"]}})
    return _public(await db.orders.find_one({"_id": o["_id"]}))


@router.post("/admin/orders/{oid}/refunded")
async def mark_refunded(oid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    now = datetime.now(timezone.utc).isoformat()
    r = await db.orders.update_one({"id": oid, "status": "cancelled", "refund_due": True}, {"$set": {"refund_due": False, "refunded_at": now}})
    if not r.matched_count:
        raise HTTPException(404, "No refund is due for this order")
    o = await db.orders.find_one({"id": oid})
    await notify_user(o["user_id"], "Refund issued", f"Your refund for {o['design_name']} is on its way.", {"url": "/orders"})
    await email_order(o, "refunded")
    return _public(o)


@router.get("/orders")
async def my_orders(user: dict = Depends(current_user)):
    docs = await db.orders.find({"user_id": user["id"]}).sort("created_at", -1).to_list(200)
    return [_public(d) for d in docs]


@router.get("/admin/orders")
async def admin_orders(request: Request, x_admin_pin: Optional[str] = Header(None)):
    check_pin(x_admin_pin, client_ip(request))
    docs = await db.orders.find({}).sort("created_at", -1).to_list(200)
    return [_public(d) for d in docs]


def _layer_out(l: dict) -> dict:
    return {k: l.get(k) for k in ("id", "kind", "src", "x", "y", "scale", "rotation", "text", "badge")}


@router.get("/admin/orders/{oid}/production")
async def order_production(oid: str, request: Request, x_admin_pin: Optional[str] = Header(None)):
    """Everything needed to print an order: the customer's (or creator's) final mockup plus every layer (images, text, AI art,
    badges) with its exact placement inside the print area."""
    check_pin(x_admin_pin, client_ip(request))
    o = await db.orders.find_one({"id": oid}, {"_id": 0})
    if not o:
        raise HTTPException(404, "Order not found")
    snap = o.get("design_snapshot") or {}
    art = snap.get("design") if o.get("kind") == "catalog" else snap  # creator products carry the design they were made from
    art = art or {}
    garment = art.get("product") if art else None
    areas = o.get("print_areas")
    if not areas and (garment or o.get("product") != "catalog"):
        pc = (await load_config()).products.get(garment or o.get("product", "tee"))
        areas = {"front": pc.front.model_dump(), "back": pc.back.model_dump()} if pc else None
    creator = None
    if o.get("creator_id"):
        c = await db.creators.find_one({"id": o["creator_id"]}, {"_id": 0, "display_name": 1, "slug": 1, "phone": 1})
        creator = c
    return {
        "order": _public(o),
        "creator": creator,
        "design": {
            "name": art.get("name") or o.get("design_name"),
            "product": garment or o.get("product"),
            "color": art.get("color") or o.get("color"),
            "mockup": art.get("mockup"),
            "thumb": art.get("thumb"),
            "photos": snap.get("photos") or [],
            "front": [_layer_out(l) for l in art.get("front", [])],
            "back": [_layer_out(l) for l in art.get("back", [])],
        },
        "areas": areas,
    }
